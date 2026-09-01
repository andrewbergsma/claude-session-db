#!/usr/bin/env python3
"""Native-CC session console — full-screen chat + inline kmcp reads (Direction A).

The reply-capable cockpit surface. Everything the UI shows is derived from the
session's OWN transcript (~/.claude/projects/<project>/<session>.jsonl):

  - chat turns              from user/assistant message records
  - kmcp reads (inline)     from mcp__*__(get_entry|get_section|get_entries)
                            tool_use blocks — app/path/mode/sections from `input`,
                            plus `knowledge-cli call <tool>` invoked through Bash
  - kmcp surfaced           from the SURFACE_TOOLS tool_result: every ref a
                            search OFFERED, cross-referenced client-side
                            against what was actually consumed
  - kmcp writes             from the WRITE_TOOLS tool_use ⨝ its tool_result —
                            which entries this session created/updated/patched,
                            and which passes were only a dry_run
  - response size (rail)    from the matching tool_result block, joined EXACTLY
                            by tool_use_id (no server, no time-match)
  - context detail          latest assistant message.usage (token counts)

...and one thing read off disk: the turn-angle headlines for the session's
latest turn, mined out-of-band by `csd angles-watch` into the angles state dir
(`$CSD_STATE_DIR/angles/<sid>.json`). The console never runs a probe, never
calls Ollama, and never queries kmcp — it renders what the miner already left
on disk. That keeps Direction A intact: transcript + state dir are the source
of truth; no service is reached into.

One deliberate exception to "no service is reached into": the per-session
tl;dr (tldr.py) — a last-3-turns catch-up judged by the same small local model
the angles probes use. Requests only ever serve the cached store off disk;
generation is queued to a single in-process background worker and lands on a
later poll, so the request path never blocks on a model.

Endpoints
  GET  /api/sessions               light nav list (project, title, state, mtime)
  GET  /api/session?id=<sid>       full transcript as a chronological event stream
  GET  /api/detail?id=<sid>&item=  the persisted detail behind one angle headline
  GET  /api/git?id=<sid>           repo status for the session's cwd (read-only)
  GET  /api/repos                  cross-repo inventory: trunk, ahead/behind,
                                   dirty, unmerged branches, live worktrees
                                   (cached-first; never fans out on request)
  POST /api/repos/refresh          force one registry+snapshot walk now
  GET  /api/repo?id=<sid>|root=    one repo in full: every branch + worktree,
                                   commits across all refs, and its PRs
  GET  /api/files?id=<sid>&path=   one directory listing under the session's
                                   repo root / cwd (read-only, root-confined)
  GET  /api/file?id=<sid>&path=    one file's text/metadata (raw=1: image bytes)
  GET  /api/claudemd?id=<sid>&n=   one CLAUDE.md memory file's content (read-only)
  GET  /api/timeline?id=<sid>      cached whole-session tl;dr timeline (never generates)
  GET  /api/version                running version+sha vs the repo on disk (staleness)
  GET  /api/changelog              CHANGELOG.md markdown, for the console's chip
  POST /api/answer                 {session_id, cwd, text} -> claude -p --resume
                                   (busy session -> message queued, never refused)
  POST /api/queue/cancel           {session_id, queue_id} -> drop a queued message
  POST /api/fork                   {session_id, cwd, text, at_uuid?}
  POST /api/priority               {session_id, priority: low|med|high|critical|null}
  POST /api/title                  {session_id, title: str|null} -> set/clear a title
  POST /api/topic                  {session_id, topic, subtopic} -> set/clear taxonomy
  GET  /api/topics                 managed topic -> subtopics list (autocomplete)
  GET  /api/tldr?id=<sid>          cached tldr store (pure read, never generates)
  POST /api/tldr                   {session_id, force?} -> ensure (run-if-stale; force regenerates)
  POST /api/timeline               {session_id, force?} -> ensure a whole-session timeline
  POST /api/title/dismiss          {session_id, proposal} -> dismiss a proposed title
  POST /api/batch                  {actions, session_ids|scope, options?:{force}}
                                   -> queue a fan-out (see the batch-ops section)
  GET  /api/cr/manifest?id=<sid>   CR manifest: one row per context block, with
                                   deterministic defaults + scaffolding floor
  POST /api/cr                     {session_id, stub[], refs[], confirm} —
                                   two-phase context-reduction fork (preview,
                                   then forge a redacted COPY; original untouched)
  POST /api/cr/search              {q, app?} -> kmcp hybrid_search for the cart
  POST /api/cr/compile             {refs[]} -> compiled context document only

Local: binds 127.0.0.1, no auth. Point-fork writes a NEW session file under
~/.claude/projects (never mutates the original).
"""
import gzip
import hmac
import json
import os
import queue as queuelib
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid as uuidlib
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .. import cr as crlib
from .. import tldr
from .. import session_timeline
from .. import version as vinfo
from ..angles import (ANGLE_SPECS, ANGLE_LABELS,
                      _WRITE_TOOLS as _ANGLE_WRITE_TOOLS)

ROOT = Path(__file__).parent
PROJECTS = Path.home() / ".claude" / "projects"
NAV_TAIL_BYTES = 256 * 1024      # nav only needs the tail for title/state
FULL_MAX_BYTES = 24 * 1024 * 1024  # guard: tail huge transcripts
MAX_NAV_SESSIONS = 40
MAX_AGE_H = 72
ANSWER_LOG = ROOT / "answers.log"

# Unauthenticated *page* loads get this instead of a raw-JSON 401: a phone
# with an expired cookie needs a paste-the-token form, not a JSON wall it
# can't act on. API paths keep the JSON 401 (the client guards on it).
LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark"><title>session console — sign in</title>
<style>
  body{background:#0d1117;color:#e6edf3;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       display:flex;align-items:center;justify-content:center;min-height:100dvh;margin:0;padding:16px;}
  form{background:#161b22;border:1px solid #2d333b;border-radius:12px;padding:24px;width:min(380px,94vw);}
  h1{font-size:15px;margin:0 0 6px;}
  p{color:#9da7b3;font-size:12.5px;margin:0 0 14px;}
  input{width:100%;box-sizing:border-box;background:#0d1117;border:1px solid #2d333b;border-radius:8px;
        color:#e6edf3;padding:12px 14px;font-size:16px;outline:none;margin-bottom:12px;}
  input:focus{border-color:#4c8dff;}
  button{width:100%;min-height:44px;background:#4c8dff;border:none;border-radius:8px;
         color:#fff;font-size:14px;font-weight:600;cursor:pointer;}
</style></head><body>
<form onsubmit="location='/?token='+encodeURIComponent(t.value.trim());return false">
  <h1>kmcp · session console</h1>
  <p>This bind requires a token (printed on the console's stdout, or
     <code>$CSD_CONSOLE_TOKEN</code>). It sets a 7-day cookie.</p>
  <input id="t" type="password" placeholder="paste token…" autocomplete="off" autofocus>
  <button type="submit">Sign in</button>
</form></body></html>"""

KMCP_RE = re.compile(r"^mcp__.+__(?P<base>[a-z_]+)$")
READ_TOOLS = {"get_entry", "get_section", "get_entries"}
SURFACE_TOOLS = {"search", "hybrid_search", "traverse_graph", "list_entries",
                 "list_by_tag", "list_children", "get_relationships",
                 "query_view", "list_by_importance"}
# Writes: the tools that MUTATE the base. Seeded from the angles W extractor's
# own classification (`angles._WRITE_TOOLS`) rather than a third hand-kept list,
# so `csd angles` and the console can never drift on what counts as a kmcp
# write; the console adds the staging/rating tools the headline miner does not
# headline.
WRITE_TOOLS = set(_ANGLE_WRITE_TOOLS) | {
    "rate_entry", "stage_template", "upload_file", "delete_staged",
    "create_application", "update_application", "add_taxonomy_node",
}
# base tool -> the operation chip the Context tab shows. A `dry_run` overrides
# this to "dry-run" at render time: a validation pass that was never followed by
# a real write must not read as a write.
WRITE_OPS = {
    "create_entry": "created", "import_entries": "created",
    "import_lessons": "created", "stage_template": "staged",
    "update_entry": "updated", "update_application": "updated",
    "create_application": "created", "patch_content": "patched",
    "create_relationship": "related", "delete_relationship": "unrelated",
    "rename_entry": "renamed", "move_entry": "moved",
    "delete_entry": "deleted", "delete_staged": "deleted",
    "add_entry_tag": "tagged", "add_taxonomy_node": "tagged",
    "rate_entry": "rated", "upload_file": "uploaded",
}

SKIP_USER_PREFIXES = ("<bash-", "<task-notification>", "<command-", "<local-command")

# Angle headlines are mined out-of-band by `csd angles-watch`; the console only
# reads them. Order mirrors the CLI's rail: judged angles first, then evidence.
ANGLES_DIR = Path(os.environ.get(
    "CSD_STATE_DIR", str(Path.home() / ".local" / "state" / "claude-session-db")
)) / "angles"
ANGLE_ORDER = ["direction", "events", "agents", "files", "kmcp", "commands",
               "git", "errors", "knowledge", "metrics"]


def _angles_store(sid: str):
    """The miner's persisted store for one session, or None."""
    f = ANGLES_DIR / f"{sid}.json"
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return None


def angle_rail(sid: str):
    """Headlines for the session's latest mined turn, grouped and ordered."""
    store = _angles_store(sid)
    if not store:
        return None
    groups = {}
    for iid, item in (store.get("items") or {}).items():
        groups.setdefault(item.get("angle", "?"), []).append(
            {"id": iid, "headline": item.get("headline", "")})
    for items in groups.values():
        items.sort(key=lambda i: i["id"])
    ordered = [{"angle": a, "items": groups[a]}
               for a in ANGLE_ORDER if a in groups]
    ordered += [{"angle": a, "items": v} for a, v in sorted(groups.items())
                if a not in ANGLE_ORDER]
    mined_at = None
    try:
        mined_at = (ANGLES_DIR / f"{sid}.json").stat().st_mtime
    except OSError:
        pass
    return {
        "turn_span": store.get("turn_span"),
        "user_text": (store.get("user_text") or "")[:200],
        "generated_at": store.get("generated_at"),
        "mined_age_s": round(time.time() - mined_at) if mined_at else None,
        "angles": ordered,
    }


def angle_detail(sid: str, item_id: str):
    store = _angles_store(sid)
    if not store:
        return None
    return (store.get("items") or {}).get(item_id.upper())


def angle_catalog(sid: str):
    """The minable-angle registry (for the mine menu), straight from
    ANGLE_SPECS so a new angle appears without touching the console. When a
    session is named, each row carries how many items its current store holds
    for that angle (0 = not mined / nothing found for the latest turn)."""
    store = _angles_store(sid) if sid else None
    counts: dict[str, int] = {}
    for item in ((store or {}).get("items") or {}).values():
        a = item.get("angle")
        counts[a] = counts.get(a, 0) + 1
    return {
        "generated_at": (store or {}).get("generated_at"),
        "angles": [{"id": key, "prefix": prefix, "kind": kind,
                    "label": ANGLE_LABELS.get(key, key),
                    "mined": counts.get(key, 0)}
                   for key, (prefix, kind) in ANGLE_SPECS.items()],
    }


# ----------------------------------------------------------------------------
# run registry — what the console spawned, and can therefore stop
#
# Claude Code opens a transcript, appends, and closes; no process holds it open,
# and an interactive `claude` carries no session id in argv. So a session that
# was started in a terminal CANNOT be mapped to a pid, and Stop cannot reach it.
# `claude -p --resume` never attaches to that process either — it spawns a NEW
# one that appends to the same file (which is why /api/answer has a two-writer
# guard). We can only stop what we started. The UI says so rather than guessing.
# ----------------------------------------------------------------------------
RUNS: dict[str, list] = {}          # session_id -> [Popen, ...]
_RUNS_LOCK = threading.Lock()


def _register(sid: str, proc):
    if not sid:
        return
    with _RUNS_LOCK:
        RUNS.setdefault(sid, []).append(proc)


def _live_procs(sid: str):
    with _RUNS_LOCK:
        procs = [p for p in RUNS.get(sid, []) if p.poll() is None]
        if procs:
            RUNS[sid] = procs
        else:
            RUNS.pop(sid, None)
        return list(procs)


def stoppable(sid: str) -> bool:
    return bool(_live_procs(sid))


def stop_session(sid: str) -> dict:
    """SIGINT the process group (Esc's signal), escalating if it won't die."""
    procs = _live_procs(sid)
    if not procs:
        return {"ok": False, "error": "no console-spawned run for this session; "
                                      "a session started in a terminal cannot be "
                                      "stopped from here"}
    killed = []
    for p in procs:
        for sig, wait in ((signal.SIGINT, 2.0), (signal.SIGTERM, 2.0),
                          (signal.SIGKILL, 1.0)):
            if p.poll() is not None:
                break
            try:
                os.killpg(os.getpgid(p.pid), sig)   # start_new_session=True
            except (ProcessLookupError, PermissionError):
                break
            deadline = time.time() + wait
            while time.time() < deadline and p.poll() is None:
                time.sleep(0.05)
        killed.append({"pid": p.pid, "rc": p.poll()})
    _live_procs(sid)
    return {"ok": True, "stopped": killed}


# ----------------------------------------------------------------------------
# archive — hide from the sidebar, never touch the transcript
#
# Archiving is an index entry in the console's own state, NOT a mutation of
# ~/.claude/projects. The JSONL is never moved, renamed, or deleted; an
# archived session is fully retrievable by id and reappears the moment it is
# unarchived. Nothing here is destructive.
# ----------------------------------------------------------------------------
CONSOLE_STATE = ANGLES_DIR.parent / "console"
ARCHIVE_FILE = CONSOLE_STATE / "archived.json"
_ARCHIVE_LOCK = threading.Lock()


def _read_archive() -> dict:
    try:
        return json.loads(ARCHIVE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def set_archived(sid: str, archived: bool, reason: str = "") -> dict:
    with _ARCHIVE_LOCK:
        idx = _read_archive()
        if archived:
            idx[sid] = {"archived_at": datetime.now(timezone.utc).isoformat(),
                        "reason": reason}
        else:
            idx.pop(sid, None)
        CONSOLE_STATE.mkdir(parents=True, exist_ok=True)
        tmp = ARCHIVE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(idx, indent=1))
        tmp.replace(ARCHIVE_FILE)          # atomic; never a half-written index
    return {"ok": True, "session_id": sid, "archived": archived}


# ----------------------------------------------------------------------------
# per-session overlay — title / priority / topic / subtopic (console state only)
#
# ONE JSON keyed by session id in the console state dir, exactly like the
# archive index: never a mutation of ~/.claude/projects. Every field is
# operator-set metadata, NOT derived from the transcript — a human title that
# overrides the derived nav label, a triage priority, and a two-level
# topic → subtopic taxonomy that groups sessions INDEPENDENT of their cwd/folder
# (folder still shows on the row, but does NOT define the grouping). Clearing a
# field drops it; an entry with no fields left is removed entirely. Atomic
# replace — never a half-written index.
#
# The topic/subtopic *values* are a reusable managed list (topics.json) so the
# UI offers autocomplete from what already exists — anti-drift, so "ControlTech"
# and "controltech" don't fragment into two groups.
#
# Legacy note: priority used to live in its own priority.json (and this branch's
# earlier titles.json). _migrate_legacy_overlays() seeds meta.json from them once
# and leaves the old files untouched — nothing is ever destroyed.
# ----------------------------------------------------------------------------
PRIORITIES = ("low", "med", "high", "critical")
META_FILE = CONSOLE_STATE / "meta.json"
TOPICS_FILE = CONSOLE_STATE / "topics.json"
PRIORITY_FILE = CONSOLE_STATE / "priority.json"     # legacy, migrated once
TITLES_FILE = CONSOLE_STATE / "titles.json"         # legacy, migrated once
_META_LOCK = threading.Lock()
_TOPICS_LOCK = threading.Lock()
# summary_of / summary_child: the two ends of the off-session summary link —
# the child run's overlay names the session it digests, the parent's names the
# latest run that digested it. Durable (meta.json), unlike SUMMARIZING.
META_FIELDS = ("title", "priority", "topic", "subtopic", "tp_dismissed",
               "summary_of", "summary_child")
MAX_TITLE_LEN = 200
MAX_TOPIC_LEN = 80


def _atomic_write_json(path: Path, obj) -> None:
    CONSOLE_STATE.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    tmp.replace(path)                       # atomic; never a half-written index


def _read_meta_overlay() -> dict:
    try:
        d = json.loads(META_FILE.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _meta_of(idx: dict, sid: str) -> dict:
    v = idx.get(sid)
    return v if isinstance(v, dict) else {}


def _update_meta(sid: str, **fields) -> dict:
    """Merge fields into a session's overlay entry (a falsy value clears a
    field). An entry with no meaningful metadata left is dropped so the file
    stays tidy. Other fields on the same session are always preserved."""
    with _META_LOCK:
        idx = _read_meta_overlay()
        cur = dict(_meta_of(idx, sid))
        for k, v in fields.items():
            if v:
                cur[k] = v
            else:
                cur.pop(k, None)
        kept = {k: cur[k] for k in META_FIELDS if cur.get(k)}
        if kept:
            kept["set_at"] = datetime.now(timezone.utc).isoformat()
            idx[sid] = kept
        else:
            idx.pop(sid, None)
        _atomic_write_json(META_FILE, idx)
        return idx.get(sid) or {}


def _priority_of(idx: dict, sid: str):
    """priority for a sid out of an already-read overlay index."""
    return _meta_of(idx, sid).get("priority")


def set_title(sid: str, title) -> dict:
    title = (title or "").strip()[:MAX_TITLE_LEN]
    _update_meta(sid, title=title or None)
    return {"ok": True, "session_id": sid, "title": title or None}


def dismiss_title_proposal(sid: str, proposal) -> dict:
    """Remember a dismissed tldr title proposal so it stops being offered.

    Stored by VALUE (not a flag): a later tldr run that proposes a different
    title is a new suggestion and surfaces again. Overlay-only, like every
    other meta field."""
    proposal = (proposal or "").strip()[:MAX_TITLE_LEN]
    if not proposal:
        return {"ok": False, "error": "proposal required"}
    _update_meta(sid, tp_dismissed=proposal)
    return {"ok": True, "session_id": sid, "dismissed": proposal}


def _pending_proposal(tl, user_title, dismissed):
    """The tldr's proposed title for a session, or None when there is nothing
    to offer: no proposal, the operator dismissed exactly this proposal, or
    the effective title already IS the proposal (accepted). A differing
    MANUAL title never suppresses the suggestion — it is surfaced beside it,
    and only an explicit accept ever writes a title."""
    prop = ((tl or {}).get("title_proposal") or "").strip()
    if not prop:
        return None
    if dismissed and dismissed.strip() == prop:
        return None
    if user_title and user_title.strip().lower() == prop.lower():
        return None
    return prop


def set_priority(sid: str, priority) -> dict:
    _update_meta(sid, priority=priority or None)
    return {"ok": True, "session_id": sid, "priority": priority or None}


def set_topic(sid: str, topic, subtopic) -> dict:
    """Assign (or clear) a session's topic/subtopic, and remember the values in
    the managed list so they're reusable next time. No topic means no subtopic."""
    topic = (topic or "").strip()[:MAX_TOPIC_LEN]
    subtopic = (subtopic or "").strip()[:MAX_TOPIC_LEN]
    if not topic:
        subtopic = ""
    _update_meta(sid, topic=topic or None, subtopic=subtopic or None)
    if topic:
        _remember_topic(topic, subtopic)
    return {"ok": True, "session_id": sid,
            "topic": topic or None, "subtopic": subtopic or None}


# ---- managed topic → subtopics list (reusable across sessions) --------------
def _read_topics() -> dict:
    try:
        d = json.loads(TOPICS_FILE.read_text())
        return {k: v for k, v in d.items() if isinstance(v, list)} \
            if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _remember_topic(topic: str, subtopic: str = "") -> None:
    """Add topic (and subtopic, if any) to the managed list — the anti-drift
    memory, so next time the operator picks from a list instead of retyping."""
    with _TOPICS_LOCK:
        d = _read_topics()
        subs = d.get(topic) or []
        if subtopic and subtopic not in subs:
            subs.append(subtopic)
        d[topic] = sorted(subs, key=str.lower)
        _atomic_write_json(TOPICS_FILE, d)


def managed_topics() -> dict:
    """topic → sorted subtopics, unioning the managed list (topics.json) with
    what is actually assigned across sessions — self-healing if topics.json
    ever lags behind the overlay."""
    d = {k: list(v) for k, v in _read_topics().items()}
    for _sid, m in _read_meta_overlay().items():
        if not isinstance(m, dict) or not m.get("topic"):
            continue
        subs = d.setdefault(m["topic"], [])
        st = m.get("subtopic")
        if st and st not in subs:
            subs.append(st)
    return {t: sorted(subs, key=str.lower) for t, subs in d.items()}


def _migrate_legacy_overlays() -> None:
    """One-time seed of meta.json from the pre-unification priority.json /
    titles.json indexes. Read-only over the legacy files — they are left in
    place, never deleted; nothing is destroyed."""
    if META_FILE.exists():
        return
    seed: dict = {}
    for path, field in ((PRIORITY_FILE, "priority"), (TITLES_FILE, "title")):
        try:
            legacy = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(legacy, dict):
            continue
        for sid, v in legacy.items():
            val = v.get(field) if isinstance(v, dict) else v
            if val:
                seed.setdefault(sid, {})[field] = val
    if not seed:
        return
    for _sid, m in seed.items():
        m["set_at"] = datetime.now(timezone.utc).isoformat()
    with _META_LOCK:
        if not META_FILE.exists():
            _atomic_write_json(META_FILE, seed)


# ----------------------------------------------------------------------------
# transcript reading
# ----------------------------------------------------------------------------
def _parse_lines(raw: str, dropped_partial: bool):
    lines = raw.split("\n")
    if dropped_partial:
        lines = lines[1:]
    out = []
    for ln in lines:
        if not ln.strip():
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def tail_records(path: Path, nbytes: int):
    size = path.stat().st_size
    with open(path, "rb") as f:
        f.seek(max(0, size - nbytes))
        raw = f.read().decode("utf-8", errors="replace")
    return _parse_lines(raw, size > nbytes)


def all_records(path: Path):
    size = path.stat().st_size
    if size > FULL_MAX_BYTES:
        return tail_records(path, FULL_MAX_BYTES), True
    with open(path, "rb") as f:
        raw = f.read().decode("utf-8", errors="replace")
    return _parse_lines(raw, False), False


# ----------------------------------------------------------------------------
# content-block helpers
# ----------------------------------------------------------------------------
def _text_of(content):
    """Concatenated text of a message/tool_result content (str or block list)."""
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if isinstance(b, dict):
            if b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif "text" in b and isinstance(b["text"], str):
                parts.append(b["text"])
        elif isinstance(b, str):
            parts.append(b)
    return "".join(parts)


def _result_map(records):
    """tool_use_id -> {chars, text?} from tool_result blocks. Result text is
    kept only for small payloads (searches are ~7KB) to bound memory — reads
    only need the char count, which is why big results drop their text."""
    out = {}
    for r in records:
        msg = r.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tid = b.get("tool_use_id")
                if not tid:
                    continue
                txt = _text_of(b.get("content"))
                rec = {"chars": len(txt), "is_error": bool(b.get("is_error"))}
                if len(txt) <= 65536:
                    rec["text"] = txt
                out[tid] = rec
    return out


def tool_result_payload(sid: str, tid: str):
    """(payload, code) for GET /api/tool_result — one tool_result's text.

    Fetched lazily when a tool row is expanded, so the polled session payload
    never carries result bodies. The text is the transcript's, verbatim (no
    truncation — the archive's invariant applies to the surface too); a result
    whose content is absent from the transcript reports that rather than an
    empty string, so "empty output" and "not recorded" stay distinguishable.
    """
    path = find_session(sid)
    if path is None:
        return {"error": "session not found"}, 404
    if not tid:
        return {"error": "tid required"}, 400
    records, _ = all_records(path)
    for r in records:
        content = (r.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if (isinstance(b, dict) and b.get("type") == "tool_result"
                    and b.get("tool_use_id") == tid):
                txt = _text_of(b.get("content"))
                return {"tid": tid, "text": txt, "chars": len(txt),
                        "is_error": bool(b.get("is_error")),
                        "recorded": b.get("content") is not None}, 200
    return {"tid": tid, "text": None, "chars": 0, "recorded": False,
            "error": "no tool_result for this tool_use yet"}, 404


def pending_tool_ids(records, rmap=None):
    """tool_use ids that have NO matching tool_result yet — the single source of
    truth for "in flight". Both the activity-state classifier (does the last
    assistant have an unresolved tool_use → agent still working) and the
    live-command-status render (WHICH specific commands are still running) call
    this, so the two can never drift on what "pending" means.

    `_result_map` keeps the id key even for >64KB results (it drops only the
    text), so membership here is size-safe — a huge tool_result never makes its
    tool_use look pending.
    """
    rmap = rmap if rmap is not None else _result_map(records)
    pend = set()
    for r in records:
        if r.get("type") != "assistant":
            continue
        for b in (r.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                tid = b.get("id")
                if tid and tid not in rmap:
                    pend.add(tid)
    return pend


def _tool_summary(name, inp):
    """(label, detail) — the one salient field that makes a tool_use readable.

    The console makes kmcp reads/searches first-class; every other tool used to
    collapse to a bare name chip, which for a Bash/Skill/Agent-heavy session is
    unreadable. label is the short verb shown inline; detail is the peek.
    """
    inp = inp or {}
    short = name.rsplit("__", 1)[-1] if name.startswith("mcp__") else name
    # label carries NO sigil — the client renders an aligned glyph column.
    if name == "Bash":
        cmd = (inp.get("command") or "").strip()
        first = cmd.split("\n", 1)[0]
        return (first + (" …" if "\n" in cmd else ""),
                inp.get("description") or "")
    if name in ("Write", "Edit", "NotebookEdit", "Read"):
        return (name + " " + (inp.get("file_path") or ""), "")
    if name == "Skill":
        return ("Skill " + (inp.get("skill") or "?"),
                str(inp.get("args") or "")[:200])
    if name in ("Agent", "Task"):
        sub = inp.get("subagent_type") or "agent"
        bg = " (bg)" if str(inp.get("run_in_background")).lower() == "true" else ""
        return (f"Agent[{sub}]{bg} " + (inp.get("description") or ""),
                str(inp.get("prompt") or "")[:400])
    if name == "SendMessage":
        return (inp.get("summary") or inp.get("to") or "",
                str(inp.get("message") or inp.get("content") or "")[:400])
    if name == "ToolSearch":
        return (str(inp.get("query") or ""), "")
    if name in ("TodoWrite",):
        return (name, "")
    # generic MCP write / unknown tool — show a compact input peek
    peek = ", ".join(f"{k}={str(v)[:40]}" for k, v in list(inp.items())[:3])
    return (short, peek)


SEARCH_HIT_CAP = 40          # per search; the Context tab lists every hit kept

# Result containers across the SURFACE_TOOLS: search/hybrid_search return
# `results`, list_* return `entries`/`items`, traverse_graph returns `nodes`,
# get_relationships returns `relationships`. A bare list is also accepted.
_HIT_KEYS = ("results", "entries", "hits", "nodes", "items", "matches",
             "relationships", "children", "paths")


def _hit_ref(e):
    """One surfaced ref out of a result element, or None.

    Shapes differ per tool (a relationship names its far end `target_path`, a
    graph node may carry only `path`), so every alias is tried and a shape we
    cannot read yields None instead of raising — a lens never crashes on a
    result payload it has not seen before.
    """
    if isinstance(e, str):
        return {"app": None, "path": e, "title": None, "score": None,
                "etype": _etype_hint(e)}
    if not isinstance(e, dict):
        return None
    path = (e.get("path") or e.get("target_path") or e.get("to_path")
            or e.get("entry_path") or e.get("source_path"))
    if not path:
        return None
    app = (e.get("application") or e.get("app") or e.get("target_application")
           or e.get("source_application"))
    return {"app": app, "path": path,
            "title": e.get("title") or e.get("name"),
            "score": e.get("score") or e.get("similarity") or e.get("weight"),
            "etype": (e.get("entity_type") or e.get("type")
                      or _etype_hint(path))}


# The compact TEXT rendering kmcp returns at detail=minimal:
#
#   <query> · 1935 hits · app=controltech_code
#   types: 845 event · 447 task · …
#
#     process  process/agent-delivery                     Agent Delivery Loop…
#     lesson   knowledge_mcp_code:lesson/git/squash-…     Squash-merged branc…
#
#     +1930 more — …
#
# It is NOT JSON, so every such search used to surface zero refs. The path
# column carries `app:path` when the search spanned applications and a bare
# path when it was app-filtered (the header's `app=` then names the app).
_TXT_HEAD_RE = re.compile(r"·\s*([\d,]+)\s+hits")
_TXT_APP_RE = re.compile(r"\bapp=([\w.\-]+)")
_TXT_TYPES_RE = re.compile(r"^types:\s*(.+)$", re.M)
_TXT_HIT_RE = re.compile(r"^ {2,}([a-z_]+)\s{2,}(\S+)(?:\s{2,}(.*?))?\s*$", re.M)


def _parse_search_text(text):
    """Surfaced refs out of the compact TEXT search rendering, or None."""
    head = text.split("\n", 1)[0]
    m = _TXT_HEAD_RE.search(head)
    if not m:
        return None
    am = _TXT_APP_RE.search(head)
    app = am.group(1) if am else None
    counts = {}
    tm = _TXT_TYPES_RE.search(text)
    if tm:
        for n, t in re.findall(r"(\d+)\s+([\w\-]+)", tm.group(1)):
            counts[t] = int(n)
    hits = []
    for etype, ref, title in _TXT_HIT_RE.findall(text):
        if ref.startswith("+"):        # the "+N more" footer
            continue
        a, _, p = ref.partition(":") if ":" in ref else ("", "", ref)
        hits.append({"app": a or app, "path": p, "score": None,
                     "title": (title or "").strip() or None,
                     "etype": etype or _etype_hint(p)})
    return {"total": int(m.group(1).replace(",", "")), "type_counts": counts,
            "returned": len(hits), "shown": len(hits[:SEARCH_HIT_CAP]),
            "hits": hits[:SEARCH_HIT_CAP]}


def _parse_search_result(text):
    """Pull (total, type_counts, hits) out of a search tool_result — the
    surfacing telemetry: what the base OFFERED the session for this query.

    `shown` says how many of the returned hits survived SEARCH_HIT_CAP, so the
    Context tab can say "12 of 60" rather than silently under-reporting.
    """
    if not text:
        return None
    try:
        d = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        try:
            return _parse_search_text(text)
        except Exception:            # a lens never dies on a result payload
            return None
    if isinstance(d, list):
        d = {"results": d}
    if not isinstance(d, dict):
        return None
    res = None
    for k in _HIT_KEYS:
        v = d.get(k)
        if isinstance(v, list):
            res = v
            break
    res = res or []
    hits = [h for h in (_hit_ref(e) for e in res[:SEARCH_HIT_CAP]) if h]
    return {"total": d.get("total"), "type_counts": d.get("type_counts"),
            "returned": len(res), "shown": len(hits), "hits": hits}


def _parse_choice_questions(inp):
    """questions[] (question/header/multi/options) out of an AskUserQuestion input."""
    qs = []
    for q in (inp or {}).get("questions") or []:
        if not isinstance(q, dict):
            continue
        opts = [{"label": o.get("label"), "description": o.get("description")}
                for o in (q.get("options") or []) if isinstance(o, dict)]
        qs.append({"question": q.get("question"), "header": q.get("header"),
                   "multi": bool(q.get("multiSelect")), "options": opts})
    return qs


def _parse_choice_answer(text):
    """question -> selected label(s), parsed from the AskUserQuestion tool_result
    (shape: ...answered: "<question>"="<label>" selected preview: ...). Returns {}
    when the choice is still pending (no tool_result yet)."""
    if not text:
        return {}
    return {q: a for q, a in re.findall(r'"([^"]*)"="([^"]*)"', text)}


def _read_meta(base, inp):
    """(mode, sections) for a kmcp read tool_use input."""
    inp = inp or {}
    if base == "get_section":
        s = inp.get("sections") or ([inp["section"]] if inp.get("section") else None)
        return "section", s
    if base == "get_entries":
        return "batch", None
    if inp.get("summary"):
        return "summary", None
    secs = inp.get("sections")
    if secs:
        return "sections", secs
    return "full", None


def _import_docs(inp):
    """Entry refs declared by an import_entries / import_lessons input.

    The document travels as `content` — a YAML or JSON string, possibly
    multi-document — or as a structured `entries`/`documents` list. Every form
    is parsed best-effort: an unreadable payload yields NO refs rather than an
    exception, so the write still gets a row identified by its tool.
    """
    inp = inp or {}
    docs = []
    # `content` is the documented carrier, but callers also pass `entries` /
    # `documents` — and pass them as a JSON *string* as often as a list.
    for key in ("content", "entries", "documents", "lessons"):
        raw = inp.get(key)
        if isinstance(raw, dict):
            docs.append(raw)
            continue
        if isinstance(raw, list):
            docs += [d for d in raw if isinstance(d, dict)]
            continue
        if not isinstance(raw, str):
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            obj = None
        if isinstance(obj, dict):
            docs.append(obj)
        elif isinstance(obj, list):
            docs += [d for d in obj if isinstance(d, dict)]
        else:
            # YAML (the documented form): scrape only the scalars a row needs.
            # No YAML parser is imported for this — a lens must not fail
            # because a document body confuses a full loader.
            app = re.search(r'^\s*-?\s*"?application"?\s*:\s*"?([\w.\-]+)',
                            raw, re.M)
            et = dict(re.findall(
                r'^\s*-?\s*"?(path|entity_type)"?\s*:\s*"?([^"\'\n,}]+)',
                raw, re.M))
            for m in re.finditer(r'^\s*-?\s*"?path"?\s*:\s*"?([^"\'\n,}]+)',
                                 raw, re.M):
                docs.append({"application": app.group(1) if app else None,
                             "path": m.group(1).strip(),
                             "entity_type": et.get("entity_type")})
    out, seen = [], set()
    for d in docs:
        p = d.get("path")
        if not isinstance(p, str) or not p.strip():
            continue
        p = p.strip()
        app = d.get("application")
        if (app, p) in seen:
            continue
        seen.add((app, p))
        out.append({"app": app, "path": p,
                    "etype": d.get("entity_type") or _etype_hint(p),
                    "title": d.get("title")})
    return out


def _parse_write_result(text):
    """created/updated/skipped/errors out of an import-shaped tool_result.

    This is the authority on whether a ref was CREATED or UPDATED — the input
    document cannot know, and a dry_run's `would_create`/`would_update` say the
    same thing about a write that has not happened yet.
    """
    if not text:
        return None
    try:
        d = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    if not any(k in d for k in ("created", "updated", "skipped", "errors",
                                "summary", "dry_run", "error")):
        return None

    def refs(key):
        # Keys collide across tools: an update_entry result carries
        # `updated: true` (a bool), not a list of refs. Anything that is not a
        # list contributes nothing rather than raising.
        v = d.get(key)
        out = []
        for e in (v if isinstance(v, list) else []):
            if isinstance(e, str):
                out.append({"path": e, "app": None, "etype": None,
                            "updated": key == "updated"})
            elif isinstance(e, dict) and e.get("path"):
                out.append({"path": e["path"],
                            "app": e.get("application"),
                            "etype": e.get("entity_type"),
                            "updated": bool(e.get("updated")
                                            or e.get("would_update")
                                            or key == "updated")})
        return out

    errs = []
    _e = d.get("errors")
    for e in (_e if isinstance(_e, list) else ([_e] if isinstance(_e, str) else [])):
        errs.append(e if isinstance(e, str) else json.dumps(e, default=str)[:300])
    # A REFUSED write ("Missing input", "Import path not allowed") comes back
    # as {error, message} with is_error unset — the tool answered, it just did
    # not write. Without this it read as a successful create.
    if d.get("error"):
        errs.append(" — ".join(str(d[k]) for k in ("error", "message")
                               if d.get(k))[:300])
    return {"created": refs("created"), "updated": refs("updated"),
            "skipped": refs("skipped"), "errors": errs,
            "dry_run": bool(d.get("dry_run")), "summary": d.get("summary")}


def _write_meta(base, inp, wres):
    """(op, refs, note) for one kmcp write tool_use.

    `refs` is the list of entries the call aims at — [{app, path, etype, op}] —
    so a multi-document import lands as several rows while a patch lands as one.
    Every branch degrades: a call with no readable path still returns one ref
    with `path=None`, which renders as a row rather than vanishing.
    """
    inp = inp or {}
    op = WRITE_OPS.get(base, "wrote")
    note = None
    refs = []

    if base in ("import_entries", "import_lessons"):
        declared = _import_docs(inp)
        by_path = {d["path"]: d for d in declared}
        landed = []
        if wres:
            for r in wres["created"] + wres["updated"]:
                d = by_path.get(r["path"], {})
                landed.append({"app": r.get("app") or d.get("app"),
                               "path": r["path"],
                               "etype": r.get("etype") or d.get("etype"),
                               "op": "updated" if r.get("updated") else "created"})
        seen = {r["path"] for r in landed}
        for d in declared:
            if d["path"] not in seen:
                landed.append({**{k: d[k] for k in ("app", "path", "etype")},
                               "op": op})
        # A staged-file import declares no refs at all — the row identifies as
        # the file, and `path` stays None so it never links into /browse as if
        # a filesystem path were an entry path.
        refs = landed or [{"app": None, "path": None, "etype": None, "op": op}]
        if inp.get("file_path"):
            note = f"file: {inp['file_path']}"
        if wres and wres["skipped"]:
            note = f"{len(wres['skipped'])} skipped" + (f" · {note}" if note else "")
    elif base == "create_relationship":
        src_app = (inp.get("source_application") or inp.get("application")
                   or inp.get("source_app") or inp.get("from_application"))
        tgt_app = (inp.get("target_application") or inp.get("target_app")
                   or inp.get("to_application") or src_app)
        refs = [{"app": src_app, "path": inp.get("source_path"),
                 "etype": _etype_hint(inp.get("source_path")), "op": op}]
        if inp.get("target_path"):
            note = (f"{inp.get('relationship_type') or 'related'} → "
                    f"{tgt_app or '?'}:{inp['target_path']}")
    elif base in ("rename_entry", "move_entry"):
        old_app = inp.get("application") or inp.get("old_application")
        refs = [{"app": old_app, "path": inp.get("old_path"),
                 "etype": inp.get("entity_type") or _etype_hint(inp.get("old_path")),
                 "op": op}]
        new_app = inp.get("new_application") or old_app
        if inp.get("new_path"):
            note = f"→ {new_app or '?'}:{inp['new_path']}"
    else:
        path = inp.get("path") or inp.get("entry_path")
        refs = [{"app": inp.get("application"), "path": path,
                 "etype": inp.get("entity_type") or _etype_hint(path),
                 "op": op}]
        if base == "patch_content":
            secs = [p.get("section") or p.get("path")
                    for p in (inp.get("patches") or []) if isinstance(p, dict)]
            secs = [s for s in secs if s] or (
                [inp["section"]] if inp.get("section") else [])
            verb = inp.get("operation") or inp.get("op")
            note = (inp.get("change_summary")
                    or ((verb + " " if verb else "") + ", ".join(secs)).strip()
                    or None)
        elif base == "update_entry":
            secs = list((inp.get("content") or {}).keys()) \
                if isinstance(inp.get("content"), dict) else []
            note = inp.get("change_summary") or (", ".join(secs) or None)
        elif base == "add_entry_tag":
            note = inp.get("tag")
        elif base == "rate_entry":
            note = str(inp.get("rating")) if inp.get("rating") is not None else None
        elif base == "upload_file":
            note = inp.get("filename") or inp.get("file_path")
    return op, refs, note


_CLI_CALL_RE = re.compile(r"knowledge-cli\s+call\s+([a-z_]+)")
# A value may be quoted (`--query "two words"`) or bare (`--path a/b`).
_CLI_ARG_RE = re.compile(
    r"""--(application|path|query)(?:=|\s+)(?:"([^"]*)"|'([^']*)'|(\S+))""")


def _cli_json_payload(tail):
    """First parseable {...} JSON object in a command tail, or None.

    The shim's documented form is a JSON positional argument
    (`knowledge-cli call get_entry '{"application":…,"path":…}'`), which the
    --flag regex never sees — those calls used to surface as "?:(batch)".
    Brace-matching (not a quote regex) so nested objects like get_entries'
    `entries` array parse whole.
    """
    i = tail.find("{")
    while i != -1:
        depth = 0
        for j in range(i, len(tail)):
            ch = tail[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(tail[i:j + 1])
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        pass
                    break
        i = tail.find("{", i + 1)
    return None


def _bash_kmcp(inp):
    """(base, input-like) when a Bash command is the knowledge-cli shim.

    The CLI fallback reaches the same tools as the MCP surface, so a session
    that used it loaded just as much context — it must not vanish from the rail
    merely because it took the shim.
    """
    cmd = (inp or {}).get("command") or ""
    m = _CLI_CALL_RE.search(cmd)
    if not m:
        return None
    base = m.group(1)
    if base not in READ_TOOLS and base not in SURFACE_TOOLS \
            and base not in WRITE_TOOLS:
        return None
    # JSON positional argument (the documented form) carries the same shape as
    # the MCP input — application/path/query/entries all come through intact.
    payload = _cli_json_payload(cmd[m.end():])
    if payload is not None:
        return base, payload
    args = {k: (dq or sq or bare)
            for k, dq, sq, bare in _CLI_ARG_RE.findall(cmd)}
    shim = {"application": args.get("application"), "path": args.get("path")}
    if args.get("query"):
        shim["query"] = args["query"]
    if re.search(r"--dry[-_]run\b", cmd):
        shim["dry_run"] = True
    return base, shim


def _etype_hint(path):
    """Best-effort entity type from the path's leading segment."""
    if not path:
        return None
    head = path.split("/", 1)[0]
    known = {"session", "design", "task", "lesson", "event", "process",
             "overview", "diagram", "agent", "personality", "command",
             "development", "project", "knowledge", "skill"}
    return head if head in known else None


# ----------------------------------------------------------------------------
# session summary (nav) + full event stream (detail)
# ----------------------------------------------------------------------------
def _is_real_user_turn(r, text):
    if r.get("isMeta"):
        return False
    if not text:
        return False
    t = text.lstrip()
    if t.startswith(SKIP_USER_PREFIXES):
        return False
    if t.startswith("<") and "system-reminder" in t[:80]:
        return False
    return True


# Activity-state ceilings. All code-computed from transcript shape + file mtime
# (no LLM, no DB — "truth from the ledger"); every ambiguous/failure case
# degrades to a NON-alarming state, never a stuck "working".
FRESH_S = 15            # ≤ this since last write → the client may add a pulse
WORK_CEIL_S = 240      # working with no write past this = likely killed → stale
WAIT_COLD_S = 900     # waiting quietly past this → idle (not actionable)


def _state(records, mtime_age, stoppable=False, agents_live=0):
    """(state, sub_working) — 4-value activity classification:

      working — agent generating, or a tool in flight
      waiting — agent ended its turn cleanly; the human's move
      idle    — open thread, quiet a long time
      stale   — claims in-flight but the file is frozen past the ceiling
                (likely a killed process), or a long-dead waiting thread

    Overrides (strongest evidence wins): a console-spawned live run forces
    working; a live subagent on an otherwise waiting/idle session counts as
    working and sets sub_working so the UI can annotate it distinctly.
    """
    rmap = _result_map(records)
    pend = pending_tool_ids(records, rmap)
    last = None
    for r in records:
        if r.get("type") in ("user", "assistant") and not r.get("isSidechain"):
            msg = r.get("message") or {}
            c = msg.get("content")
            # a user record that is only a tool_result is not a conversational turn
            if r["type"] == "user":
                txt = _text_of(c)
                if not _is_real_user_turn(r, txt):
                    continue
            last = r
    if last is None:
        base = "idle"
    elif last["type"] == "user":
        base = "working"                # prompt in, no reply yet → queued
    else:
        stop = (last.get("message") or {}).get("stop_reason")
        terminal = stop in ("end_turn", "stop_sequence")
        # unresolved tool_use on the LAST assistant record = a tool in flight
        last_pending = any(
            isinstance(b, dict) and b.get("type") == "tool_use"
            and b.get("id") in pend
            for b in (last.get("message") or {}).get("content") or [])
        base = "waiting" if (terminal and not last_pending) else "working"

    if base == "working":
        state = "working" if mtime_age <= WORK_CEIL_S else "stale"
    elif base == "waiting":
        state = "waiting" if mtime_age <= WAIT_COLD_S else "idle"
    else:
        state = base

    sub_working = False
    if stoppable:                       # a run we spawned is provably live
        state = "working"
    elif state in ("waiting", "idle") and agents_live > 0:
        state, sub_working = "working", True
    return state, sub_working


# ----------------------------------------------------------------------------
# project identity for the nav
#
# A session's project label comes from its cwd when the transcript has one.
# Two derivation bugs the sidebar used to leak:
#   - no cwd -> the RAW encoded projects dir name ("-Users-andrew-Projects-
#     controltech") stood as a project;
#   - a git worktree cwd (<repo>/.claude/worktrees/<wt>) stood as its own
#     project, peer to the repo it belongs to.
# So: prettify the encoded dir to its leaf, and fold worktrees into their
# parent repo (label = repo, worktree carried separately for the row tag).
# ----------------------------------------------------------------------------
_WORKTREE_RE = re.compile(r"([^/]+)/\.(?:claude|git)/worktrees/([^/]+)")
_PARENT_DIRS = {"projects", "github", "downloads", "documents", "desktop",
                "developer", "code", "src", "repos", "work"}


def _pretty_project(dirname: str) -> str:
    """Best-effort leaf name out of an encoded projects dir ('/'->'-')."""
    parts = dirname.strip("-").split("-")
    low = [p.lower() for p in parts]
    if low[:1] == ["users"] and len(parts) > 2:      # -Users-<user>-…
        parts, low = parts[2:], low[2:]
    while low and low[0] in _PARENT_DIRS:
        parts, low = parts[1:], low[1:]
    return "-".join(parts).lower() or dirname


def _project_identity(cwd, dirname: str):
    """(label, worktree): repo-level label, worktree leaf when cwd is one."""
    if cwd:
        c = str(cwd).rstrip("/")
        m = _WORKTREE_RE.search(c)
        if m:
            return m.group(1), m.group(2)
        return (c.split("/")[-1] or dirname), None
    return _pretty_project(dirname), None


# Whole-file facts for the nav (first timestamp, message-record count) are
# re-derived only when the transcript changes: keyed by (mtime_ns, size).
_NAV_STATS: dict[str, tuple] = {}


def _nav_stats(path: Path):
    """{started_at, msg_count} scanned from the full file, signature-cached.

    msg_count is a byte-level count of user/assistant records (tool-result
    user records included) — a nav-grade magnitude, not an event-stream count.
    """
    try:
        st = path.stat()
    except OSError:
        return {"started_at": None, "msg_count": None}
    sig = (st.st_mtime_ns, st.st_size)
    hit = _NAV_STATS.get(str(path))
    if hit and hit[0] == sig:
        return hit[1]
    try:
        data = path.read_bytes()
    except OSError:
        return {"started_at": None, "msg_count": None}
    msg_count = (data.count(b'"type":"user"') + data.count(b'"type": "user"')
                 + data.count(b'"type":"assistant"')
                 + data.count(b'"type": "assistant"'))
    started = None
    for ln in data.split(b"\n"):
        if b'"timestamp"' not in ln:
            continue
        try:
            r = json.loads(ln)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(r, dict) and r.get("timestamp"):
            started = r["timestamp"]
            break
    stats = {"started_at": started, "msg_count": msg_count}
    _NAV_STATS[str(path)] = (sig, stats)
    return stats


def summarize_nav(path: Path):
    recs = tail_records(path, NAV_TAIL_BYTES)
    if not recs:
        return None
    title = cwd = branch = None
    last_user = None
    usage = None
    last_ts = None
    for r in recs:
        t = r.get("type")
        if t == "ai-title":
            title = r.get("aiTitle") or title
        elif t == "custom-title":
            title = r.get("customTitle") or title
        elif t in ("user", "assistant"):
            cwd = r.get("cwd") or cwd
            branch = r.get("gitBranch") or branch
            # TRUE last activity = the last message record's own timestamp
            # (records are chronological, so the last one wins) — NOT file
            # mtime, which only ever lies toward "more recent".
            if r.get("timestamp"):
                last_ts = r["timestamp"]
            if t == "user" and not r.get("isSidechain"):
                txt = _text_of((r.get("message") or {}).get("content"))
                if _is_real_user_turn(r, txt):
                    last_user = txt
            elif t == "assistant":
                u = (r.get("message") or {}).get("usage")
                if isinstance(u, dict):
                    usage = u
    mtime_age = max(0, time.time() - path.stat().st_mtime)   # guard clock skew
    # title_src: "set" = a real title record (ai-title/custom-title);
    # "prompt"/"id" = raw fallbacks — those rows may show the tldr's proposed
    # title as a ghost placeholder until it's accepted or dismissed.
    title_src = "set"
    if not title:
        title_src = "prompt" if last_user else "id"
        title = (last_user[:70] + "…") if last_user else path.stem[:12]
    label, worktree = _project_identity(cwd, str(path.parent.name))
    ctx_tokens = None
    if usage:
        ctx_tokens = (usage.get("input_tokens", 0)
                      + usage.get("cache_read_input_tokens", 0)
                      + usage.get("cache_creation_input_tokens", 0))
    stats = _nav_stats(path)
    # Activity-state overrides need stoppable + live-subagent count, both cheap
    # and computed here (discover_sessions reuses these, doesn't recompute).
    stop = stoppable(path.stem)
    agents = _agents_glance(path)
    state, sub_working = _state(recs, mtime_age, stop,
                                agents["live"] if agents else 0)
    return {
        "session_id": path.stem,
        "project": str(path.parent.name),
        "project_label": label,
        "worktree": worktree,
        "cwd": cwd, "branch": branch, "title": title.strip(),
        "title_src": title_src,
        "state": state,
        "sub_working": sub_working,
        "stoppable": stop,
        "agents": agents,
        "mtime": path.stat().st_mtime,
        "mtime_age_s": round(mtime_age),
        "last_ts": last_ts,
        "started_at": stats["started_at"],
        "msg_count": stats["msg_count"],
        "ctx_tokens": ctx_tokens,
    }


def _nav_row(p: Path, idx: dict, meta: dict):
    """One enriched nav row for a transcript path (or None if unreadable)."""
    try:
        s = summarize_nav(p)
    except OSError:
        return None
    if not s:
        return None
    m = _meta_of(meta, p.stem)
    s["archived"] = p.stem in idx
    # stoppable + agents already computed in summarize_nav (state override)
    s["priority"] = m.get("priority")
    s["user_title"] = m.get("title")
    s["topic"] = m.get("topic")
    s["subtopic"] = m.get("subtopic")
    s["summary_of"] = m.get("summary_of")
    s["summary_child"] = m.get("summary_child")
    # Cached-or-nothing; stale rows queue an async regeneration.
    s["tldr"] = tldr.payload(p.stem, p)
    # Per-row digest presence for the sidebar glance: does a
    # timeline exist, and is it current? Signature-memoized in
    # session_timeline — the poll never re-reads unchanged stores.
    s["timeline"] = session_timeline.presence(p.stem, p)
    # Pending title suggestion (None once accepted/dismissed).
    s["title_proposal"] = _pending_proposal(
        s["tldr"], m.get("title"), m.get("tp_dismissed"))
    return s


def discover_sessions(archived=False):
    """Nav list. archived=False hides archived sessions; True shows only them.

    An archived session is filtered from this list, never from disk — it is
    still served by /api/session and returns the moment it is unarchived.
    """
    idx = _read_archive()
    cutoff = time.time() - MAX_AGE_H * 3600
    cands = []
    for p in PROJECTS.glob("*/*.jsonl"):
        if "subagents" in p.parts:
            continue
        if (p.stem in idx) != archived:
            continue
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        # Archived sessions ignore the age cutoff — retrieval is the point.
        if m >= cutoff or archived:
            cands.append((m, p))
    cands.sort(reverse=True)
    meta = _read_meta_overlay()
    out = []
    for _, p in cands[:MAX_NAV_SESSIONS]:
        s = _nav_row(p, idx, meta)
        if s:
            out.append(s)
    return out


# An id-shaped sidebar query (a full uuid, or a prefix — the 8-hex short id the
# UI itself displays) must reach PAST the loaded nav rows: the nav list is
# capped (MAX_NAV_SESSIONS) and cut off at MAX_AGE_H, and archived sessions are
# filtered out of the recent tab. "Retrievable by id" is the archive's promise,
# so the lookup goes straight to disk — the same glob find_session uses.
ID_QUERY_RE = re.compile(r"^[0-9a-f][0-9a-f-]*$")
MAX_ID_HITS = 5


def lookup_sessions_by_id(q: str):
    """Nav rows for main sessions whose uuid contains `q` (id-shaped queries
    only). Read-only, bounded, and never raises — a miss is an empty list."""
    q = (q or "").strip().lower()
    if len(q.replace("-", "")) < 8 or not ID_QUERY_RE.match(q):
        return []
    hits = []
    for p in PROJECTS.glob(f"*/*{q}*.jsonl"):
        if "subagents" in p.parts:
            continue
        try:
            hits.append((p.stat().st_mtime, p))
        except OSError:
            continue
    hits.sort(reverse=True)
    idx, meta = _read_archive(), _read_meta_overlay()
    out = []
    for _, p in hits[:MAX_ID_HITS]:
        s = _nav_row(p, idx, meta)
        if s:
            out.append(s)
    return out


def find_session(sid: str):
    """Main-session uuid -> <proj>/<uuid>.jsonl; child key '<parent>:<agent>'
    -> the subagents/**/agent-<id>.jsonl sidechain file (same address the
    archive's is_subagent rows and v_agent_children use)."""
    if ":" in sid:
        parent, aid = sid.split(":", 1)
        return next(PROJECTS.glob(f"*/{parent}/subagents/**/agent-{aid}.jsonl"),
                    None)
    return next(PROJECTS.glob(f"*/{sid}.jsonl"), None)


# ----------------------------------------------------------------------------
# subagent navigation — Agent chip -> child focus view, spawn-anchor back-link
#
# The wiring mirrors the archive's spawn ledger (v_agent_children): the harness
# writes a toolUseResult carrier (agentId/agentType/status) on the user record
# that carries the Agent tool_result. Joining tool_use_id -> carrier maps each
# Agent chip to its child session key '<parent>:<agentId>'; the carrier's
# sourceToolAssistantUuid/parentUuid is the spawn anchor for the back-link.
# ----------------------------------------------------------------------------
def _agent_result_map(records):
    """tool_use_id -> {agent_id, agent_type, status} from record-level
    toolUseResult carriers (the harness's own record of each Agent spawn)."""
    out = {}
    for rec in records:
        tur = rec.get("toolUseResult")
        if rec.get("type") != "user" or not isinstance(tur, dict) \
                or not tur.get("agentId"):
            continue
        for b in (rec.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_result" \
                    and b.get("tool_use_id"):
                out[b["tool_use_id"]] = {
                    "agent_id": tur.get("agentId"),
                    "agent_type": tur.get("agentType", ""),
                    "status": tur.get("status", ""),
                }
    return out


def _spawn_anchor(parent_records, agent_id):
    """uuid of the parent message to jump to for a child's back-link: the
    assistant message carrying the Agent tool_use, via the result carrier."""
    for rec in parent_records:
        tur = rec.get("toolUseResult")
        if rec.get("type") == "user" and isinstance(tur, dict) \
                and tur.get("agentId") == agent_id:
            return rec.get("sourceToolAssistantUuid") or rec.get("parentUuid")
    return None


AGENT_LIVE_S = 300   # sidechain mtime within this = agent still running


def _agents_glance(path: Path):
    """Cheap per-session subagent census for the nav list: {total, live} from
    the session's subagents/ dir (live = sidechain written recently)."""
    d = path.parent / path.stem / "subagents"
    if not d.is_dir():
        return None
    now = time.time()
    total = live = 0
    for f in d.glob("**/agent-*.jsonl"):
        total += 1
        try:
            if now - f.stat().st_mtime < AGENT_LIVE_S:
                live += 1
        except OSError:
            pass
    return {"total": total, "live": live} if total else None


# ----------------------------------------------------------------------------
# CLAUDE.md memory files — the always-loaded context Claude Code injects
#
# Read-only resolution, confined by construction to $HOME + the session's own
# directory chain: the global ~/.claude/CLAUDE.md plus a CLAUDE.md per ancestor
# directory of the session cwd (outermost first — the order Claude Code applies
# them; a worktree cwd therefore surfaces both the repo's and the worktree's
# file). /api/claudemd never accepts a path from the caller — it re-resolves
# this list from the session id and indexes into it.
# ----------------------------------------------------------------------------
CLAUDEMD_MAX_BYTES = 512 * 1024


def _claudemd_files(cwd):
    """[{scope, path, chars}] for the CLAUDE.md files a session at `cwd` loads.

    Missing files are omitted (never an error row); duplicates (symlinks,
    cwd == repo root) collapse on the resolved path.
    """
    home = Path.home()
    out, seen = [], set()

    def add(scope, p: Path):
        try:
            rp = p.resolve()
            if rp in seen or not rp.is_file():
                return
            seen.add(rp)
            out.append({"scope": scope, "path": str(p),
                        "chars": rp.stat().st_size})
        except OSError:
            pass

    add("global", home / ".claude" / "CLAUDE.md")
    if cwd:
        try:
            c = Path(cwd).resolve()
        except OSError:
            c = None
        if c is not None:
            for d in (*reversed(c.parents), c):
                if d != home and d.is_relative_to(home):
                    add("project", d / "CLAUDE.md")
    return out


# ----------------------------------------------------------------------------
# Deployed / served URLs — what this session "has up"
#
# Pure-regex/structural extraction (doctrine: extraction is code, models only
# judge). URLs are anchored to deploy/serve-shaped TOOL EVENTS — an Artifact
# publish result, a `gh pr create` output, a deploy CLI's output, a server
# launch command — never to prose, docs, or search results, so a URL merely
# *mentioned* in conversation can't reach the rail (precision over recall).
# Read-only over the already-parsed records; nothing is resumed or written.
# ----------------------------------------------------------------------------
ARTIFACT_URL_RE = re.compile(r"https://claude\.ai/[^\s\"'<>|)\]]+")
PR_CMD_RE = re.compile(r"\bgh\s+pr\s+create\b")
PR_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
# Deploy CLIs in command position (start of a line/pipeline segment), so
# `curl https://x.vercel.app` or a URL in prose never counts as a deploy.
DEPLOY_CMD_RE = re.compile(
    r"(?:^|[|&;(]\s*|\s)(?:npx\s+|nohup\s+)?"
    r"(?:wrangler\s+(?:pages\s+)?deploy|vercel\b|netlify\s+deploy"
    r"|fly(?:ctl)?\s+deploy|firebase\s+deploy|surge\s)", re.M)
DEPLOY_HOST_RE = re.compile(
    r"https?://[\w.-]+\.(?:workers\.dev|pages\.dev|vercel\.app|netlify\.app"
    r"|fly\.dev|onrender\.com|web\.app|surge\.sh)(?:/[^\s\"'<>|)\]]*)?")
LOCAL_URL_RE = re.compile(
    r"https?://(?:127\.0\.0\.1|localhost|0\.0\.0\.0)(?::\d{2,5})?(?:/|\b)")
# (launcher regex, default port or None, short name). A launcher with no
# explicit --port and no default and no URL in its output yields nothing.
SERVE_LAUNCHERS = [
    (re.compile(r"\bcsd\s+console\b"), 4462, "csd console"),
    (re.compile(r"-m\s+claude_session_db\.console\.server\b"), 4462,
     "console.server"),
    (re.compile(r"\bserver\.serve\(|\bserve\(host"), 4462, "server.serve"),
    (re.compile(r"-m\s+http\.server\b"), 8000, "http.server"),
    (re.compile(r"\buvicorn\b"), 8000, "uvicorn"),
    (re.compile(r"\bgunicorn\b"), 8000, "gunicorn"),
    (re.compile(r"\bflask\b[^\n|;&]*\brun\b"), 5000, "flask run"),
    (re.compile(r"\b(?:npx\s+)?vite\b(?!\s+build)"), 5173, "vite"),
    (re.compile(r"\bnext\s+dev\b"), 3000, "next dev"),
    (re.compile(r"\bnpm\s+run\s+(?:dev|serve)\b"
                r"|\b(?:yarn|pnpm|bun)\s+(?:run\s+)?dev\b"), None, "npm dev"),
    (re.compile(r"\bnpx\s+(?:serve|http-server)\b|\bhttp-server\b"), None,
     "http-server"),
    (re.compile(r"\bphp\s+-S\b"), None, "php -S"),
]
SERVE_PORT_RE = re.compile(r"(?:--port[= ]\s*|\bport\s*=\s*|\s-p\s+)(\d{2,5})\b")
HTTP_SERVER_PORT_RE = re.compile(r"-m\s+http\.server\s+(\d{2,5})\b")
PHP_S_PORT_RE = re.compile(r"-S\s+[\w.]*:(\d{2,5})")
# Lines that only inspect/kill/search never *launch* — a launcher token inside
# them (grep pattern, pkill -f "port=...", a comment) must not mint a URL.
NON_LAUNCH_LINE_RE = re.compile(
    r"^\s*(?:#|(?:sudo\s+)?(?:pkill|kill|killall|grep|rg|ag|lsof|ps|cat|sed"
    r"|awk|curl|wget|echo)\b)")
DEPLOYED_PROBE_MAX = 8          # cap per-payload liveness probes
DEPLOYED_PROBE_TIMEOUT = 0.1    # seconds; loopback connect only


def _probe_local(port: int) -> bool:
    """~100ms TCP connect to loopback ONLY — never a remote host."""
    try:
        with socket.create_connection(("127.0.0.1", port),
                                      timeout=DEPLOYED_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def _deployed(records, rmap):
    """[{url, kind, label, ts, source, up?}] — what this session has up.

    kind ∈ artifact | deploy | local | pr. Dedup by URL, LAST occurrence's
    timestamp wins. `local` rows carry `up` from a loopback-only connect probe
    (dead servers render dimmed, not hidden). Sorted artifact → deploy →
    local → pr (PRs are secondary to actually-served things), newest first
    within a kind.
    """
    found = {}   # url -> row (dict preserves order; re-put moves to the end)

    def put(url, kind, label, ts, source):
        url = url.rstrip(".,;:")
        found.pop(url, None)
        found[url] = {"url": url, "kind": kind, "label": label, "ts": ts,
                      "source": " ".join((source or "").split())[:160]}

    for r in records:
        if r.get("type") != "assistant":
            continue
        ts = r.get("timestamp")
        content = (r.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                continue
            name, inp = b.get("name", ""), b.get("input") or {}
            res = rmap.get(b.get("id")) or {}
            out_text = res.get("text") or ""

            if name == "Artifact":
                # Result reads "Published <file> at https://claude.ai/…".
                m = ARTIFACT_URL_RE.search(out_text)
                if m and not res.get("is_error"):
                    label = (inp.get("title")
                             or Path(inp.get("file_path") or "").name
                             or "artifact")
                    put(m.group(0), "artifact", label, ts, "Artifact publish")
                continue
            if name != "Bash":
                continue
            cmd = inp.get("command") or ""

            if PR_CMD_RE.search(cmd):
                m = PR_URL_RE.search(out_text)
                if m:
                    put(m.group(0), "pr", "PR #" + m.group(0).rsplit("/", 1)[1],
                        ts, "gh pr create")

            if DEPLOY_CMD_RE.search(cmd):
                # The URL must come from the deploy's OWN output.
                for m in DEPLOY_HOST_RE.finditer(out_text):
                    u = m.group(0)
                    host = (urlparse(u).hostname or u)
                    put(u, "deploy", host, ts, cmd)

            if res.get("is_error"):
                continue    # a refused/failed launch never served anything
            heredoc_end = None   # prose in heredocs (commit msgs, PR bodies)
            for line in cmd.splitlines():
                if heredoc_end is not None:
                    if line.strip() == heredoc_end:
                        heredoc_end = None
                    continue
                hm = re.search(r"<<[-~]?\s*['\"]?(\w+)", line)
                if hm:
                    heredoc_end = hm.group(1)
                served = False
                # per pipeline segment, so the launcher, its port, and the
                # displayed source all come from the same simple command
                for seg in re.split(r"&&|\|\||;|\|", line):
                    seg = seg.strip()
                    if not seg or NON_LAUNCH_LINE_RE.match(seg):
                        continue
                    hit = next(((rx, dflt, nm)
                                for rx, dflt, nm in SERVE_LAUNCHERS
                                if rx.search(seg)), None)
                    if hit is None:
                        continue
                    rx, dflt, nm = hit
                    # `csd console` in backticks is prose ABOUT the command,
                    # not a launch of it
                    ms = rx.search(seg)
                    if ms and seg[max(0, ms.start()-1):ms.start()] == "`":
                        continue
                    pm = (HTTP_SERVER_PORT_RE.search(seg)
                          or PHP_S_PORT_RE.search(seg)
                          or SERVE_PORT_RE.search(seg))
                    port = int(pm.group(1)) if pm else None
                    if port is None:
                        mu = LOCAL_URL_RE.search(out_text)
                        if mu:
                            port = urlparse(mu.group(0).rstrip("/")).port or 80
                    if port is None:
                        port = dflt
                    if port:
                        put(f"http://127.0.0.1:{port}/", "local",
                            f"127.0.0.1:{port} · {nm}", ts, seg)
                        served = True
                        break
                if served:
                    break   # one served URL per command is enough

    rows = list(found.values())
    probed = 0
    for row in rows:
        if row["kind"] != "local" or probed >= DEPLOYED_PROBE_MAX:
            continue
        pu = urlparse(row["url"])
        if pu.hostname == "127.0.0.1" and pu.port:   # loopback by construction
            row["up"] = _probe_local(pu.port)
            probed += 1
    order = {"artifact": 0, "deploy": 1, "local": 2, "pr": 3}
    # ISO-8601 timestamps sort lexicographically: newest first within a kind.
    rows.sort(key=lambda x: x["ts"] or "", reverse=True)
    rows.sort(key=lambda x: order.get(x["kind"], 9))
    return rows


def _session_cwd(path: Path):
    """cwd from the transcript tail — the same derivation /api/git uses."""
    cwd = None
    for r in tail_records(path, NAV_TAIL_BYTES):
        if r.get("type") in ("user", "assistant") and r.get("cwd"):
            cwd = r["cwd"]
    return cwd


def claudemd_payload(sid: str, n: int):
    """(payload, code) for GET /api/claudemd — one memory file, read-only.

    `n` indexes the server-side re-resolved list for this session; the caller
    can never name a path, so the endpoint can only serve the global CLAUDE.md
    or one inside the session's own directory chain.
    """
    path = find_session(sid)
    if path is None:
        return {"error": "session not found"}, 404
    files = _claudemd_files(_session_cwd(path))
    if not 0 <= n < len(files):
        return {"error": "no such memory file"}, 404
    f = files[n]
    try:
        with open(f["path"], "rb") as fh:
            data = fh.read(CLAUDEMD_MAX_BYTES + 1)
    except OSError as e:
        return {"error": str(e)[:200]}, 500
    return {**f,
            "content": data[:CLAUDEMD_MAX_BYTES].decode("utf-8", "replace"),
            "truncated": len(data) > CLAUDEMD_MAX_BYTES}, 200


def build_session(sid: str):
    path = find_session(sid)
    if path is None:
        return None
    records, truncated = all_records(path)
    is_child = ":" in sid
    if is_child:
        # Every record in a sidechain file is sidechain; lift the flag so the
        # main-chain rendering path (state, turns, tools) applies unchanged.
        for r in records:
            r.pop("isSidechain", None)
    rmap = _result_map(records)
    pend_ids = pending_tool_ids(records, rmap)   # shared "in flight" primitive
    agent_spawns = _agent_result_map(records)
    base_sid = sid.split(":", 1)[0]

    events = []
    cwd = branch = model = None
    usage = None
    title = None
    n_reads = n_searches = n_writes = 0

    for r in records:
        t = r.get("type")
        if t == "ai-title":
            title = r.get("aiTitle") or title
            continue
        if t == "custom-title":
            title = r.get("customTitle") or title
            continue
        if t not in ("user", "assistant"):
            continue

        sub = bool(r.get("isSidechain"))
        cwd = r.get("cwd") or cwd
        branch = r.get("gitBranch") or branch
        msg = r.get("message") or {}
        ts = r.get("timestamp")
        uid = r.get("uuid")
        content = msg.get("content")

        if t == "assistant":
            model = msg.get("model") or model
            if isinstance(msg.get("usage"), dict):
                usage = msg["usage"]
            text_parts, other_tools = [], []
            blocks = content if isinstance(content, list) else (
                [{"type": "text", "text": content}] if content else [])
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    text_parts.append(b.get("text", ""))
                elif bt == "tool_use":
                    name = b.get("name", "")
                    m = KMCP_RE.match(name)
                    base = m.group("base") if m else None
                    inp = b.get("input") or {}
                    tid = b.get("id")
                    via = "mcp"
                    if base is None and name == "Bash":
                        shim = _bash_kmcp(inp)
                        if shim:
                            base, inp, via = shim[0], shim[1], "cli"
                    if base in READ_TOOLS:
                        n_reads += 1
                        mode, secs = _read_meta(base, inp)
                        path_ = inp.get("path")
                        app_ = inp.get("application")
                        # A batch get_entries carries no top-level app/path — the
                        # refs live in `entries`. Surface them so the row reads
                        # as its targets, not a bare "(batch)".
                        targets = None
                        if base == "get_entries":
                            targets = [
                                f"{it.get('application','?')}:{it.get('path','?')}"
                                for it in (inp.get("entries") or inp.get("paths") or [])
                                if isinstance(it, dict)
                            ]
                        events.append({
                            "kind": "read", "ts": ts, "uuid": uid, "sub": sub,
                            "tid": tid,        # CR mode addresses the block
                            "tool": base, "app": app_, "path": path_,
                            "mode": mode, "sections": secs, "via": via,
                            "etype": inp.get("entity_type") or _etype_hint(path_),
                            "chars": (rmap.get(tid) or {}).get("chars"),
                            "count": len(targets) if targets is not None else None,
                            "targets": targets,
                        })
                    elif base in SURFACE_TOOLS:
                        n_searches += 1
                        events.append({
                            "kind": "search", "ts": ts, "uuid": uid, "sub": sub,
                            "tid": tid, "tool": base, "via": via,
                            "query": (inp.get("query") or inp.get("path")
                                      or inp.get("application") or ""),
                            "app": inp.get("application"),
                            "chars": (rmap.get(tid) or {}).get("chars"),
                            "result": _parse_search_result(
                                (rmap.get(tid) or {}).get("text")),
                        })
                    elif base in WRITE_TOOLS:
                        # Writes used to fall through every branch and be
                        # DROPPED: `base` is set (so the generic-tool branch,
                        # gated on `base is None`, never saw them) but no
                        # branch claimed them. The Context tab's written list
                        # is the first thing to render them.
                        n_writes += 1
                        res = rmap.get(tid) or {}
                        wres = _parse_write_result(res.get("text"))
                        op, wrefs, note = _write_meta(base, inp, wres)
                        dry = bool(inp.get("dry_run")) or bool(
                            (wres or {}).get("dry_run"))
                        err = bool(res.get("is_error")) or bool(
                            (wres or {}).get("errors"))
                        events.append({
                            "kind": "write", "ts": ts, "uuid": uid, "sub": sub,
                            "tid": tid, "tool": base, "via": via,
                            "op": op, "dry_run": dry,
                            "app": wrefs[0]["app"] if wrefs else None,
                            "path": wrefs[0]["path"] if wrefs else None,
                            "etype": wrefs[0]["etype"] if wrefs else None,
                            "refs": wrefs, "note": note,
                            "chars": res.get("chars"),
                            "is_error": err,
                            "error": (("; ".join((wres or {}).get("errors") or [])
                                       or (res.get("text") or "")[:300]
                                       or "the tool returned an error")
                                      if err else None),
                            "result": wres,
                            "pending": tid in pend_ids,
                        })
                    elif name == "AskUserQuestion":
                        qs = _parse_choice_questions(inp)
                        ansmap = _parse_choice_answer(
                            (rmap.get(tid) or {}).get("text"))
                        for q in qs:
                            q["selected"] = ansmap.get(q["question"])
                        events.append({"kind": "choice", "ts": ts, "uuid": uid,
                                       "sub": sub, "questions": qs})
                    elif base is None and name:
                        label, detail = _tool_summary(name, inp)
                        res = rmap.get(tid) or {}
                        row = {
                            "name": name, "label": label, "detail": detail,
                            "id": tid,
                            "chars": res.get("chars"),
                            "is_error": res.get("is_error", False),
                            # A tool_use with no tool_result yet is still in
                            # flight — the client shows "running…" for such a row
                            # on a LIVE session. Same primitive the activity-state
                            # classifier uses (pending_tool_ids), so they can't
                            # drift. Verbatim command text (never truncated) rides
                            # along so the row expands to the full command.
                            "pending": tid in pend_ids,
                        }
                        if name == "Bash":
                            row["cmd"] = inp.get("command") or ""
                        spawn = agent_spawns.get(tid)
                        if name in ("Agent", "Task") and spawn \
                                and spawn.get("agent_id"):
                            # The chip becomes a link to the child focus view.
                            row["child"] = f"{base_sid}:{spawn['agent_id']}"
                            row["status"] = spawn.get("status", "")
                        other_tools.append(row)
            text = "\n".join(tp for tp in text_parts if tp).strip()
            if text:
                events.append({"kind": "assistant", "ts": ts, "uuid": uid,
                               "sub": sub, "text": text, "tools": other_tools})
            elif other_tools:
                events.append({"kind": "tool", "ts": ts, "uuid": uid,
                               "sub": sub, "tools": other_tools})
        else:  # user
            if sub:
                continue
            txt = _text_of(content)
            if not _is_real_user_turn(r, txt):
                continue
            events.append({"kind": "user", "ts": ts, "uuid": uid, "text": txt})

    mtime_age = max(0, time.time() - path.stat().st_mtime)   # guard clock skew
    if not title:
        first_user = next((e["text"] for e in events if e["kind"] == "user"), None)
        title = (first_user[:70] + "…") if first_user else sid[:12]

    ctx_tokens = None
    if usage:
        ctx_tokens = (usage.get("input_tokens", 0)
                      + usage.get("cache_read_input_tokens", 0)
                      + usage.get("cache_creation_input_tokens", 0))

    _m = _meta_of(_read_meta_overlay(), sid)
    _stop = stoppable(sid)
    _agents = _agents_glance(path)
    _state_v, _sub_working = _state(records, mtime_age, _stop,
                                    _agents["live"] if _agents else 0)
    out = {
        "session_id": sid,
        "project": str(path.parent.name),
        "cwd": cwd, "branch": branch, "title": title.strip(),
        "model": model, "ctx_tokens": ctx_tokens,
        "state": _state_v,
        "sub_working": _sub_working,
        "mtime_age_s": round(mtime_age),
        "truncated": truncated,
        "counts": {"reads": n_reads, "searches": n_searches,
                   "writes": n_writes, "events": len(events)},
        "claudemd": _claudemd_files(cwd),
        # deployed/served URLs mined from deploy/serve-shaped tool events
        # (Artifact publishes, deploy CLI output, server launches, PRs) —
        # never from prose. Local rows carry a loopback liveness `up` flag.
        "deployed": _deployed(records, rmap),
        "events": events,
        "rail": angle_rail(sid),
        "tldr": tldr.payload(sid, path),
        "archived": sid in _read_archive(),
        "stoppable": _stop,
        # queued-not-yet-dispatched composer messages (per-session FIFO); the
        # client renders them at the stream tail as pending "you" turns.
        "queue": queue_for(sid),
        # off-session summary status (in-memory SUMMARIZING) surfaced in the
        # DETAIL pane too — not just the nav — so its running→done/failed
        # transition is visible on the session the operator is watching. Child
        # sids never summarize, so this is None for them.
        "summarizing": SUMMARIZING.get(sid),
        # the off-session summary link, both directions: on the PARENT the
        # latest child run (durable, meta.json) plus the live run record; on
        # the CHILD the session it is digesting — so the chip is a link into
        # the run and the run's header links back.
        "summary_child": _m.get("summary_child"),
        "summary_run": SUMMARY_RUNS.get(sid),
        "summary_of": _m.get("summary_of"),
        # prior-capture facts (watermark date, next pass, prior entry ref) so
        # the Summarize buttons can say NEW work since <date>. DB-only and
        # TTL-cached — cheap enough to ride this poll; None when nothing was
        # ever captured or the archive is unreachable.
        "summary_scope": None if is_child else summary_scope(sid),
        "priority": _m.get("priority"),
        "user_title": _m.get("title"),
        "topic": _m.get("topic"),
        "subtopic": _m.get("subtopic"),
    }
    out["title_proposal"] = _pending_proposal(
        out["tldr"], _m.get("title"), _m.get("tp_dismissed"))
    if is_child:
        from ..subagent import read_agent_meta
        parent, aid = sid.split(":", 1)
        meta = read_agent_meta(path)
        anchor = None
        ppath = find_session(parent)
        if ppath is not None:
            try:
                anchor = _spawn_anchor(all_records(ppath)[0], aid)
            except OSError:
                pass
        out["subagent"] = {
            "parent_session_id": parent,
            "agent_id": aid,
            "agent_type": meta.get("agentType", ""),
            "description": meta.get("description", ""),
            "spawn_depth": meta.get("spawnDepth"),
            "anchor_uuid": anchor,
        }
    return out


# ----------------------------------------------------------------------------
# side-session permission envelope — spawn_claude as resolver/translator
#
# Pilot of claude_session_db:design/task-driven-side-sessions. Every side-session
# the console spawns used to inherit whatever ambient settings its `cwd` resolved
# to, passing not one scope or permission flag. A summarize spawned with a
# git-worktree cwd therefore could not read ~/.claude/projects — where the
# transcript it is digesting lives — so it failed on the filesystem guard and
# then flailed through fallbacks, each blocked for the same reason.
#
# The fix is NOT a per-worktree settings.local.json (reactive, drifts, does not
# travel) and NOT a Python dict of flags (drifts identically, just in here). It
# is a VERSIONED kmcp skill entry under this app's steward. Same doctrine as the
# angles miner: the envelope is DECLARED DATA, this code only TRANSLATES it.
#
#   skill.harness_hints.required_tools     -> --allowedTools (comma-joined)
#   skill.harness_hints.fs_read + fs_write -> --add-dir
#   agent.constraints.max_turns            -> --max-turns
#   skill.harness_hints.model              -> --model (pin; else ambient default)
#   agent.guardrails + the resolved paths  -> --append-system-prompt
#
# The last line is not decoration: the off-session summary skill locates its
# transcript with a compound, command-substituted shell command, which a headless
# run can never get approved. Handing the ALREADY-RESOLVED path down (the console
# knows it — find_session) removes the need for that command entirely. Filesystem
# scope alone would not have fixed the pilot.
#
# DOCTRINE — why this cannot break the console: resolve_envelope() NEVER raises
# and never blocks a spawn. kmcp unreachable, skill missing, entry malformed —
# every failure degrades to ZERO flags, which is byte-for-byte what every spawn
# did before this existed, with the reason surfaced to the caller instead of
# swallowed. There is deliberately NO hardcoded fallback envelope: duplicating a
# declared scope in Python is exactly the drift this design displaces, and a
# silent fallback would mask a broken resolver by making it look like it worked.
# Least privilege only — the bypass permission mode is never emitted.
# ----------------------------------------------------------------------------
ACTION_SKILLS = {"summarize": "claude_session_db:skill/console-summarize"}
ENVELOPE_TTL_S = 300
_ENV_CACHE: dict = {}                 # skill ref -> (fetched_at, envelope)
_ENV_LOCK = threading.Lock()


def _fetch_envelope(ref: str) -> dict:
    """Load a skill entry + its bound agent from kmcp. Raises on failure."""
    app, _, path = ref.partition(":")
    skill = _kmcp_call("get_entry", {"application": app, "path": path,
                                     "entity_type": "skill",
                                     "include_relationships": False})
    content = (skill or {}).get("content") or {}
    if not content:
        raise KmcpError(f"{ref}: {skill.get('error') or 'no content'}")
    env = {"skill": ref, "agent_ref": content.get("assigned_agent"),
           "hints": content.get("harness_hints") or {},
           "guardrails": [], "constraints": {}}
    if env["agent_ref"]:
        aapp, _, apath = str(env["agent_ref"]).partition(":")
        try:
            agent = _kmcp_call("get_entry", {"application": aapp, "path": apath,
                                             "entity_type": "agent",
                                             "include_relationships": False})
            ac = (agent or {}).get("content") or {}
            env["guardrails"] = ac.get("guardrails") or []
            env["constraints"] = ac.get("constraints") or {}
        except KmcpError:
            pass          # the skill's own scope is still worth applying
    return env


def _envelope(ref: str) -> dict:
    now = time.time()
    with _ENV_LOCK:
        hit = _ENV_CACHE.get(ref)
        if hit and now - hit[0] < ENVELOPE_TTL_S:
            return hit[1]
    env = _fetch_envelope(ref)        # outside the lock: it shells out
    with _ENV_LOCK:
        _ENV_CACHE[ref] = (now, env)
    return env


def _digest_script() -> str:
    """Absolute path to session_digest.py — handed to the child so a delta pass
    never has to locate (or guess) the script the skill shells out to."""
    try:
        from .. import session_digest
        return str(Path(session_digest.__file__).resolve())
    except Exception:  # noqa: BLE001 — the prompt must never raise
        return str(Path(__file__).resolve().parent.parent / "session_digest.py")


def _envelope_prompt(env: dict, ctx: dict):
    """The --append-system-prompt body: what the child must not have to discover."""
    out = []
    sid, tpath = ctx.get("session_id"), ctx.get("transcript")
    if sid:
        out.append(f"Target session UUID: {sid}")
    if tpath:
        out.append(
            f"That session's transcript is ALREADY RESOLVED at: {tpath}\n"
            "Use this path directly. Do not search the filesystem for it, and do "
            "not run a piped, chained or command-substituted shell command to "
            "locate it — this is a headless run with no human to approve one.")
    # Repeatable-pass framing. A session captured once is summarized AGAIN only
    # over the tail its prior pass never saw — and the child cannot work that
    # window out for itself: the watermark lives in the archive, which it has no
    # scope for. So the window, the exact digest command and the entry it
    # continues travel down here, the same way the transcript path does.
    since = ctx.get("since")
    if since:
        cmd = (f'python3 {_digest_script()} "{tpath}" --since "{since}"'
               if tpath else f'session_digest.py <transcript> --since "{since}"')
        out.append(
            f"CONTINUATION PASS {ctx.get('pass') or 2}. This session has ALREADY "
            f"been summarized up to {since} — that earlier capture is not yours "
            "to repeat. Digest ONLY the tail after that watermark:\n"
            f"  {cmd}\n"
            "Pass --since exactly as given (the digest prints the delta span it "
            "actually covers, and says so if the window is empty). Everything "
            "you write — events, lessons, tasks and the session entry — must "
            "come from that tail alone. Do not re-create entries for work "
            "before the watermark.")
        prior = ctx.get("prior")
        out.append(
            f"The pass you are continuing is {prior}. The new session entry must "
            "carry it in linked_entries and file a see_also edge to it, so the "
            "passes read as one thread." if prior else
            "The prior pass's entry could not be resolved — write the "
            "continuation standalone and say so in its summary.")
    guardrails = [g for g in (env.get("guardrails") or []) if isinstance(g, str)]
    if guardrails:
        out.append(f"Hard rules for this run (from {env.get('agent_ref')}):")
        out += [f"- {g}" for g in guardrails]
    return "\n".join(out) if out else None


def resolve_envelope(action, ctx=None):
    """(flags, note) — CLI tokens to PREPEND for this action, and why.

    NEVER raises. An unresolved envelope returns ([], reason): the spawn then
    behaves exactly as it did before this resolver existed, and the reason
    travels back to the operator instead of being swallowed.
    """
    if not action:
        return [], None
    ref = ACTION_SKILLS.get(action)
    if not ref:
        return [], f"no skill bound to action {action!r}"
    try:
        env = _envelope(ref)
    except Exception as exc:            # noqa: BLE001 — degrade, never block
        return [], (f"envelope unresolved ({type(exc).__name__}: {exc}); "
                    "spawned with ambient permissions")

    hints = env.get("hints") or {}
    flags, applied = [], []

    tools = [t for t in (hints.get("required_tools") or []) if isinstance(t, str)]
    if tools:
        # Comma-joined as ONE argument on purpose: --allowedTools is variadic and
        # a space-separated list would greedily consume following bare tokens.
        flags += ["--allowedTools", ",".join(tools)]
        applied.append(f"{len(tools)} tools")

    dirs = []
    for key in ("fs_read", "fs_write"):
        for p in (hints.get(key) or []):
            if isinstance(p, str) and p:
                rp = str(Path(p).expanduser())
                if rp not in dirs:
                    dirs.append(rp)
    if dirs:
        flags += ["--add-dir"] + dirs
        applied.append(f"{len(dirs)} dirs")

    max_turns = (env.get("constraints") or {}).get("max_turns")
    if isinstance(max_turns, int) and max_turns > 0:
        flags += ["--max-turns", str(max_turns)]
        applied.append(f"max_turns={max_turns}")

    # Declared model pin: without it the child inherits the operator's ambient
    # ~/.claude/settings.json model — measured as the top-tier default, which a
    # transcript digest does not need. Declared in the skill, translated here.
    model = hints.get("model")
    if isinstance(model, str) and model:
        flags += ["--model", model]
        applied.append(f"model={model}")

    prompt = _envelope_prompt(env, ctx or {})
    if prompt:
        flags += ["--append-system-prompt", prompt]
        applied.append("system-prompt")

    mcps = [m for m in (hints.get("required_mcps") or []) if isinstance(m, str)]
    if mcps:
        # Declared, but not translated in the pilot: there is no console-side MCP
        # config file to point --mcp-config at, and inventing a path would be a
        # guess. The child reaches kmcp the way the console does (knowledge-cli in
        # local-trusted mode). Surfaced rather than silently dropped.
        applied.append(f"mcps declared but unmapped: {','.join(mcps)}")

    if not flags:
        return [], f"{ref}: envelope resolved but declared nothing"
    return flags, f"{ref}: " + ", ".join(applied)


# ----------------------------------------------------------------------------
# answer / fork (unchanged behaviour from the prototype)
# ----------------------------------------------------------------------------
def spawn_claude(args, cwd, session_id=None, log_path=None, action=None,
                 envelope_ctx=None):
    """Spawn a claude run, registering it so Stop can signal its process group.

    log_path captures this run's output to a DEDICATED file instead of the
    shared answers.log — used by the summarize action so it can measure whether
    the child actually produced anything (a zero-output rc==0 child is the
    observed silent no-op). Default behaviour (shared answers.log) is unchanged.

    action names a console action bound to a kmcp skill (see ACTION_SKILLS); its
    declared permission envelope is resolved and PREPENDED to args. Callers that
    pass no action — answer, fork, the queue dispatcher — are untouched: they
    resolve to zero flags and spawn exactly as before. The resolved note is
    attached to the returned Popen as `envelope_note` so endpoints can surface it
    without changing this function's return contract.
    """
    # Resolve `claude` robustly. A console launched with a minimal PATH (a
    # launchd/GUI parent hands down `/usr/bin:/bin:/usr/sbin:/sbin`) has no
    # ~/.local/bin, so a bare Popen(["claude", …]) throws FileNotFoundError —
    # which /api/answer then turned into a bodyless 500 (the JSON.parse crash).
    # Mirror the _csd_bin()/shutil.which fallback pattern.
    claude = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
    if not Path(claude).exists():
        raise FileNotFoundError(
            f"`claude` binary not found (checked PATH and {claude}); is Claude "
            "Code installed and on the console's PATH?")
    # Augment the child PATH so the resumed claude can find its own tools even
    # when the console itself was started with a truncated PATH.
    env = dict(os.environ)
    local_bin = str(Path.home() / ".local" / "bin")
    if local_bin not in env.get("PATH", "").split(os.pathsep):
        env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")

    # Resolve the declared envelope and PREPEND its flags. Prepending is only
    # safe because every call site's args begin with an option token ("-p"):
    # --allowedTools and --add-dir are variadic and would otherwise swallow a
    # bare prompt positional. That is a contract, so check it rather than trust
    # it — a caller that breaks it loses the envelope, never its prompt.
    flags, envelope_note = resolve_envelope(action, envelope_ctx)
    if flags and not (args and str(args[0]).startswith("-")):
        flags, envelope_note = [], (
            "envelope skipped: spawn args must begin with an option token or a "
            "variadic flag would consume the prompt")
    full_args = flags + list(args)

    log_file = Path(log_path) if log_path else ANSWER_LOG
    with open(log_file, "a") as log:
        log.write(f"\n--- spawn {time.strftime('%H:%M:%S')}: {claude} {' '.join(full_args)} (cwd={cwd})\n")
        if envelope_note:
            log.write(f"--- envelope: {envelope_note}\n")
        log.flush()
        proc = subprocess.Popen(
            [claude] + full_args, cwd=cwd or str(Path.home()),
            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True, env=env,
        )
    proc.envelope_note = envelope_note
    _register(session_id, proc)
    return proc


# ----------------------------------------------------------------------------
# reply queue — never block the operator on the two-writer guard
#
# /api/answer used to REFUSE when the session couldn't accept a write: the
# transcript was written <15s ago (the two-writer guard), or a run this console
# spawned for it is still in flight. Now the composer always accepts — when the
# session is busy the message is queued (per-session FIFO, persisted at
# $CSD_STATE_DIR/console/queue.json with the same atomic-replace pattern as
# archived.json, so a restart loses nothing) and a background dispatcher sends
# it through the SAME spawn path as a direct answer once the block clears.
#
# The guard itself is untouched — the queue is how we WAIT FOR it, never a
# bypass: answer_blocked() wraps the guard's boolean plus the run registry
# (_live_procs, the console's existing record of what it spawned), and both the
# direct path and the dispatcher consult it. Strict order: only the head of a
# session's queue is ever eligible, and a head that exhausted its retries stays
# visible (state "failed", error attached) and deliberately blocks the rest
# until the operator dismisses it — reordering or silently dropping a message
# would be worse than pausing.
# ----------------------------------------------------------------------------
QUEUE_FILE = CONSOLE_STATE / "queue.json"
_QUEUE_LOCK = threading.Lock()        # queue.json read-modify-write
_DISPATCH_LOCK = threading.Lock()     # one spawn decision at a time (all paths)
QUEUE_MAX_ATTEMPTS = 3
QUEUE_RETRY_BACKOFF_S = 30            # × attempts, between failed dispatches
QUEUE_POLL_S = 2.0                    # dispatcher tick; cheap (one stat + poll)


def transcript_write_guard(sid: str) -> bool:
    """The two-writer guard's boolean: True while the session's transcript was
    written within the last 15s. Same check /api/answer always made inline —
    kept as ONE interface so queue consumers wait on it rather than re-derive
    it. Never weakened here; the queue exists to outlast it, not bypass it."""
    src = find_session(sid)
    try:
        return bool(src and time.time() - src.stat().st_mtime < 15)
    except OSError:
        return False


def answer_blocked(sid: str):
    """Why the session can't take `claude -p --resume` RIGHT NOW, or None.

    Two blocks, both already tracked elsewhere and only consulted here:
    a live run the console itself spawned (the run registry), and the
    two-writer guard window after any transcript write."""
    if _live_procs(sid):
        return "console-spawned run still in flight"
    if transcript_write_guard(sid):
        return "session written in the last 15s (two-writer guard)"
    return None


def _read_queue() -> dict:
    """sid -> [item, ...] (FIFO). Missing/corrupt file degrades to empty."""
    try:
        d = json.loads(QUEUE_FILE.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_queue(q: dict) -> None:
    _atomic_write_json(QUEUE_FILE, {k: v for k, v in q.items() if v})


def queue_for(sid: str) -> list:
    """UI projection of a session's pending queue (never the cwd internals)."""
    return [{"id": it.get("id"), "text": it.get("text"),
             "queued_at": it.get("queued_at"),
             "state": it.get("state", "queued"),
             "attempts": it.get("attempts", 0), "error": it.get("error")}
            for it in _read_queue().get(sid) or []]


def enqueue_answer(sid: str, cwd, text: str, reason: str) -> dict:
    item = {"id": uuidlib.uuid4().hex[:12], "text": text, "cwd": cwd,
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "attempts": 0, "state": "queued", "error": None,
            "next_attempt_at": 0, "blocked_reason": reason}
    with _QUEUE_LOCK:
        q = _read_queue()
        q.setdefault(sid, []).append(item)
        _write_queue(q)
        pos = len(q[sid])
    return {"ok": True, "action": "queued", "session": sid,
            "queue_id": item["id"], "position": pos, "reason": reason}


def cancel_queued(sid: str, qid: str) -> dict:
    """Drop a queued message. Refused mid-dispatch (the spawn may already have
    happened) and after dispatch (nothing left to cancel) — never ambiguous."""
    with _QUEUE_LOCK:
        q = _read_queue()
        items = q.get(sid) or []
        hit = next((it for it in items if it.get("id") == qid), None)
        if hit is None:
            return {"ok": False,
                    "error": "message not queued (already dispatched or cancelled)"}
        if hit.get("state") == "dispatching":
            return {"ok": False, "error": "message is dispatching right now"}
        items = [it for it in items if it.get("id") != qid]
        if items:
            q[sid] = items
        else:
            q.pop(sid, None)
        _write_queue(q)
    return {"ok": True, "session": sid, "cancelled": qid}


def submit_answer(sid: str, cwd, text: str) -> tuple:
    """(payload, http_code) for /api/answer — the never-refuse composer path.

    Sendable now AND nothing already queued -> spawn directly, exactly the
    pre-queue behaviour. Otherwise enqueue: a non-empty queue enqueues even
    when the guard is clear, so a new message can never overtake older queued
    ones (strict per-session FIFO)."""
    with _DISPATCH_LOCK:
        reason = answer_blocked(sid)
        if reason is None and not (_read_queue().get(sid) or []):
            # Spawn can throw (e.g. `claude` unresolved) — must return JSON,
            # not let the exception close the connection bodyless.
            try:
                spawn_claude(["-p", "--resume", sid, text], cwd, sid)
            except Exception as e:  # noqa: BLE001
                return {"error": f"failed to spawn claude: {str(e)[:250]}"}, 500
            return {"ok": True, "action": "answer", "session": sid}, 200
    return enqueue_answer(sid, cwd, text, reason or "behind queued messages"), 200


def _fail_queued(sid: str, qid: str, err: str) -> None:
    """Record a failed dispatch on the item: bounded retries with backoff,
    then a terminal 'failed' state the operator sees and must dismiss."""
    with _QUEUE_LOCK:
        q = _read_queue()
        for it in q.get(sid) or []:
            if it.get("id") == qid:
                it["attempts"] = it.get("attempts", 0) + 1
                it["error"] = err[:300]
                exhausted = it["attempts"] >= QUEUE_MAX_ATTEMPTS
                it["state"] = "failed" if exhausted else "queued"
                it["next_attempt_at"] = (
                    time.time() + QUEUE_RETRY_BACKOFF_S * it["attempts"])
                break
        _write_queue(q)


def _pop_queued(sid: str, qid: str) -> None:
    with _QUEUE_LOCK:
        q = _read_queue()
        items = [it for it in (q.get(sid) or []) if it.get("id") != qid]
        if items:
            q[sid] = items
        else:
            q.pop(sid, None)
        _write_queue(q)


def _dispatch_tick() -> None:
    """One pass over every session with queued messages. Head-only (strict
    order), single-flight per session (a just-spawned run registers in RUNS
    before the lock is released, so answer_blocked holds until it exits and
    the guard window passes)."""
    now = time.time()
    for sid in list(_read_queue().keys()):
        with _DISPATCH_LOCK:
            if answer_blocked(sid):
                continue
            with _QUEUE_LOCK:
                q = _read_queue()
                items = q.get(sid) or []
                if not items:
                    continue
                head = items[0]
                if head.get("state") == "failed":
                    continue            # blocks the queue until dismissed
                if (head.get("next_attempt_at") or 0) > now:
                    continue            # backing off after a failed attempt
                head["state"] = "dispatching"   # cancel refuses from here on
                _write_queue(q)
            try:
                spawn_claude(["-p", "--resume", sid, head.get("text") or ""],
                             head.get("cwd"), sid)
            except Exception as e:  # noqa: BLE001 — surfaced on the item
                _fail_queued(sid, head["id"], str(e))
            else:
                _pop_queued(sid, head["id"])


def _reset_stuck_dispatching() -> None:
    """Startup pass: a crash mid-dispatch leaves an item 'dispatching'; put it
    back to 'queued' so it isn't orphaned (worst case is one duplicate send,
    never a silent drop)."""
    with _QUEUE_LOCK:
        q = _read_queue()
        changed = False
        for items in q.values():
            for it in items:
                if it.get("state") == "dispatching":
                    it["state"] = "queued"
                    changed = True
        if changed:
            _write_queue(q)


def _queue_dispatcher() -> None:
    _reset_stuck_dispatching()
    while True:
        try:
            _dispatch_tick()
        except Exception:  # noqa: BLE001 — the dispatcher must never die
            pass
        time.sleep(QUEUE_POLL_S)


# ----------------------------------------------------------------------------
# angles: mine on demand, then curate
#
# The action-vocabulary named in claudecode:design/turn-angles-context-cockpit:
#   track  -> event      record -> lesson      task -> task
#   load/drop -> context (client-side: it edits the NEXT message, not the base)
#   link   -> edge       (deferred: needs a second endpoint to link to)
#
# Curation is the only thing here that WRITES to kmcp, and it is two-phase:
# compose a draft, validate it with import_entries dry_run, show it, and write
# only on explicit confirm. A small model's headline never reaches the corpus
# unreviewed.
# ----------------------------------------------------------------------------
KMCP_DSN = None            # set by serve()
MINE_TIMEOUT_S = 300
KMCP_TIMEOUT_S = 120

EVENT_TYPES = {"schema_change", "deployment", "data_migration", "decision",
               "bugfix", "configuration", "import", "security", "refactor",
               "feature", "deprecation"}


def _csd_bin():
    return shutil.which("csd") or None


def mine_angles(sid: str, no_probes=False, angles=None) -> dict:
    """Run the miner for one session, the way we already shell out to claude.

    `angles` (a validated subset of ANGLE_SPECS keys) mines just those angles;
    the miner carries the rest of the store forward when the turn is unchanged.
    """
    csd = _csd_bin()
    cmd = ([csd] if csd else [sys.executable, "-m", "claude_session_db.cli"])
    cmd += ["angles", *(angles or []), "--session", sid]
    if no_probes:
        cmd.append("--no-probes")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=MINE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"mining timed out after {MINE_TIMEOUT_S}s"}
    if p.returncode != 0:
        return {"ok": False, "error": (p.stderr or p.stdout).strip()[:400]}
    return {"ok": True, "rail": angle_rail(sid)}


class KmcpError(RuntimeError):
    pass


def _kmcp_call(tool: str, args: dict) -> dict:
    """knowledge-cli in local-trusted mode — the same path csd summarize uses."""
    cli = (os.environ.get("CSD_KNOWLEDGE_CLI") or shutil.which("knowledge-cli")
           or str(Path.home() / ".local" / "bin" / "knowledge-cli"))
    if not Path(cli).exists():
        raise KmcpError("knowledge-cli not found (set CSD_KNOWLEDGE_CLI)")
    env = dict(os.environ)
    if KMCP_DSN:
        env["DATABASE_URL"] = KMCP_DSN
    env["KNOWLEDGE_ALLOW_UNAUTH_LOCAL"] = "1"
    state = CONSOLE_STATE.parent / "kmcp-data"
    state.mkdir(parents=True, exist_ok=True)
    env.setdefault("KNOWLEDGE_DATA_DIR", str(state))
    try:
        p = subprocess.run([cli, "call", tool, "-"], input=json.dumps(args),
                           capture_output=True, text=True, env=env,
                           cwd=str(state), timeout=KMCP_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise KmcpError(f"{tool}: timed out after {KMCP_TIMEOUT_S}s") from exc
    out = p.stdout.strip()
    brace = out.find("{")
    if brace >= 0:
        try:
            return json.loads(out[brace:])
        except json.JSONDecodeError:
            pass
    raise KmcpError(f"{tool}: rc={p.returncode} out={out[:200]!r} "
                    f"err={p.stderr.strip()[:200]!r}")


def _slug(text: str, cap=60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:cap].rstrip("-")) or "untitled"


DEFAULT_APP = os.environ.get("CSD_CONSOLE_DEFAULT_APP", "claudecode")
_APPS_CACHE: dict = {"names": None, "at": 0.0}
_APPS_TTL_S = 300

# cwd basenames that don't match their kmcp app name
APP_ALIASES = {
    "knowledge": "knowledge_mcp_code",
    "claude_session_db": "claude_session_db",
    "claude-session-db": "claude_session_db",
}


def _live_apps() -> set:
    if (_APPS_CACHE["names"] is not None
            and time.time() - _APPS_CACHE["at"] < _APPS_TTL_S):
        return _APPS_CACHE["names"]
    try:
        r = _kmcp_call("list_applications", {})
        names = {a["name"] for a in r.get("applications", []) if a.get("name")}
    except KmcpError:
        names = set()
    if names:
        _APPS_CACHE.update(names=names, at=time.time())
    return names


def _infer_app(cwd: str) -> tuple:
    """(application, status) where status is matched | fallback | unknown.

    The cwd basename is a GUESS. Two failure modes, both real:
      - `final_taglists` is not a kmcp app; writing there would CREATE a junk
        application out of a directory name.
      - Silently falling back to DEFAULT_APP is worse: the write succeeds, in
        the wrong corpus, and nothing says so.
    So inference only ever PROPOSES. A `fallback` never gets written without
    the operator naming the application explicitly.
    """
    base = Path(cwd or "").name
    cand = APP_ALIASES.get(base) or APP_ALIASES.get(base.replace("-", "_")) \
        or base.replace("-", "_")
    live = _live_apps()
    if not live:                       # kmcp unreachable — don't pretend
        return cand, "unknown"
    if cand in live:
        return cand, "matched"
    return DEFAULT_APP, "fallback"


def compose_curation(sid: str, item_id: str, action: str, fields: dict) -> dict:
    """Build the kmcp entry document for one curated angle headline."""
    item = angle_detail(sid, item_id)
    if not item:
        raise KmcpError(f"{item_id} not mined for {sid}")
    store = _angles_store(sid) or {}
    headline = fields.get("headline") or item.get("headline") or item_id
    detail = item.get("detail")
    detail_txt = (detail if isinstance(detail, str)
                  else json.dumps(detail, indent=2, ensure_ascii=False))[:4000]
    if fields.get("application"):
        app = fields["application"]
        live = _live_apps()
        app_status = "explicit" if (not live or app in live) else "fallback"
    else:
        app, app_status = _infer_app(store.get("cwd", ""))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prov = (f"Curated from turn-angle {item_id} ({item.get('angle')}) of "
            f"session {sid}. Session cwd: {store.get('cwd') or 'unknown'}.")

    if action == "track":
        etype = fields.get("event_type", "decision")
        if etype not in EVENT_TYPES:
            raise KmcpError(f"event_type {etype!r} not in {sorted(EVENT_TYPES)}")
        doc = {
            "application": app, "path": f"event/{today}/{_slug(headline)}",
            "entity_type": "event", "title": headline,
            "description": fields.get("description") or headline,
            "tags": fields.get("tags") or ["turn-angles"],
            "content": {
                "summary": headline, "details": f"{detail_txt}\n\n{prov}",
                "event_type": etype, "occurred_at": today,
                "actor": "console-curation", "scope": [sid],
            },
        }
    elif action == "record":
        doc = {
            "application": app, "path": f"lesson/{_slug(headline)}",
            "entity_type": "lesson", "title": headline,
            "description": fields.get("description") or headline,
            "tags": fields.get("tags") or ["turn-angles"],
            "content": {
                "problem": fields.get("problem") or headline,
                "solution": fields.get("solution") or "",
                "lesson_learned": fields.get("lesson_learned") or headline,
                "category": fields.get("category", "process"),
                "severity": fields.get("severity", "medium"),
                "context": f"{detail_txt}\n\n{prov}",
                "date_learned": today,
            },
        }
    elif action == "task":
        doc = {
            "application": app, "path": f"task/{_slug(headline)}",
            "entity_type": "task", "title": headline,
            "description": fields.get("description") or headline,
            "tags": fields.get("tags") or ["turn-angles"],
            "content": {
                "objective": fields.get("objective") or headline,
                "task_type": fields.get("task_type", "action"),
                "status": "pending",
                "context": f"{detail_txt}\n\n{prov}",
                "acceptance_criteria": fields.get("acceptance_criteria")
                                       or [headline],
            },
        }
    else:
        raise KmcpError(f"unknown action {action!r} "
                        "(track | record | task; load/drop are client-side)")
    doc["_app_status"] = app_status        # stripped before the write
    doc["_cwd"] = store.get("cwd", "")
    return doc


def curate(sid: str, item_id: str, action: str, fields: dict,
           confirm: bool) -> dict:
    """Two-phase: dry_run validates and returns the draft; confirm writes it.

    JSON is valid YAML 1.2, so passing the document as JSON sidesteps the
    import_entries YAML footguns wholesale — unquoted `#` truncation, bare
    timestamps coerced to datetime, angle-bracket placeholder rejection.
    """
    doc = compose_curation(sid, item_id, action, fields)
    status = doc.pop("_app_status")
    cwd = doc.pop("_cwd")
    apps = sorted(_live_apps())

    if not confirm:
        res = _kmcp_call("import_entries", {"content": json.dumps(doc),
                                            "dry_run": True})
        return {"ok": True, "phase": "draft", "draft": doc, "dry_run": res,
                "app_status": status, "cwd": cwd, "applications": apps}

    # Two ways a confirmed write lands somewhere wrong, both refused here:
    #   fallback — the cwd basename named no live app, so `application` is a
    #              default, not a decision. Silently writing there puts the
    #              entry in the wrong corpus and says nothing.
    #   unknown  — kmcp was unreachable, so we cannot know if the app exists;
    #              writing could CREATE a junk application from a directory name.
    if status in ("fallback", "unknown"):
        why = (f"the session cwd ({cwd or 'unknown'}) names no live kmcp app"
               if status == "fallback" else
               "the kmcp application list is unreachable")
        return {"ok": False, "phase": "refused", "draft": doc,
                "app_status": status, "cwd": cwd, "applications": apps,
                "error": f"refusing to write: {why}, so "
                         f"{doc['application']!r} is a guess, not a choice. "
                         "Name the application explicitly."}

    res = _kmcp_call("import_entries", {"content": json.dumps(doc),
                                        "dry_run": False})
    verify = _kmcp_call("get_entry", {"application": doc["application"],
                                      "path": doc["path"], "summary": True})
    wrote = "error" not in verify
    return {"ok": wrote, "phase": "written", "draft": doc, "result": res,
            "verified": verify if wrote else None,
            "error": None if wrote else f"read-back failed: {verify.get('error')}"}


def point_fork(session_id: str, at_uuid: str):
    src = find_session(session_id)
    if src is None:
        raise FileNotFoundError(f"session {session_id} not found")
    new_id = str(uuidlib.uuid4())
    dst = src.parent / f"{new_id}.jsonl"
    kept, found = [], False
    with open(src) as f:
        for ln in f:
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if r.get("sessionId"):
                r["sessionId"] = new_id
            kept.append(json.dumps(r))
            if r.get("uuid") == at_uuid:
                found = True
                break
    if not found:
        raise ValueError(f"message {at_uuid} not in session {session_id}")
    kept.append(json.dumps({
        "type": "custom-title", "sessionId": new_id,
        "customTitle": f"fork of {session_id[:8]} @ {at_uuid[:8]}",
    }))
    dst.write_text("\n".join(kept) + "\n")
    return new_id


# ----------------------------------------------------------------------------
# CR — context reduction: a fork with curation (engine: claude_session_db/cr.py)
#
# The operator selects which context survives; CR writes a NEW reduced session
# file (redaction fork — every record kept structurally, unkept content swapped
# for breadcrumbs in both copies) plus ONE synthetic preamble user record
# carrying the compiled kmcp cart. Two-phase like curate(): confirm:false is a
# PREVIEW (nothing written), confirm:true forges the fork. No spawn — the fork
# appears in the sidebar; pull not push. The original is never touched, and the
# fork id is minted HERE (house doctrine — never inferred from claude).
#
# kmcp is optional at every step: search surfaces a visible {error}, cart
# hydration failure degrades the preamble to plain text pointers. Neither ever
# blocks the fork.
# ----------------------------------------------------------------------------
def cr_manifest_payload(sid: str):
    path = find_session(sid)
    if path is None:
        return {"error": "not found"}, 404
    records, truncated = all_records(path)
    records = [r for r in records if not r.get("isSidechain")]
    m = crlib.build_manifest(records, bash_kmcp=_bash_kmcp)
    m["session_id"] = sid
    m["truncated"] = truncated
    return m, 200


def cr_search(q: str, app):
    """hybrid_search proxied through the knowledge-cli path. Unreachable kmcp
    is a visible {error}, never a block — the cart keeps plain-text refs."""
    args = {"query": q, "limit": 8}
    if app:
        args["application"] = app
    try:
        r = _kmcp_call("hybrid_search", args)
    except KmcpError as e:
        return {"error": str(e)[:300], "results": []}
    hits = r.get("results") or r.get("entries") or []
    out = []
    for h in hits if isinstance(hits, list) else []:
        if not isinstance(h, dict):
            continue
        out.append({
            "application": h.get("application"), "path": h.get("path"),
            "title": h.get("title"), "description": h.get("description"),
            "entity_type": h.get("entity_type"),
            "score": h.get("score") or h.get("rrf_score"),
            "chars": h.get("content_size") or h.get("size"),
        })
    return {"results": out}


def cr_hydrate(refs):
    """(entries, error) — ONE get_entries batch for the whole cart, input-
    ordered. Any failure returns (None, why); the caller degrades to plain
    pointers. Never raises."""
    items = []
    for ref in refs:
        app, path = crlib.parse_ref(ref)
        items.append({"application": app or "?", "path": path or str(ref)})
    try:
        r = _kmcp_call("get_entries", {"entries": items})
    except KmcpError as e:
        return None, str(e)[:300]
    ents = r.get("entries") or r.get("results")
    if not isinstance(ents, list):
        return None, "unexpected get_entries response shape"
    return ents, None


def _cr_refs(body_refs) -> list:
    return [str(x) for x in body_refs if isinstance(x, str) and x.strip()] \
        if isinstance(body_refs, list) else []


def cr_compile(refs):
    """Compile-only mode: the cart document for composer/clipboard — no fork."""
    refs = _cr_refs(refs)
    entries = err = None
    if refs:
        entries, err = cr_hydrate(refs)
    doc = crlib.render_preamble(refs, entries=entries, error=err)
    return {"ok": True, "document": doc, "refs": refs,
            "hydrated": bool(refs) and err is None, "error": err}


def cr_apply(sid: str, stub, refs, confirm: bool):
    """Two-phase CR (house curate idiom): confirm:false previews the before/
    after totals + the compiled preamble draft; confirm:true writes the
    redacted COPY and returns new_session. Original never touched."""
    path = find_session(sid)
    if path is None:
        return {"error": "not found"}, 404
    stub = [str(x) for x in stub if isinstance(x, str)] \
        if isinstance(stub, list) else []
    refs = _cr_refs(refs)

    records = [r for r in crlib.load(path) if not r.get("isSidechain")]
    bad = crlib.unsupported_versions(records)
    if bad:
        return {"error": "transcript carries record version(s) this redactor "
                         "has not been validated against: "
                         f"{', '.join(bad)} — refusing to fork"}, 409

    entries = err = None
    if refs:
        entries, err = cr_hydrate(refs)
    preamble = crlib.render_preamble(refs, entries=entries, error=err) \
        if refs else None

    if not confirm:
        manifest = crlib.build_manifest(records, bash_kmcp=_bash_kmcp)
        before = crlib.context_surface(records)
        stats = crlib.apply_stubs(records, manifest, stub)   # in-memory only
        after = crlib.context_surface(records) + len(preamble or "")
        return {"ok": True, "phase": "preview",
                "before_chars": before, "after_chars": after,
                "before_tokens": crlib.est_tokens(before),
                "after_tokens": crlib.est_tokens(after),
                "saved_pct": round((before - after) / before * 100, 1)
                if before else 0,
                "floor": dict(crlib.FLOOR_TOKENS),
                "stubbed": len(stats["stubbed"]), "ignored": stats["ignored"],
                "preamble": preamble, "refs": refs,
                "hydrated": bool(refs) and err is None,
                "hydrate_error": err}, 200

    new_id = str(uuidlib.uuid4())        # the console mints the fork id itself
    res = crlib.forge_fork(path, stub, preamble_text=preamble, new_id=new_id,
                           bash_kmcp=_bash_kmcp)
    return {"ok": True, "phase": "forked", "action": "cr-fork",
            "new_session": res["new_session"], "path": res["path"],
            "before_tokens": res["before_tokens"],
            "after_tokens": res["after_tokens"],
            "saved_pct": res["saved_pct"],
            "stubbed": len(res["stubbed"]), "ignored": res["ignored"],
            "refs": refs, "hydrated": bool(refs) and err is None,
            "hydrate_error": err}, 200


# ----------------------------------------------------------------------------
# session summary + archive — independent, off-session
#
# Runs /session-summary in its OFF-SESSION mode: a throwaway `claude -p` process
# (no --resume) is handed the session UUID as the skill argument, so the skill
# digests the target transcript from disk (session_digest.py) and writes the
# changelog events + attribution-tagged lessons to kmcp WITHOUT ever resuming or
# appending to the original session. The transcript is read, never touched.
#
# Because nothing writes back to the session, two things fall away from the old
# inline path: the 15s two-writer guard (an independent reader can't collide with
# a live session), and the archive-after-rc==0 coupling. The session is archived
# the moment the summary is dispatched; the summary's outcome is tracked in
# SUMMARIZING for visibility but no longer gates the archive.
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# repeatable passes — prior-capture resolution (Stage 2 of the delta design)
#
# A session summarized once can be summarized AGAIN, and the second pass must
# cover only what the first never saw. The scope decision is NOT re-implemented
# here: summarize._delta_gate is the authority (the very gate the launchd timer
# grades its queue with). The console only supplies the row it grades, and
# translates the verdict into the child's framing + the pass ledger.
#
# DOCTRINE, identical to resolve_envelope(): this can never block the console.
# No DSN, unreachable archive, missing summary_passes, a raising gate — every
# failure degrades to FULL scope (the historic behaviour, byte for byte), with
# the reason surfaced in the response instead of swallowed. The one thing that
# DOES refuse is a lost claim: two passes digesting the same tail into two
# entries is worse than not summarizing now.
# ----------------------------------------------------------------------------
DELTA_MODES = ("auto", "force", "off")
SUMMARIZE_IDLE_WARN_S = int(os.environ.get("CSD_SUMMARIZE_MIN_IDLE_S", "900"))
SCOPE_TTL_S = 120                    # the UI label rides the 3s session poll
_SCOPE_CACHE: dict = {}              # sid -> (fetched_at, scope|None)
_SCOPE_LOCK = threading.Lock()

# The SQL, the prior-capture read and the grader itself now live in
# summarize.py (`prior_capture` / `resolve_summary_scope`) — one grader for the
# console button, `csd summary-scope` and the /session-summary skill. What stays
# here is the console's binding of its module-level DSNs to that grader
# (summarize is imported lazily — the console must not need psycopg to boot).


def _prior_capture(sid: str) -> dict:
    """The row summarize._delta_gate grades, for ONE session. Raises."""
    from .. import summarize as ph4
    return ph4.prior_capture(sid, CSD_DSN)


def summary_scope(sid: str):
    """Cheap prior-capture facts for the UI label — DB only, NO transcript
    classification (this rides the /api/session poll). None when the session was
    never captured, or the archive is unreachable. Never raises."""
    now = time.time()
    with _SCOPE_LOCK:
        hit = _SCOPE_CACHE.get(sid)
        if hit and now - hit[0] < SCOPE_TTL_S:
            return hit[1]
    out = None
    try:
        row = _prior_capture(sid)
        if row.get("prev_pass"):
            from .. import session_mgmt as mgmt
            from .. import summarize as ph4
            wm, src = mgmt._watermark_for(sid, CSD_DSN, KMCP_DSN)
            app, path = row.get("prev_app"), row.get("prev_path")
            out = {"since": ph4._iso(wm), "source": src,
                   "pass": row["prev_pass"] + 1,
                   "prior": f"{app}:{path}" if app and path else None}
    except Exception:  # noqa: BLE001 — no label is fine; a 500 is not
        out = None
    with _SCOPE_LOCK:
        _SCOPE_CACHE[sid] = (now, out)
    return out


def resolve_summary_scope(sid: str, mode: str = "auto") -> dict:
    """How the next pass should be scoped, for THIS console's DSNs. NEVER raises.

    auto  — delta when a watermark resolves AND the gate calls the tail real.
    force — delta from the watermark whatever the gate thinks of the tail.
    off   — full scope, the historic behaviour.
    """
    from .. import summarize as ph4
    return ph4.resolve_summary_scope(sid, CSD_DSN, KMCP_DSN, mode=mode,
                                     prior_capture_fn=_prior_capture)


def _idle_warning(sid: str):
    """Phase-4 quiesces a session for 900s before digesting it; the console
    button does not — a manual close-out is deliberate, and hard-blocking it
    would make the operator wait on a session they just finished with. But a
    transcript still being written is digested SHORT, silently, so the
    condition is surfaced instead of enforced."""
    blocked = answer_blocked(sid)
    if blocked and "in flight" in blocked:
        return ("a console-spawned run is still in flight — whatever it writes "
                "lands after this digest and will NOT be summarized")
    src = find_session(sid)
    try:
        idle = (time.time() - src.stat().st_mtime) if src else None
    except OSError:
        idle = None
    if idle is not None and idle < SUMMARIZE_IDLE_WARN_S:
        return (f"session wrote {int(idle)}s ago (phase-4 waits "
                f"{SUMMARIZE_IDLE_WARN_S}s before digesting) — anything written "
                "from now on is NOT in this summary")
    return None


# --- pass ledger claim (console ⟷ launchd single dispatch) --------------------

def _claim_pass(sid: str, pass_no: int) -> dict:
    """Take the session-scoped advisory lock and open this pass's ledger row.

    The lock lives on the RETURNED connection and is held until the child exits
    (_settle_pass releases it) — that is what stops the console and the launchd
    timer from digesting the same tail into two entries. An unreachable archive
    is not a refusal: the pass runs unrecorded and says so."""
    if not CSD_DSN:
        return {"conn": None, "refused": None,
                "note": "pass not recorded (no archive DSN)"}
    conn = None
    try:
        import psycopg
        from .. import summarize as ph4
        conn = psycopg.connect(CSD_DSN, connect_timeout=5)
        ph4.ensure_passes_table(conn)
        if not ph4.claim_session(conn, sid):
            conn.close()
            return {"conn": None, "note": None,
                    "refused": "another summarize pass is already in flight for "
                               "this session (the console or the launchd timer "
                               "holds it) — try again when it settles"}
        ph4.open_pass(conn, sid, pass_no)
        return {"conn": conn, "refused": None, "note": None}
    except Exception as exc:  # noqa: BLE001 — the ledger must not block a pass
        try:
            if conn is not None:
                conn.close()
        except Exception:  # noqa: BLE001
            pass
        return {"conn": None, "refused": None,
                "note": f"pass not recorded ({type(exc).__name__}: {exc})"}


def _settle_pass(sid: str, claim, pass_no: int, status: str, detail=None) -> None:
    """Close the ledger row and release the claim. The WATERMARK is not stamped
    here — the child writes the kmcp entry, and `csd reconcile-summaries` is the
    one thing allowed to move summary_state (truth from the ledger, not from the
    narrator)."""
    conn = (claim or {}).get("conn")
    if conn is None:
        return
    try:
        from .. import summarize as ph4
        ph4.record_pass(conn, sid, pass_no, status=status, detail=detail)
        ph4.release_session(conn, sid)
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    with _SCOPE_LOCK:
        _SCOPE_CACHE.pop(sid, None)


SUMMARIZE_PROMPT = "/session-summary"
SUMMARIZING: dict[str, str] = {}     # sid -> "running" | "done" | error text
# parent sid -> the run record: {child, pass, started, ended, rc}. The child
# is a REAL session (a fresh `claude -p` under an id the console minted), so
# it is addressable in the sidebar from the moment it is dispatched.
SUMMARY_RUNS: dict[str, dict] = {}
SUMMARY_MIN_OUTPUT_BYTES = 40        # child output past the header ⇒ it ran
_SUMMARY_LOG_DIR = CONSOLE_STATE / "summaries"


def _await_summary(sid: str, proc, log_path: Path, base_size: int,
                   claim=None, pass_no: int = 1):
    """Resolve a dispatched summary. rc!=0 → failed. rc==0 does NOT prove a kmcp
    write happened — but a child that produced NO output past the spawn header
    is the observed silent no-op, so it is downgraded rather than called done.
    The same verdict settles the pass ledger row and releases its claim."""
    rc = proc.wait()
    run = SUMMARY_RUNS.get(sid)
    if run is not None:
        run.update(ended=time.time(), rc=rc)
    if rc != 0:
        SUMMARIZING[sid] = f"summary failed (rc={rc})"
        _settle_pass(sid, claim, pass_no, "failed", f"child rc={rc}")
        return
    try:
        produced = log_path.stat().st_size - base_size
    except OSError:
        produced = SUMMARY_MIN_OUTPUT_BYTES + 1     # can't measure → don't accuse
    ok = produced > SUMMARY_MIN_OUTPUT_BYTES
    SUMMARIZING[sid] = "done" if ok else "summary produced no output"
    _settle_pass(sid, claim, pass_no, "written" if ok else "failed",
                 None if ok else "child produced no output")


def summarize_session(sid: str, cwd: str, archive: bool = True,
                      delta: str = "auto", dry_run: bool = False) -> dict:
    """Dispatch the off-session summary. archive=True (the default, and the
    only behaviour until the digest-reader actions) also archives the session
    the moment the summary is dispatched; archive=False leaves the session in
    the sidebar — summary only. The dispatch itself is identical either way.

    `delta` scopes the pass (auto | force | off — see resolve_summary_scope):
    a session already captured once is summarized only over the tail its prior
    pass never saw, and the window travels to the child through the envelope's
    appended system prompt. dry_run resolves the scope and returns it WITHOUT
    claiming, spawning or archiving anything — the testable seam."""
    if delta not in DELTA_MODES:
        delta = "auto"
    if SUMMARIZING.get(sid) == "running":
        return {"ok": False, "error": "a summary is already running"}

    scope = resolve_summary_scope(sid, delta)
    idle = _idle_warning(sid)
    src = find_session(sid)
    base = {"action": "summarize", "session": sid,
            "pass": scope["pass"], "since": scope["since"],
            "delta_mode": "delta" if scope["delta"] else "full",
            "prior": scope["prior"], "scope_note": scope["note"],
            "warning": " · ".join(w for w in (scope["warning"], idle) if w) or None}
    if dry_run:
        return {"ok": True, "dry_run": True, "archived": False,
                "transcript": str(src) if src else None,
                "scope": scope, **base}

    # One dispatch per session, console AND launchd: the claim is an advisory
    # lock held for the whole pass. A lost claim REFUSES (two passes over one
    # tail would write two entries); an unreachable ledger does not.
    claim = _claim_pass(sid, scope["pass"])
    if claim["refused"]:
        return {"ok": False, "error": claim["refused"], **base}

    # Off-session: fresh `claude -p`, the UUID as the /session-summary argument.
    # No --resume — the original transcript is digested, never appended to.
    # A dedicated per-summary log lets _await_summary measure real output.
    _SUMMARY_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = _SUMMARY_LOG_DIR / f"{sid}.log"
    # Dispatch through the resolver: the summarize action is bound to
    # claude_session_db:skill/console-summarize, whose declared scope covers
    # ~/.claude/projects (the ambient cwd here is the session's own working dir,
    # often a git worktree that cannot see it). The transcript path is resolved
    # HERE and handed down, so the child never needs the compound shell command
    # a headless run cannot get approved. On a continuation the delta window
    # rides along the same channel — the child has no scope to resolve it.
    ctx = {"session_id": sid, "transcript": str(src) if src else None}
    if scope["delta"]:
        ctx.update({"since": scope["since"], "pass": scope["pass"],
                    "prior": scope["prior"]})
    # The console mints the CHILD's session id (the fork doctrine): the run is
    # registered under it — so Stop in the child view aims at the run, and the
    # parent stays free for Answer (the child never writes to it) — and it is
    # titled + linked before it exists, so the sidebar shows a named row the
    # moment the transcript appears instead of a bare uuid.
    child = str(uuidlib.uuid4())
    _pm = _meta_of(_read_meta_overlay(), sid)
    ptitle = _pm.get("title")
    if not ptitle:
        try:
            ptitle = ((summarize_nav(src) or {}).get("title") if src else None)
        except OSError:
            ptitle = None
    ptitle = (ptitle or sid[:8]).strip()
    try:
        proc = spawn_claude(["-p", f"{SUMMARIZE_PROMPT} {sid}",
                             "--session-id", child], cwd, child,
                            log_path=log_path, action="summarize",
                            envelope_ctx=ctx)
    except Exception as exc:  # noqa: BLE001 — a failed spawn must free the claim
        _settle_pass(sid, claim, scope["pass"], "failed",
                     f"spawn failed: {type(exc).__name__}: {exc}")
        return {"ok": False, "error": f"spawn failed: {exc}", **base}
    try:
        base_size = log_path.stat().st_size          # header only, pre-output
    except OSError:
        base_size = 0
    SUMMARIZING[sid] = "running"
    SUMMARY_RUNS[sid] = {"child": child, "pass": scope["pass"],
                         "started": time.time(), "ended": None, "rc": None,
                         "log": str(log_path)}
    set_title(child, f"Summary of {ptitle} (pass {scope['pass']})")
    _update_meta(child, summary_of=sid)
    _update_meta(sid, summary_child=child)
    if archive:
        set_archived(sid, True, reason="session-summary")
    threading.Thread(target=_await_summary,
                     args=(sid, proc, log_path, base_size, claim, scope["pass"]),
                     daemon=True, name=f"summarize-{sid[:8]}").start()
    return {"ok": True, "pid": proc.pid, "archived": archive,
            "child_session": child,
            "envelope": getattr(proc, "envelope_note", None),
            "ledger": claim.get("note"),
            "note": ("independent off-session summary dispatched"
                     + (f" — pass {scope['pass']}, NEW work since "
                        f"{(scope['since'] or '')[:16]}" if scope["delta"]
                        else " — full session scope")
                     + "; "
                     + ("session archived" if archive else "session not archived")),
            **base}


# ----------------------------------------------------------------------------
# session-management lens — open-thread inventory + delta-after-summary digests
#
# The console face of session_mgmt.py (the same lens `csd angles sessions` /
# `csd angles digest` print): one row per recent main session with TRUE last
# activity = max(messages.ts) from the archive, verdicts LIVE / OPEN /
# OPEN-delta / CLOSED, agent-spawn badges from v_agent_children, and the
# deterministic delta-after-summary classification. Read-only over the archive
# + knowledge DB; an unreachable archive degrades to {"error": ...}, never 500.
# ----------------------------------------------------------------------------
CSD_DSN = None             # archive DSN, set by serve()


def mgmt_payload(window_days: int, live_min: int):
    if not CSD_DSN:
        return {"error": "no archive DSN configured (set DATABASE_URL / "
                         "CSD_DATABASE_URL, or pass --dsn to csd console)"}
    from .. import session_mgmt as mgmt
    try:
        rows = mgmt.inventory(CSD_DSN, KMCP_DSN, window_days=window_days,
                              live_min=live_min, with_delta=True)
    except Exception as exc:  # noqa: BLE001 — degrade, don't die
        return {"error": f"{type(exc).__name__}: {exc}"}
    out = []
    for r in rows:
        out.append({
            "session_id": r["session_id"],
            "project_name": r["project_name"],
            "cwd": r["cwd"],
            "git_branch": r["git_branch"],
            "message_count": r["message_count"],
            "last_ts": r["last_ts"].isoformat() if r["last_ts"] else None,
            "idle_s": r["idle_s"],
            "state": r["state"],
            "reason": r["reason"],
            "kmcp_target": (f"{r['kmcp_application']}:{r['kmcp_path']}"
                            if r["kmcp_application"] else None),
            "agents": {"total": r.get("agents_total", 0),
                       "running": r.get("agents_running", 0),
                       "failed": r.get("agents_failed", 0)},
            "delta": r["delta"],
            "verdict": r["verdict"],
        })
    return {"sessions": out}


def digest_payload(sid: str, delta: bool, head, tail, full: bool):
    """(text, http_code) — the per-session digest, delta mode = the
    post-summary tail only."""
    from .. import session_mgmt as mgmt
    try:
        return mgmt.digest_for(sid, dsn=CSD_DSN, kmcp_dsn=KMCP_DSN,
                               delta=delta, head=head, tail=tail,
                               full=full), 200
    except ValueError as exc:
        return f"digest: {exc}", 404
    except Exception as exc:  # noqa: BLE001
        return f"digest: {type(exc).__name__}: {exc}", 500


# ----------------------------------------------------------------------------
# batch operations — one per-session action fanned out over many sessions
#
# The per-session buttons (Mine angles / Generate timeline / Summarize +
# archive / tl;dr) as a queue, never a stampede. Doctrine mirrors angles-watch:
# a SINGLE worker drains the job queue, so N sessions cannot hit the local
# Ollama concurrently. Per action:
#   - angles:    mine_angles() (blocking subprocess) — strictly serial.
#   - timeline:  reuses session_timeline's own single in-process worker via
#     payload(force=True), then waits on its STATUS — so a batch and a manual
#     Generate press share ONE lane instead of racing.
#   - summarize: the SAME off-session dispatch path as the button
#     (summarize_session — never --resume, archive on dispatch), bounded to
#     SUMMARIZE_MAX_INFLIGHT concurrent `claude -p` children; a per-item
#     watcher resolves the outcome off SUMMARIZING.
#   - tldr:      same lane-sharing shape as timeline — tldr.enqueue() puts the
#     job on tldr.py's own single worker (shared with the nav-poll path), then
#     waits on tldr.STATUS. Skip-if-fresh by default: an item whose cached
#     turn_key still matches the transcript is marked done (skipped) without a
#     model call; per-batch options.force regenerates fresh ones too.
# Failures isolate per item — one bad session never aborts the batch.
# State is the console pattern: atomic-replace JSON (batch.json). On restart,
# queued items are re-enqueued (same doctrine as queue.json); items caught
# mid-run are marked failed ("interrupted") rather than silently re-run.
# ----------------------------------------------------------------------------
BATCH_FILE = CONSOLE_STATE / "batch.json"
BATCH_ACTIONS = ("angles", "timeline", "summarize", "tldr")
BATCH_KEEP = 8                       # finished batches kept for the UI
BATCH_MAX_ITEMS = 200
SUMMARIZE_MAX_INFLIGHT = 2
BATCH_TIMELINE_WAIT_S = int(os.environ.get("CSD_BATCH_TIMELINE_WAIT_S", "3600"))
BATCH_TLDR_WAIT_S = int(os.environ.get("CSD_BATCH_TLDR_WAIT_S", "900"))
BATCH_SUMMARY_WAIT_S = int(os.environ.get("CSD_BATCH_SUMMARY_WAIT_S", "7200"))
_BATCH_LOCK = threading.Lock()
_BATCHES: dict[str, dict] = {}       # id -> batch dict (mirrored to BATCH_FILE)
_BATCH_JOBS: "queuelib.Queue[tuple[str, int]]" = queuelib.Queue()
_BATCH_WORKER = None
_SUMMARIZE_SLOTS = threading.BoundedSemaphore(SUMMARIZE_MAX_INFLIGHT)

OPEN_VERDICTS = ("LIVE", "OPEN", "OPEN-delta", "OPEN?")   # i.e. not CLOSED


def _persist_batches_locked() -> None:
    """Caller holds _BATCH_LOCK. Prunes finished batches beyond BATCH_KEEP."""
    done = sorted((b for b in _BATCHES.values() if b.get("done")),
                  key=lambda b: b.get("created_at") or 0)
    for b in done[:-BATCH_KEEP] if len(done) > BATCH_KEEP else []:
        _BATCHES.pop(b["id"], None)
    _atomic_write_json(BATCH_FILE, {"batches": list(_BATCHES.values())})


def _batch_update(bid: str, idx, **fields) -> None:
    with _BATCH_LOCK:
        b = _BATCHES.get(bid)
        if not b:
            return
        if idx is not None and 0 <= idx < len(b["items"]):
            b["items"][idx].update(fields)
        b["done"] = all(i["status"] in ("done", "failed", "cancelled")
                        for i in b["items"])
        if b["done"] and not b.get("ended_at"):
            b["ended_at"] = time.time()
        _persist_batches_locked()


def _watch_batch_summary(bid: str, idx: int, sid: str) -> None:
    """Resolve a dispatched batch summary off SUMMARIZING, then free the slot."""
    try:
        t0 = time.time()
        while time.time() - t0 < BATCH_SUMMARY_WAIT_S:
            st = SUMMARIZING.get(sid)
            if st != "running":
                break
            time.sleep(2)
        else:
            st = f"still running after {BATCH_SUMMARY_WAIT_S}s"
        if st == "done":
            _batch_update(bid, idx, status="done", ended_at=time.time())
        else:
            _batch_update(bid, idx, status="failed", ended_at=time.time(),
                          error=str(st or "summary did not resolve")[:300])
    finally:
        _SUMMARIZE_SLOTS.release()


def _run_batch_item(bid: str, idx: int) -> None:
    with _BATCH_LOCK:
        b = _BATCHES.get(bid)
        item = (b["items"][idx]
                if b and 0 <= idx < len(b["items"]) else None)
        if not item or item["status"] != "queued":
            return                    # cancelled (or gone) while waiting
        sid, action, cwd = item["session_id"], item["action"], item.get("cwd")
        force = bool(item.get("force"))
    _batch_update(bid, idx, status="running", started_at=time.time())
    try:
        if action == "angles":
            r = mine_angles(sid)
            if not r.get("ok"):
                raise RuntimeError(r.get("error") or "mine failed")
        elif action == "timeline":
            p = find_session(sid)
            if p is None:
                raise RuntimeError("transcript not found")
            session_timeline.payload(sid, p, force=True)   # enqueue, one lane
            t0 = time.time()
            while True:
                st = session_timeline.STATUS.get(sid) or ""
                if st == "ok":
                    break
                if st and not st.startswith(("queued", "generating")):
                    raise RuntimeError(st[:300])
                if time.time() - t0 > BATCH_TIMELINE_WAIT_S:
                    raise RuntimeError(
                        f"timeline still running after {BATCH_TIMELINE_WAIT_S}s")
                time.sleep(2)
        elif action == "tldr":
            p = find_session(sid)
            if p is None:
                raise RuntimeError("transcript not found")
            key = tldr.turn_key(p)
            if key is None:
                raise RuntimeError("no user prompt to digest")
            cached = tldr.get_cached(sid)
            if not force and cached and cached.get("turn_key") == key:
                # Fresh: a real store is a skip, a negative-cache stub is a
                # known failure — surface it without re-running the model.
                if cached.get("headline"):
                    _batch_update(bid, idx, status="done", skipped=True,
                                  ended_at=time.time())
                    return
                raise RuntimeError(
                    (cached.get("error") or "cached failure")[:250]
                    + " (cached; force re-runs)")
            tldr.enqueue(sid, p)      # tldr's own single lane, force path
            t0 = time.time()
            while True:
                st = tldr.STATUS.get(sid) or ""
                if st == "ok":
                    break
                if st and st not in ("queued", "generating"):
                    raise RuntimeError(st[:300])
                if time.time() - t0 > BATCH_TLDR_WAIT_S:
                    raise RuntimeError(
                        f"tldr still running after {BATCH_TLDR_WAIT_S}s")
                time.sleep(1)
        elif action == "summarize":
            _SUMMARIZE_SLOTS.acquire()   # bound concurrent claude -p children
            try:
                r = summarize_session(sid, cwd or "")
            except BaseException:
                _SUMMARIZE_SLOTS.release()
                raise
            if not r.get("ok"):
                _SUMMARIZE_SLOTS.release()
                raise RuntimeError(r.get("error") or "dispatch failed")
            threading.Thread(target=_watch_batch_summary, args=(bid, idx, sid),
                             daemon=True,
                             name=f"batch-summary-{sid[:8]}").start()
            return                    # the watcher resolves done/failed
        else:
            raise RuntimeError(f"unknown action {action!r}")
        _batch_update(bid, idx, status="done", ended_at=time.time())
    except Exception as exc:  # noqa: BLE001 — one bad item ≠ dead batch
        _batch_update(bid, idx, status="failed", error=str(exc)[:300],
                      ended_at=time.time())


def _batch_worker_loop() -> None:
    while True:
        bid, idx = _BATCH_JOBS.get()
        try:
            _run_batch_item(bid, idx)
        except Exception:  # noqa: BLE001 — the worker must never die
            pass


def _ensure_batch_worker() -> None:
    global _BATCH_WORKER
    with _BATCH_LOCK:
        if _BATCH_WORKER is None or not _BATCH_WORKER.is_alive():
            _BATCH_WORKER = threading.Thread(target=_batch_worker_loop,
                                             daemon=True, name="batch-worker")
            _BATCH_WORKER.start()


def open_session_ids() -> tuple:
    """(ids, error) — sessions the mgmt lens calls open (not CLOSED), minus
    archived. The same verdicts the threads overlay shows."""
    m = mgmt_payload(7, 15)
    if m.get("error"):
        return [], m["error"]
    archived = _read_archive()
    ids = [r["session_id"] for r in m.get("sessions", [])
           if r.get("verdict") in OPEN_VERDICTS
           and r["session_id"] not in archived]
    return ids, None


def create_batch(actions, session_ids, scope=None, options=None) -> tuple:
    """(payload, http_code). Expands actions × sessions into queued items.

    options.force (bool) applies to tldr items only: regenerate even when the
    cached tldr's turn_key still matches (default is skip-if-fresh)."""
    force = bool(options.get("force")) if isinstance(options, dict) else False
    acts = [a for a in (actions or []) if a in BATCH_ACTIONS]
    if not acts:
        return {"error": f"actions must be a non-empty subset of "
                         f"{list(BATCH_ACTIONS)}"}, 400
    if scope == "open":
        ids, err = open_session_ids()
        if err:
            return {"error": f"open-scope unavailable: {err}"}, 502
    else:
        ids = [s for s in (session_ids or [])
               if isinstance(s, str) and s.strip()]
    seen: set = set()
    ids = [s for s in ids if not (s in seen or seen.add(s))]
    if not ids:
        return {"error": "no sessions selected"}, 400
    items = []
    for act in acts:
        for sid in ids:
            if act in ("summarize", "timeline") and ":" in sid:
                continue              # child transcripts: parent's business
            cwd = None
            if act == "summarize":
                p = find_session(sid)
                cwd = _session_cwd(p) if p else None
            it = {"session_id": sid, "action": act, "cwd": cwd,
                  "status": "queued", "error": None}
            if act == "tldr":
                it["force"] = force
            items.append(it)
    if not items:
        return {"error": "nothing to do for the selected sessions"}, 400
    if len(items) > BATCH_MAX_ITEMS:
        return {"error": f"batch too large (> {BATCH_MAX_ITEMS} items)"}, 400
    bid = time.strftime("b%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    batch = {"id": bid, "actions": acts, "scope": scope or "explicit",
             "created_at": time.time(), "done": False, "items": items}
    with _BATCH_LOCK:
        _BATCHES[bid] = batch
        _persist_batches_locked()
    for i in range(len(items)):
        _BATCH_JOBS.put((bid, i))
    _ensure_batch_worker()
    return {"ok": True, "batch": batch}, 200


def cancel_batch(bid: str) -> dict:
    """Cancel every still-queued item; running items finish on their own."""
    with _BATCH_LOCK:
        b = _BATCHES.get(bid)
        if not b:
            return {"ok": False, "error": "no such batch"}
        n = 0
        for it in b["items"]:
            if it["status"] == "queued":
                it["status"] = "cancelled"
                n += 1
        b["done"] = all(i["status"] in ("done", "failed", "cancelled")
                        for i in b["items"])
        if b["done"] and not b.get("ended_at"):
            b["ended_at"] = time.time()
        _persist_batches_locked()
    return {"ok": True, "cancelled": n}


def batches_payload() -> dict:
    with _BATCH_LOCK:
        out = sorted(_BATCHES.values(),
                     key=lambda b: b.get("created_at") or 0, reverse=True)
        return json.loads(json.dumps({"batches": out}))   # detached copy


def _resume_batches() -> None:
    """Reload batch.json at startup: re-enqueue queued items (the queue.json
    doctrine — restart-safe), mark items caught mid-run as failed rather than
    silently re-running them (summarize archives; a re-run must be explicit)."""
    try:
        d = json.loads(BATCH_FILE.read_text())
        stored = d.get("batches") or []
    except (OSError, ValueError):
        return
    requeue = []
    with _BATCH_LOCK:
        for b in stored:
            if not isinstance(b, dict) or not b.get("id"):
                continue
            for i, it in enumerate(b.get("items") or []):
                if it.get("status") == "running":
                    it.update(status="failed",
                              error="interrupted by console restart")
                elif it.get("status") == "queued":
                    requeue.append((b["id"], i))
            b["done"] = all(i.get("status") in ("done", "failed", "cancelled")
                            for i in b.get("items") or [])
            _BATCHES[b["id"]] = b
        if _BATCHES:
            _persist_batches_locked()
    for job in requeue:
        _BATCH_JOBS.put(job)
    if requeue:
        _ensure_batch_worker()


# ----------------------------------------------------------------------------
# git tab — per-session repository status
#
# Read-only, lazy, and timeout-bounded: the endpoint resolves the session's cwd
# from its transcript, shells out to git (status --porcelain, rev-parse, log,
# stash list — NEVER a write command), and caches the snapshot per cwd with a
# short TTL so tab polling doesn't hammer the repo. `gh pr list` is slower and
# rate-limited, so PR data caches per repo root with a much longer TTL and a
# refresh-on-demand path (?refresh=1 busts both caches). Every subprocess call
# carries a timeout so a hung repo (network FS etc.) can't stall the console.
#
# Session-window commit attribution is best-effort by construction: commits are
# flagged by whether their committer timestamp falls inside the transcript's
# [started_at, last activity + margin] span — the UI labels them "commits in
# session window", not "commits made by this session".
# ----------------------------------------------------------------------------
GIT_TIMEOUT_S = 3
GH_TIMEOUT_S = 10
GIT_TTL_S = 12
GH_TTL_S = 300
# Well-known install dirs probed when `gh` is not on PATH. A launchd-parented
# console inherits the bare default PATH (`/usr/bin:/bin:/usr/sbin:/sbin`), so
# `shutil.which` alone reports a Homebrew gh as "not installed" — the same
# failure mode the `claude` resolver in spawn_claude() already guards against.
GH_PROBE_DIRS = ("/opt/homebrew/bin", "/usr/local/bin",
                 str(Path.home() / ".local" / "bin"))


def _gh_bin() -> str | None:
    """Resolve the `gh` binary: $CSD_GH_BIN, then PATH, then GH_PROBE_DIRS."""
    env = os.environ.get("CSD_GH_BIN")
    if env and Path(env).is_file():
        return env
    found = shutil.which("gh")
    if found:
        return found
    for d in GH_PROBE_DIRS:
        cand = Path(d) / "gh"
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None
GIT_LIST_CAP = 40          # max dirty/untracked paths returned per list
GIT_LOG_N = 30             # recent commits scanned for window flagging
GIT_WINDOW_END_MARGIN_S = 120
_GIT_CACHE: dict[str, tuple] = {}   # cwd -> (expires_at, snapshot)
_GH_CACHE: dict[str, tuple] = {}    # repo root -> (expires_at, payload)
_GIT_LOCK = threading.Lock()
_FS = "\x1f"               # field separator for git log formats


def _git(args, cwd):
    """(rc, stdout) for a READ-ONLY git command; (None, "") on timeout/error.

    --no-optional-locks keeps even `status` from touching the index, so the
    console never writes into a repo it is merely observing.
    """
    try:
        p = subprocess.run(["git", "--no-optional-locks"] + list(args),
                           cwd=cwd, capture_output=True, text=True,
                           timeout=GIT_TIMEOUT_S)
        return p.returncode, p.stdout
    except (subprocess.TimeoutExpired, OSError):
        return None, ""


def _iso_epoch(ts):
    """Epoch seconds out of an ISO timestamp (Z or offset), or None."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _git_snapshot(cwd: str) -> dict:
    """Repo identity + working-tree + branch snapshot for one cwd (uncached).

    Non-repo cwds return {"repo": None}; a missing directory or a git that
    times out degrades to an explanatory field, never an exception.
    """
    if not cwd or not Path(cwd).is_dir():
        return {"cwd": cwd, "cwd_exists": False, "repo": None}
    rc, top = _git(["rev-parse", "--show-toplevel"], cwd)
    if rc is None:
        return {"cwd": cwd, "cwd_exists": True, "repo": None,
                "git_error": f"git timed out after {GIT_TIMEOUT_S}s"}
    if rc != 0:
        return {"cwd": cwd, "cwd_exists": True, "repo": None}
    root = top.strip()

    # worktree detection: a linked worktree's .git is a FILE pointing at the
    # parent repo's .git/worktrees/<name>; git-common-dir names the parent.
    _, dirs = _git(["rev-parse", "--git-dir", "--git-common-dir"], root)
    lines = dirs.strip().split("\n")
    git_dir = str((Path(root) / lines[0]).resolve()) if lines and lines[0] else ""
    common = (str((Path(root) / lines[1]).resolve())
              if len(lines) > 1 and lines[1] else git_dir)
    is_worktree = bool(git_dir and common and git_dir != common)
    parent_root = None
    if is_worktree and common.endswith("/.git"):
        parent_root = common[:-len("/.git")]
    elif is_worktree:
        parent_root = str(Path(common).parent)

    rc_b, br = _git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    branch = br.strip() if rc_b == 0 else None
    detached = branch == "HEAD"
    if detached:
        _, sha = _git(["rev-parse", "--short", "HEAD"], root)
        branch = sha.strip() or None

    # working tree: one porcelain pass — tracked changes vs untracked
    dirty, untracked = [], []
    rc_s, out = _git(["status", "--porcelain"], root)
    for ln in (out.splitlines() if rc_s == 0 else []):
        if len(ln) < 4:
            continue
        flags, path_ = ln[:2], ln[3:]
        (untracked if flags == "??" else dirty).append(
            {"flags": flags.strip(), "path": path_})

    _, stash = _git(["stash", "list", "--format=%gd"], root)
    stash_count = len([x for x in stash.splitlines() if x.strip()])

    upstream = ahead = behind = None
    rc_u, up = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name",
                     "@{upstream}"], root)
    if rc_u == 0:
        upstream = up.strip()
        rc_c, cnt = _git(["rev-list", "--left-right", "--count",
                          "HEAD...@{upstream}"], root)
        if rc_c == 0 and cnt.strip():
            parts = cnt.split()
            if len(parts) == 2:
                ahead, behind = int(parts[0]), int(parts[1])

    # recent commits on HEAD, oldest data the window flagging needs
    commits = []
    rc_l, log = _git(["log", f"-{GIT_LOG_N}",
                      f"--format=%h{_FS}%s{_FS}%cI{_FS}%an"], root)
    for ln in (log.splitlines() if rc_l == 0 else []):
        p = ln.split(_FS)
        if len(p) == 4:
            commits.append({"hash": p[0], "subject": p[1], "when": p[2],
                            "author": p[3], "epoch": _iso_epoch(p[2])})

    return {
        "cwd": cwd, "cwd_exists": True,
        "repo": {
            "root": root, "branch": branch, "detached": detached,
            "is_worktree": is_worktree, "parent_root": parent_root,
        },
        "status": {
            "dirty_count": len(dirty), "dirty": dirty[:GIT_LIST_CAP],
            "untracked_count": len(untracked),
            "untracked": untracked[:GIT_LIST_CAP],
            "truncated": max(len(dirty), len(untracked)) > GIT_LIST_CAP,
            "stash_count": stash_count,
        },
        "branch_status": {
            "upstream": upstream, "ahead": ahead, "behind": behind,
            "last_commit": commits[0] if commits else None,
        },
        "commits": commits,
    }


def _cached_snapshot(cwd: str, refresh: bool) -> dict:
    now = time.time()
    with _GIT_LOCK:
        hit = _GIT_CACHE.get(cwd)
        if hit and not refresh and hit[0] > now:
            return hit[1]
    snap = _git_snapshot(cwd)
    with _GIT_LOCK:
        _GIT_CACHE[cwd] = (now + GIT_TTL_S, snap)
    return snap


_CHECK_FAIL = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED",
               "STARTUP_FAILURE"}
_CHECK_PEND = {"PENDING", "EXPECTED", "IN_PROGRESS", "QUEUED", "REQUESTED",
               "WAITING"}


def _checks_rollup(rollup) -> str | None:
    """Collapse a statusCheckRollup list to fail/pending/pass; None if no checks.

    Entries are CheckRun ({status, conclusion}) or StatusContext ({state});
    any failure-ish verdict wins, then any still-running one.
    """
    if not rollup:
        return None
    verdicts = []
    for c in rollup:
        if not isinstance(c, dict):
            continue
        v = (c.get("conclusion") or c.get("state") or c.get("status") or "")
        verdicts.append(str(v).upper())
    if not verdicts:
        return None
    if any(v in _CHECK_FAIL for v in verdicts):
        return "fail"
    if any(v in _CHECK_PEND or v == "" for v in verdicts):
        return "pending"
    return "pass"


def _gh_prs(root: str, refresh: bool) -> dict:
    """PR listing for the repo (open AND recently closed/merged), cached (GH_TTL_S).

    `local` marks PRs whose head branch exists in this clone — the ones the
    operator's sessions could have produced; open-and-local sort first.
    Each row keeps the PR's commit oids (`oids`, full hashes) so callers can
    attribute repo commits to the PR that carries them, plus a `checks`
    rollup (pass/fail/pending) and `merged_at`.
    """
    now = time.time()
    with _GIT_LOCK:
        hit = _GH_CACHE.get(root)
        if hit and not refresh and hit[0] > now:
            return hit[1]
    gh = _gh_bin()
    if not gh:
        # Not a claim about the machine — only about what this process can see.
        payload = {"available": False,
                   "reason": "gh not found on the console's PATH "
                             f"({os.environ.get('PATH', '')}); "
                             "set CSD_GH_BIN or fix the launcher's PATH"}
    else:
        rc, url = _git(["remote", "get-url", "origin"], root)
        if rc != 0 or "github" not in (url or ""):
            payload = {"available": False, "reason": "no GitHub origin remote"}
        else:
            try:
                p = subprocess.run(
                    [gh, "pr", "list", "--state", "all", "--json",
                     "number,title,state,isDraft,headRefName,url,mergedAt,"
                     "statusCheckRollup,commits",
                     "--limit", "30"],
                    cwd=root, capture_output=True, text=True,
                    timeout=GH_TIMEOUT_S)
                if p.returncode == 0:
                    _, refs = _git(["for-each-ref", "refs/heads",
                                    "--format=%(refname:short)"], root)
                    local = set(refs.split())
                    rows = [{"number": r.get("number"), "title": r.get("title"),
                             "state": r.get("state"), "draft": r.get("isDraft"),
                             "branch": r.get("headRefName"),
                             "url": r.get("url"),
                             "merged_at": r.get("mergedAt"),
                             "checks": _checks_rollup(r.get("statusCheckRollup")),
                             "oids": [c.get("oid") for c in (r.get("commits") or [])
                                      if isinstance(c, dict) and c.get("oid")],
                             "subs": [c.get("messageHeadline") or ""
                                      for c in (r.get("commits") or [])
                                      if isinstance(c, dict)],
                             "local": r.get("headRefName") in local}
                            for r in json.loads(p.stdout or "[]")]
                    rows.sort(key=lambda r: (
                        r["state"] != "OPEN",
                        not r["local"] if r["state"] == "OPEN" else False,
                        -(r["number"] or 0)))
                    payload = {"available": True, "prs": rows,
                               "fetched_at": now}
                else:
                    payload = {"available": True, "prs": [],
                               "error": (p.stderr or "").strip()[:200]}
            except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
                payload = {"available": True, "prs": [],
                           "error": f"gh: {type(exc).__name__}: {exc}"[:200]}
    with _GIT_LOCK:
        _GH_CACHE[root] = (now + GH_TTL_S, payload)
    return payload


def _pr_ref(pr: dict) -> dict:
    """The compact commit-side annotation: which PR a commit belongs to."""
    return {"number": pr["number"], "state": pr["state"],
            "url": pr["url"], "checks": pr["checks"]}


_EQUIV_MIN_SUBJECT = 20   # chars — generic subjects ("fix typo") never ≈-match


def _attribute_commits_to_prs(snap: dict, gh: dict):
    """Stamp every commit the payload surfaces with the PR that carries it.

    Matching, strongest first:
    1. `Merge pull request #N` subject — the base-branch merge commit that
       landed a PR (it is not part of the PR's own commits).
    2. oid-prefix — the commit object itself is in the PR (snapshot hashes are
       abbreviated %h, PR oids are full).
    3. subject equivalence (`equiv: true`) — a PR commit carries the exact
       same subject under a DIFFERENT sha, i.e. the change was cherry-picked
       onto the PR's branch (the workbench-branch flow). Guarded by a minimum
       subject length so boilerplate subjects can't false-positive; ties go to
       the newest PR. The UI renders this as ≈#N, distinct from membership.

    Commits matching nothing get pr=None — "not part of any PR".
    """
    prs = gh.get("prs") or []
    by_num = {p["number"]: p for p in prs}
    merge_re = re.compile(r"^Merge pull request #(\d+)\b")
    by_subject = {}
    for p in prs:
        for s in p.get("subs", ()):
            if len(s) >= _EQUIV_MIN_SUBJECT:
                cur = by_subject.get(s)
                if cur is None or (p["number"] or 0) > (cur["number"] or 0):
                    by_subject[s] = p

    def find(c):
        h, subj = c.get("hash") or "", c.get("subject") or ""
        m = merge_re.match(subj)
        if m and int(m.group(1)) in by_num:
            return _pr_ref(by_num[int(m.group(1))])
        if h:
            for p in prs:
                if any(o.startswith(h) for o in p.get("oids", ())):
                    return _pr_ref(p)
        p = by_subject.get(subj)
        if p is not None:
            return {**_pr_ref(p), "equiv": True}
        return None

    for c in (snap.get("session_window") or {}).get("commits", []):
        c["pr"] = find(c)
    last = (snap.get("branch_status") or {}).get("last_commit")
    if last:
        last["pr"] = find(last)


# ---- commit grouping: which BRANCH each commit is part of -------------------
# A flat `git log` interleaves every branch that ever landed, so a rail row can
# say WHAT happened but never *as part of what*. Grouping is deterministic code,
# never a model: git already records the answer.
#
#   1. Trunk is RESOLVED (_trunk_of: origin/HEAD, then a main/master/trunk
#      probe), never guessed — the same rule the repos lens follows.
#   2. `log --first-parent` IS a branch's own line. Every commit on it belongs
#      to the branch it was logged from; that claim wins over every other rule.
#   3. Each merge M on that line pulled in a side branch, and `rev-list M^1..M^2`
#      enumerates exactly the commits it carried. The group's NAME comes from
#      the PR whose oids cover them (preferred — it survives `--delete-branch`),
#      else from M's subject, else "(merged branch)".
#   4. A commit still unclaimed but reachable from a live local branch belongs
#      to that branch (`trunk..branch`); one exclusive to several goes to the
#      branch whose tip is nearest, listed once, never duplicated.
#   5. Everything left stays with the current branch.
#
# The request path never fans out: the git side is a per-root TOPOLOGY cached on
# the same TTL as the snapshot, and grouping a commit list against it is pure set
# arithmetic. Every rev-list is bounded, every call goes through _git()
# (read-only, --no-optional-locks, timeout), and the whole thing is failure-
# isolated — an underivable topology returns a `group_note` and the caller falls
# back to the flat list it already ships. It never raises.

GROUP_FIRST_PARENT_N = 200   # commits of the current branch's own line scanned
GROUP_REVLIST_CAP = 200      # side commits enumerated per merge / per branch
GROUP_MERGE_CAP = 20         # merges expanded per topology
GROUP_BRANCH_CAP = 15        # live local branches probed for orphaned commits
# A topology is ~2 + one rev-list per merge and per candidate branch — the most
# expensive read in the rail. It is also the slowest-changing thing in the repo
# (the working tree moves every keystroke; the branch shape moves when you
# merge), so it gets its own TTL, well above the snapshot's, and ⟳ busts it.
TOPO_TTL_S = 90

_TOPO_CACHE: dict[str, tuple] = {}   # repo root -> (expires_at, topology)

_MERGE_PR_RE = re.compile(r"^Merge pull request #\d+ from [^/\s]+/(\S+)")
_MERGE_BRANCH_RE = re.compile(r"^Merge (?:remote-tracking )?branch '([^']+)'")


def _merged_branch_from_subject(subject: str):
    """The side-branch name a merge commit's subject names, or None.

    Covers both spellings git and gh produce: `Merge pull request #N from
    owner/branch`, and `Merge branch 'x'` / `Merge remote-tracking branch
    'origin/x'`. A remote prefix is stripped so `origin/feat/x` and `feat/x`
    are one group, not two.
    """
    s = subject or ""
    m = _MERGE_PR_RE.match(s)
    if m:
        return m.group(1)
    m = _MERGE_BRANCH_RE.match(s)
    if m:
        name = m.group(1)
        head, _, rest = name.partition("/")
        return rest if rest and head in ("origin", "upstream") else name
    return None


def _head_branch_of(root: str):
    rc, br = _git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    name = br.strip() if rc == 0 else ""
    return None if not name or name == "HEAD" else name


def _git_topology(root: str, head_branch: str | None = None) -> dict:
    """The branch shape of one repo: the current line, its merges, live branches.

    Uncached and bounded. Partial failure fills `note` rather than raising — a
    topology that lost its branch list still groups the merges.
    """
    trunk, _ = _trunk_of(root)
    topo = {"trunk": trunk, "head_branch": head_branch, "line": [],
            "merges": [], "branches": [], "worktrees": {},
            "remote_names": [], "note": None}

    rc, out = _git(["log", "--first-parent", f"-{GROUP_FIRST_PARENT_N}",
                    f"--format=%h{_FS}%H{_FS}%p{_FS}%s"], root)
    if rc != 0:
        topo["note"] = "the current branch's first-parent line is unreadable"
        return topo
    merge_refs = []
    for ln in out.splitlines():
        p = ln.split(_FS, 3)
        if len(p) < 4:
            continue
        topo["line"].append(p[0])
        if len(p[2].split()) > 1:
            merge_refs.append((p[0], p[1], p[3]))

    # Worktrees and remote refs: one call each, for the group headers. A branch
    # is linked out to GitHub only when a remote actually has it — a local-only
    # name linked at /tree/<name> is a 404 wearing a hyperlink.
    for w in _worktree_inventory(root, cap=10_000)[0]:
        if w.get("branch"):
            topo["worktrees"].setdefault(w["branch"], w.get("path"))
    rc_r, refs = _git(["for-each-ref", "refs/remotes",
                       "--format=%(refname:short)"], root)
    if rc_r == 0:
        topo["remote_names"] = [r for r in refs.split()
                                if not r.endswith("/HEAD")]

    for mh, mfull, subj in merge_refs[:GROUP_MERGE_CAP]:
        rc_s, side = _git(["rev-list", f"--max-count={GROUP_REVLIST_CAP}",
                           f"{mfull}^1..{mfull}^2"], root)
        if rc_s != 0:
            continue
        full = [s for s in side.split() if s]
        if not full:
            continue
        topo["merges"].append({"hash": mh, "subject": subj,
                               "name": _merged_branch_from_subject(subj),
                               "side": full})

    if trunk:
        branches, _, note = _branch_inventory(root, trunk, cap=10_000)
        if note:
            topo["note"] = note
        # Only a branch carrying work the trunk lacks can own an unclaimed
        # commit; the rest cost a rev-list to learn nothing.
        cands = [b for b in branches
                 if not b["is_trunk"] and b["name"] != head_branch
                 and (b["ahead"] is None or b["ahead"] > 0)][:GROUP_BRANCH_CAP]
        for b in cands:
            rc_b, out_b = _git(["rev-list", f"--max-count={GROUP_REVLIST_CAP}",
                                f"{trunk}..{b['name']}"], root)
            if rc_b != 0:
                continue
            topo["branches"].append({
                "name": b["name"], "ahead": b["ahead"], "behind": b["behind"],
                "upstream": b["upstream"],
                "hashes": [h for h in out_b.split() if h]})
    return topo


def _cached_topology(root: str, refresh: bool) -> dict:
    now = time.time()
    with _GIT_LOCK:
        hit = _TOPO_CACHE.get(root)
        if hit and not refresh and hit[0] > now:
            return hit[1]
    topo = _git_topology(root, _head_branch_of(root))
    with _GIT_LOCK:
        _TOPO_CACHE[root] = (now + TOPO_TTL_S, topo)
    return topo


def _remote_name_for(topo: dict, branch: str | None):
    """The branch's name ON a remote (so the UI may link it), else None.

    origin wins when several remotes carry the same branch; a branch no remote
    has returns None and renders as plain text.
    """
    if not branch:
        return None
    matches = [r.partition("/") for r in (topo.get("remote_names") or [])]
    for rem, _, rest in matches:
        if rest == branch and rem == "origin":
            return rest
    for _, _, rest in matches:
        if rest == branch:
            return rest
    return None


def group_commits(commits, topo: dict, gh: dict | None = None):
    """(groups, note) — a flat commit list partitioned by its owning branch.

    Pure set arithmetic over a cached topology: no git, no exceptions. Each
    commit dict is carried BY REFERENCE, so whatever the caller already stamped
    on it (pr, pushed, refs, is_merge) rides along untouched and the flat list
    and the groups can never disagree about a commit.

    Groups come back in date order of their newest commit, the current branch's
    own line always first.
    """
    try:
        return _group_commits(commits, topo, gh)
    except Exception as exc:                      # never break the rail
        return [], f"grouping failed: {type(exc).__name__}: {exc}"[:200]


def _group_commits(commits, topo, gh):
    rows = [c for c in (commits or []) if c.get("hash")]
    if not rows:
        return [], topo.get("note")
    head_name = topo.get("head_branch") or topo.get("trunk") or "HEAD"

    # Rule 2: the first-parent line is the current branch's, full stop. Only
    # what is NOT on it is up for grabs.
    on_line = set(topo.get("line") or ())
    pool = {c["hash"] for c in rows if c["hash"] not in on_line}
    lens = sorted({len(h) for h in pool})
    best: dict[str, tuple] = {}      # hash -> (priority, distance, group key)
    meta: dict[str, dict] = {}       # group key -> header fields

    def claim(full_hashes, key, header, priority):
        """Offer a group's commits to the pool; the nearest owner wins each hash.

        Snapshot hashes are abbreviated (%h) and these are full oids, so the
        match is a prefix test. `priority` ranks the RULE (a merge's side list
        is definitive, a branch's reachability is not) and `distance` the
        position from the branch tip, so a commit on two branches lands under
        the nearer one — exactly once.
        """
        used = False
        for i, f in enumerate(full_hashes):
            h = next((f[:n] for n in lens if f[:n] in pool), None)
            if h is None:
                continue
            used = True
            cur = best.get(h)
            if cur is None or (priority, i) < cur[:2]:
                best[h] = (priority, i, key)
        if used:
            meta.setdefault(key, header)

    prs = (gh or {}).get("prs") or []
    for m in topo.get("merges") or ():
        side = set(m.get("side") or ())
        pr = next((p for p in prs
                   if any(o in side for o in (p.get("oids") or ()))), None)
        # headRefName first: it is the only name that survives --delete-branch.
        name = ((pr.get("branch") if pr else None) or m.get("name")
                or "(merged branch)")
        claim(m.get("side") or [], f"merge:{m['hash']}", {
            "branch": name, "merged": True, "merge_hash": m["hash"],
            "pr": _pr_ref(pr) if pr else None,
            "worktree": (topo.get("worktrees") or {}).get(name),
            "remote_name": _remote_name_for(topo, name),
            "ahead": None, "behind": None}, 0)

    for b in topo.get("branches") or ():
        claim(b.get("hashes") or [], f"branch:{b['name']}", {
            "branch": b["name"], "merged": False, "merge_hash": None,
            "pr": None,
            "worktree": (topo.get("worktrees") or {}).get(b["name"]),
            "remote_name": (_remote_name_for(topo, b["name"])
                            or (b["upstream"].partition("/")[2]
                                if b.get("upstream") else None)),
            "ahead": b.get("ahead"), "behind": b.get("behind")}, 1)

    head_key = "\x00head"
    meta[head_key] = {
        "branch": head_name, "merged": False, "merge_hash": None, "pr": None,
        "worktree": (topo.get("worktrees") or {}).get(head_name),
        "remote_name": _remote_name_for(topo, head_name),
        "ahead": None, "behind": None}

    owner = {h: v[2] for h, v in best.items()}
    buckets: dict[str, list] = {}
    for c in rows:                                # input order IS date order
        buckets.setdefault(owner.get(c["hash"], head_key), []).append(c)

    groups = []
    for key, items in buckets.items():
        groups.append({**meta[key], "current": key == head_key,
                       "count": len(items), "commits": items,
                       "_newest": _iso_epoch(items[0].get("when")) or 0})
    groups.sort(key=lambda g: (not g["current"], -g["_newest"]))
    for g in groups:
        g.pop("_newest")
    return groups, topo.get("note")


def git_payload(sid: str, refresh: bool = False):
    """(payload, code) for GET /api/git — repo status through the session lens."""
    path = find_session(sid)
    if path is None:
        return {"error": "session not found"}, 404
    # cwd from the transcript tail — the same derivation the nav uses
    cwd = None
    for r in tail_records(path, NAV_TAIL_BYTES):
        if r.get("type") in ("user", "assistant") and r.get("cwd"):
            cwd = r["cwd"]
    if not cwd:
        return {"cwd": None, "repo": None,
                "error": "no cwd recorded in this transcript"}, 200

    snap = dict(_cached_snapshot(cwd, refresh))

    # session window: transcript start -> last append (+margin), commits flagged
    started = _iso_epoch(_nav_stats(path)["started_at"])
    try:
        ended = path.stat().st_mtime
    except OSError:
        ended = time.time()
    window = {"started_at": _nav_stats(path)["started_at"],
              "ended_epoch": ended, "commits": []}
    commits = snap.pop("commits", [])
    if started:
        end = ended + GIT_WINDOW_END_MARGIN_S
        for c in commits:
            if c.get("epoch") and started <= c["epoch"] <= end:
                window["commits"].append(c)
    snap["session_window"] = window

    if snap.get("repo"):
        root = snap["repo"]["root"]
        gh = _gh_prs(root, refresh)
        # only stamp pr/None on commits when a real listing was fetched —
        # otherwise "no PR" would be indistinguishable from "gh unavailable"
        if gh.get("available") and not gh.get("error"):
            _attribute_commits_to_prs(snap, gh)
        # Group AFTER attribution: the group carries the very same commit dicts
        # the flat list does, so the `pr` stamp rides along by reference.
        window["groups"], window["group_note"] = group_commits(
            window["commits"], _cached_topology(root, refresh), gh)
        snap["web"] = _web_url_cached(root)
        # ship the listing without the oid payload; flag the session branch's PR
        branch = snap["repo"].get("branch")
        out = dict(gh)
        out["prs"] = [
            {**{k: v for k, v in p.items() if k not in ("oids", "subs")},
             "session_branch": bool(branch) and p.get("branch") == branch}
            for p in (gh.get("prs") or [])]
        snap["gh"] = out
    snap["generated_at"] = time.time()
    return snap, 200


# ----------------------------------------------------------------------------
# repos lens — the cross-REPO inventory (/api/repos)
#
# /api/git is a SESSION lens: one cwd, derived from one transcript, one repo.
# This is the other axis — every repo the operator's sessions have touched,
# each answering the same questions at once: what is the trunk, is it level
# with its remote, what is dirty, which branches are unmerged, which worktrees
# are still mounted. The threads overlay is this shape for sessions; this is it
# for repos.
#
# Doctrine, inherited from session_mgmt.py and the angles state dir:
#
#   - READ-ONLY, and never a fetch. Every git call goes through _git(), which
#     passes --no-optional-locks, so observing a repo cannot write into it.
#     ahead/behind is measured against the remote-tracking refs ALREADY on
#     disk; a stale origin/main reports stale numbers rather than the console
#     reaching over the network on a poll. The payload says so (`no_fetch`).
#   - The registry is DISCOVERED, not configured: distinct `sessions.cwd` from
#     the archive, resolved through `rev-parse --show-toplevel`. A cwd that is
#     not a repo simply never appears. CSD_REPOS pins extra roots; the archive
#     being unreachable degrades to the transcript-tail derivation the nav
#     already uses, so the lens works with no database at all.
#   - CACHED-FIRST, pull not push. One repo snapshot is ~9 git invocations; a
#     20-repo grid on a 30s poll would be ~180 subprocesses a tick. The request
#     path NEVER fans out — it serves the store off disk, and a single
#     background refresher walks the registry one repo at a time. Same seam as
#     tldr/timeline: the endpoint reads, the worker writes.
#   - Failure isolates per ROW. A deleted directory, a git that times out, a
#     branch atom an older git does not know — each degrades that repo (or that
#     one field) to a reason string. The lens never raises.
# ----------------------------------------------------------------------------
REPOS_FILE = CONSOLE_STATE / "repos.json"
REPO_REFRESH_S = int(os.environ.get("CSD_REPOS_REFRESH_S", "90"))
REPO_REGISTRY_TTL_S = int(os.environ.get("CSD_REPOS_REGISTRY_TTL_S", "600"))
REPO_WINDOW_DAYS = int(os.environ.get("CSD_REPOS_WINDOW_DAYS", "30"))
REPO_STAGGER_S = float(os.environ.get("CSD_REPOS_STAGGER_S", "0.35"))
REPO_MAX = 40              # registry cap — the fan-out is bounded by design
REPO_BRANCH_CAP = 14       # branches shown per repo, most recent first
REPO_WORKTREE_CAP = 12
_REPOS_LOCK = threading.Lock()
_REPOS: dict = {}          # the served store, mirrored to REPOS_FILE
_REGISTRY: tuple = (0.0, [], "")   # (discovered_at, [roots], source)


def _repo_toplevel(cwd: str):
    """Repo root for a cwd, or None — a linked worktree folded into its PARENT.

    A worktree is a second checkout, not a second repository. Left un-folded it
    lands in the grid as its own row carrying its parent's branch and worktree
    counts, so `controltech` and its `receive-packing-slip-cli` worktree both
    claim the same 22 branches. One row per repository; the worktrees are a
    field on that row (same derivation _git_snapshot uses for parent_root).
    """
    if not cwd or not Path(cwd).is_dir():
        return None
    rc, top = _git(["rev-parse", "--show-toplevel"], cwd)
    if rc != 0 or not top.strip():
        return None
    root = top.strip()
    rc2, dirs = _git(["rev-parse", "--git-dir", "--git-common-dir"], root)
    lines = dirs.strip().split("\n") if rc2 == 0 else []
    if len(lines) < 2 or not lines[0] or not lines[1]:
        return root
    git_dir = str((Path(root) / lines[0]).resolve())
    common = str((Path(root) / lines[1]).resolve())
    if git_dir == common:                   # the main checkout — already the root
        return root
    parent = (common[:-len("/.git")] if common.endswith("/.git")
              else str(Path(common).parent))
    return parent if Path(parent).is_dir() else root


def _registry_cwds() -> tuple[list[str], str]:
    """(candidate cwds, where they came from) — archive first, transcripts second.

    The archive is the better source (it knows every cwd a session ever ran in,
    not just the ones still in the 72h nav window), but it is not required: the
    transcript tail carries cwd too, which is the same derivation /api/git uses.
    """
    pinned = [p for p in os.environ.get("CSD_REPOS", "").split(":") if p.strip()]
    if CSD_DSN:
        try:
            import psycopg
            with psycopg.connect(CSD_DSN, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT DISTINCT cwd FROM sessions "
                        "WHERE cwd IS NOT NULL AND NOT is_subagent "
                        "  AND modified_at > now() - make_interval(days => %s) "
                        "ORDER BY 1", (REPO_WINDOW_DAYS,))
                    rows = [r[0] for r in cur.fetchall()]
            return pinned + rows, f"archive ({REPO_WINDOW_DAYS}d)"
        except Exception:  # noqa: BLE001 — degrade to transcripts, never die
            pass
    cwds = []
    for s in discover_sessions():
        p = find_session(s["session_id"])
        if p is not None:
            c = _session_cwd(p)
            if c:
                cwds.append(c)
    return pinned + cwds, "transcripts (nav window)"


def _repo_registry(force: bool = False) -> tuple[list[str], str]:
    """The repo roots to watch, cached REPO_REGISTRY_TTL_S. Ordered, deduped.

    Discovery is the expensive half (a DB round-trip, or tailing every nav
    transcript) and the answer barely moves, so it is cached far longer than
    the snapshots that hang off it.
    """
    global _REGISTRY
    now = time.time()
    with _REPOS_LOCK:
        at, roots, src = _REGISTRY
        if roots and not force and now - at < REPO_REGISTRY_TTL_S:
            return roots, src        # the real source, not "cached" — the UI
                                     # names where the list came FROM, and
                                     # "cached" answers a question nobody asked
    cwds, source = _registry_cwds()
    seen, roots = set(), []
    for c in cwds:
        root = _repo_toplevel(c)
        if root and root not in seen:
            seen.add(root)
            roots.append(root)
        if len(roots) >= REPO_MAX:
            break
    with _REPOS_LOCK:
        _REGISTRY = (now, roots, source)
    return roots, source


def _trunk_of(root: str):
    """The repo's trunk branch name — origin/HEAD if it is set, else a probe.

    Three of these repos call it `main` and the older ones call it `master`;
    guessing one is how a lens ends up reporting every branch as unmerged.
    """
    rc, out = _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], root)
    if rc == 0 and out.strip():
        return out.strip().split("/", 1)[-1], "origin/HEAD"
    for cand in ("main", "master", "trunk"):
        rc, _ = _git(["show-ref", "--verify", "--quiet",
                      f"refs/heads/{cand}"], root)
        if rc == 0:
            return cand, "probe"
    return None, "unresolved"


def _branch_inventory(root: str, trunk: str | None, cap: int | None = None):
    """Every local branch with ahead/behind vs the trunk, newest commit first.

    ONE git call: for-each-ref's %(ahead-behind:<trunk>) atom does the whole
    matrix that would otherwise be a rev-list per branch. The atom needs git
    2.41+, and an unknown atom fails the WHOLE command rather than one field —
    so a non-zero rc retries without it and reports counts as unknown instead
    of losing the branch list.
    """
    cap = REPO_BRANCH_CAP if cap is None else cap
    fmt = ("%(refname:short)" + _FS + "%(committerdate:iso-strict)" + _FS
           + "%(objectname:short)" + _FS + "%(upstream:short)" + _FS
           + "%(contents:subject)")
    ab = trunk is not None
    args = ["for-each-ref", "refs/heads", "--sort=-committerdate"]
    rc, out = _git(args + [f"--format={fmt}{_FS}%(ahead-behind:{trunk})"
                           if ab else f"--format={fmt}"], root)
    if rc != 0 and ab:                      # older git: no ahead-behind atom
        ab = False
        rc, out = _git(args + [f"--format={fmt}"], root)
    if rc != 0:
        return [], 0, "branch list unavailable"
    rows = []
    for ln in out.splitlines():
        p = ln.split(_FS)
        if len(p) < 5:
            continue
        ahead = behind = None
        if ab and len(p) > 5 and len(p[5].split()) == 2:
            ahead, behind = (int(x) for x in p[5].split())
        rows.append({"name": p[0], "when": p[1], "hash": p[2],
                     "upstream": p[3] or None, "subject": p[4],
                     "ahead": ahead, "behind": behind,
                     "is_trunk": p[0] == trunk})
    total = len(rows)
    # Surface order: the trunk, then branches carrying unmerged work, then the
    # rest — a 40-branch repo's interesting rows are never below the cap.
    rows.sort(key=lambda b: (not b["is_trunk"], -(b["ahead"] or 0)))
    return rows[:cap], total, None


def _worktree_inventory(root: str, cap: int | None = None):
    """Linked worktrees for the repo: path, branch, and whether it still exists.

    `worktree list` reports what .git/worktrees records, which is exactly the
    point — a /tmp worktree the machine has since cleaned still has an entry
    until someone prunes it, and that gap is worth seeing.
    """
    cap = REPO_WORKTREE_CAP if cap is None else cap
    rc, out = _git(["worktree", "list", "--porcelain"], root)
    if rc != 0:
        return [], 0
    trees, cur = [], {}
    for ln in out.splitlines() + [""]:
        if not ln.strip():
            if cur.get("path"):
                trees.append(cur)
            cur = {}
        elif ln.startswith("worktree "):
            cur["path"] = ln[9:]
        elif ln.startswith("branch "):
            cur["branch"] = ln[7:].replace("refs/heads/", "")
        elif ln.startswith("HEAD "):
            cur["hash"] = ln[5:][:7]
        elif ln.strip() in ("detached", "bare", "locked", "prunable"):
            cur[ln.strip()] = True
    linked = []
    for t in trees:
        if t.get("path") == root:           # the main checkout, not a worktree
            continue
        t["exists"] = Path(t["path"]).is_dir()
        linked.append(t)
    return linked[:cap], len(linked)


def repo_snapshot(root: str) -> dict:
    """One repo's row: identity + working tree + trunk + branches + worktrees.

    Built on _git_snapshot (the /api/git engine) so the two lenses can never
    disagree about the same repo; everything after it is the repo-level detail
    a single-cwd view has no reason to collect.
    """
    started = time.time()
    snap = _git_snapshot(root)
    if not snap.get("repo"):
        return {"root": root, "name": Path(root).name, "ok": False,
                "reason": (snap.get("git_error") or
                           ("directory is gone" if not snap.get("cwd_exists")
                            else "not a git repository")),
                "checked_at": started}
    trunk, trunk_from = _trunk_of(root)
    branches, branch_total, branch_note = _branch_inventory(root, trunk)
    worktrees, worktree_total = _worktree_inventory(root)

    # trunk vs its remote, which is NOT necessarily HEAD's upstream: the whole
    # point of the lens is answering "is main pushed?" while sitting on a
    # feature branch. Measured against the ref on disk — no fetch (see above).
    tr_ahead = tr_behind = None
    if trunk:
        rc, cnt = _git(["rev-list", "--left-right", "--count",
                        f"origin/{trunk}...{trunk}"], root)
        if rc == 0 and len(cnt.split()) == 2:
            tr_behind, tr_ahead = (int(x) for x in cnt.split())

    st, bs = snap["status"], snap["branch_status"]
    unmerged = [b for b in branches if not b["is_trunk"] and (b["ahead"] or 0) > 0]
    return {
        "root": root, "name": Path(root).name, "ok": True,
        "branch": snap["repo"]["branch"],
        "detached": snap["repo"]["detached"],
        "is_worktree": snap["repo"]["is_worktree"],
        "parent_root": snap["repo"]["parent_root"],
        "trunk": trunk, "trunk_from": trunk_from,
        "trunk_ahead": tr_ahead, "trunk_behind": tr_behind,
        "dirty": st["dirty_count"], "untracked": st["untracked_count"],
        "stash": st["stash_count"],
        "dirty_paths": [d["path"] for d in st["dirty"][:8]],
        "upstream": bs["upstream"], "ahead": bs["ahead"], "behind": bs["behind"],
        "last_commit": bs["last_commit"],
        "branches": branches, "branch_total": branch_total,
        "branch_note": branch_note,
        "unmerged_count": len(unmerged),
        "worktrees": worktrees, "worktree_total": worktree_total,
        "took_ms": int((time.time() - started) * 1000),
        "checked_at": started,
    }


# ---- repo detail: one repo, everything ------------------------------------
# The grid card is a glance; this is the drill-down behind the chat header's
# 📁 / ⎇ chips. Same doctrine — read-only, no fetch — widened to the whole
# repo: every branch, every worktree, recent commits ACROSS ALL REFS (not just
# HEAD's line, which hides the parallel branches that are the point), and the
# PR listing /api/git already knows how to fetch.
#
# The caller NEVER names a path. `id` resolves through the transcript (the
# /api/git derivation); `root` is accepted only when it is already in the
# registry. A repo root is a git command's cwd, so an unvalidated one is a
# path-injection surface, and the console's posture everywhere else is that no
# request parameter can move a root.
REPO_DETAIL_LOG_N = 40


def _web_url(root: str):
    """The repo's GitHub web base, or None — for linking commits/branches out.

    Handles both remote spellings (scp-style `git@host:owner/repo.git` and
    `https://host/owner/repo.git`). Anything that is not GitHub returns None
    rather than a guessed URL: a dead link is worse than a plain hash.
    """
    rc, url = _git(["remote", "get-url", "origin"], root)
    if rc != 0 or not url.strip():
        return None
    u = url.strip()
    if u.startswith("git@"):
        host, _, path = u[4:].partition(":")
    elif "://" in u:
        rest = u.split("://", 1)[1]
        if "@" in rest.split("/", 1)[0]:            # strip any userinfo
            rest = rest.split("@", 1)[1]
        host, _, path = rest.partition("/")
    else:
        return None
    if "github" not in host:
        return None
    return f"https://{host}/{path[:-4] if path.endswith('.git') else path}"


_WEB_CACHE: dict[str, str | None] = {}


def _web_url_cached(root: str):
    """_web_url memoized for the poll path — a remote URL does not change.

    /api/git is polled; the repo detail is opened. Only the former needs this,
    and an unbounded dict keyed by repo root is bounded by how many repos the
    operator has.
    """
    if root not in _WEB_CACHE:
        _WEB_CACHE[root] = _web_url(root)
    return _WEB_CACHE[root]


def _all_commits(root: str, n: int):
    """Recent commits across ALL refs, decorated, each marked pushed or not.

    HEAD's log alone hides exactly what a repo view is for — the other branches
    moving in parallel — so this is `log --all`, and %D carries the branch/tag
    names so a merge is legible as a merge.

    `pushed` is the difference between a commit GitHub can show and one that
    exists only on this machine. One extra call (`rev-list --all --not
    --remotes` = everything reachable from NO remote ref) answers it for the
    whole page, and the UI links only what is actually there — an unpushed hash
    linked to /commit/<sha> is a 404 wearing a hyperlink.
    """
    rc_u, unpushed = _git(["rev-list", "--all", "--not", "--remotes"], root)
    local_only = set(unpushed.split()) if rc_u == 0 else set()
    rc, out = _git(["log", "--all", f"-{n}", "--date-order",
                    f"--format=%h{_FS}%H{_FS}%s{_FS}%cI{_FS}%an{_FS}%D{_FS}%p"],
                   root)
    rows = []
    for ln in (out.splitlines() if rc == 0 else []):
        p = ln.split(_FS)
        if len(p) < 7:
            continue
        # origin/HEAD and upstream/HEAD are SYMBOLIC aliases for the default
        # branch, which is already in the list beside them. Kept, they render as
        # a second badge linking at /tree/HEAD — a URL that means nothing.
        refs = [r.strip() for r in p[5].split(",") if r.strip()
                and not r.strip().endswith("/HEAD")]
        rows.append({"hash": p[0], "subject": p[2], "when": p[3], "author": p[4],
                     "refs": refs, "is_merge": len(p[6].split()) > 1,
                     # rev-list is empty when it fails, which would mark
                     # everything pushed; unknown must not read as "linkable".
                     "pushed": (p[1] not in local_only) if rc_u == 0 else None})
    return rows


def _remotes(root: str):
    """Remote names, so the UI can tell a remote-tracking ref from a local one."""
    rc, out = _git(["remote"], root)
    return [r for r in out.split()] if rc == 0 else []


def repo_detail(root: str) -> dict:
    """The full drill-down for one repo. Caller-validated root only."""
    base = repo_snapshot(root)
    if not base.get("ok"):
        return base
    trunk = base["trunk"]
    # The card caps branches to keep a glance a glance; the detail is the one
    # place that shows every branch, so re-read with the cap lifted.
    branches, total, note = _branch_inventory(root, trunk, cap=10_000)
    worktrees, wt_total = _worktree_inventory(root, cap=10_000)
    commits = _all_commits(root, REPO_DETAIL_LOG_N)
    gh = _gh_prs(root, refresh=False)
    base.update({
        "detail": True,
        "web": _web_url(root),
        "branches": branches, "branch_total": total, "branch_note": note,
        "worktrees": worktrees, "worktree_total": wt_total,
        "commits": commits,
        "remotes": _remotes(root),
        "gh": gh,
    })
    # `log --all` is the most interleaved list in the console — the one place
    # grouping earns its keep. Same commit dicts, so `commits` and `groups`
    # can never drift; the flat list stays for a caller that wants it and for
    # the UI's fallback when `group_note` says the topology was unreadable.
    base["groups"], base["group_note"] = group_commits(
        commits, _cached_topology(root, refresh=False), gh)
    # The oid payload is for commit->PR attribution, not for the wire.
    if base["gh"].get("prs"):
        base["gh"] = dict(base["gh"], prs=[
            {k: v for k, v in p.items() if k not in ("oids", "subs")}
            for p in base["gh"]["prs"]])
    return base


def repo_detail_payload(sid: str, root: str):
    """(payload, code) for GET /api/repo — addressed by SESSION or by root.

    `id` is the safe address: the root is derived server-side from the
    transcript, exactly as /api/git does it. `root` exists for the grid's own
    cards and is admitted only if the registry already knows it — a root that
    is not in the registry is refused rather than handed to git as a cwd.
    """
    if sid:
        path = find_session(sid)
        if path is None:
            return {"error": "session not found"}, 404
        cwd = _session_cwd(path)
        if not cwd:
            return {"error": "no cwd recorded in this transcript"}, 404
        resolved = _repo_toplevel(cwd)
        if not resolved:
            return {"error": f"{cwd} is not inside a git repository",
                    "cwd": cwd}, 404
        return repo_detail(resolved), 200
    if not root:
        return {"error": "id or root required"}, 400
    known, _ = _repo_registry()
    if root not in known:
        known, _ = _repo_registry(force=True)   # a repo added since boot
    if root not in known:
        return {"error": "unknown repo root"}, 404
    return repo_detail(root), 200


def _read_repos_store() -> dict:
    try:
        return json.loads(REPOS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _refresh_repos(force_registry: bool = False) -> dict:
    """Walk the registry once, one repo at a time, and replace the store.

    Serial and staggered on purpose: the refresher exists so the REQUEST path
    never fans out, and a burst of 20 concurrent `git status` calls would just
    move the stampede rather than remove it.
    """
    global _REPOS
    roots, source = _repo_registry(force=force_registry)
    rows = []
    for i, root in enumerate(roots):
        try:
            rows.append(repo_snapshot(root))
        except Exception as exc:  # noqa: BLE001 — one bad repo, one bad row
            rows.append({"root": root, "name": Path(root).name, "ok": False,
                         "reason": f"{type(exc).__name__}: {exc}"[:200],
                         "checked_at": time.time()})
        if REPO_STAGGER_S and i + 1 < len(roots):
            time.sleep(REPO_STAGGER_S)
    # Attention first, recency within each band. Two stable passes, not one
    # compound key: the recency sort is what orders rows inside a band.
    def _band(r):
        """0 broken · 1 needs a decision · 2 quiet.

        Deliberately NARROW. Untracked files and months-old unmerged branches
        are the normal resting state of a working repo — banding on them put 16
        of 17 rows in the attention band, which is the same as having no band.
        What earns it: uncommitted TRACKED edits (losable), an unpushed trunk
        (invisible to everyone else), and a worktree whose folder is gone
        (git's registration outliving the directory).
        """
        if not r.get("ok"):
            return 0                        # broken — surface it, don't bury it
        stale_wt = any(not w.get("exists") for w in (r.get("worktrees") or []))
        if r.get("dirty") or r.get("trunk_ahead") or stale_wt:
            return 1
        return 2
    rows.sort(key=lambda r: ((r.get("last_commit") or {}).get("when") or ""),
              reverse=True)
    rows.sort(key=_band)
    store = {"repos": rows, "registry_source": source,
             "registry_count": len(roots), "no_fetch": True,
             "refresh_s": REPO_REFRESH_S, "generated_at": time.time()}
    with _REPOS_LOCK:
        _REPOS = store
    try:
        _atomic_write_json(REPOS_FILE, store)
    except OSError:
        pass                                # the in-memory store still serves
    return store


def repos_payload() -> dict:
    """GET /api/repos — cached-first, and NEVER a fan-out on the request path.

    Cold start (nothing on disk yet) reports `warming` so the UI can say so
    instead of showing an empty grid that looks like "no repos".
    """
    with _REPOS_LOCK:
        store = dict(_REPOS)
    if not store:
        store = _read_repos_store()
    if not store:
        return {"repos": [], "warming": True,
                "note": "first snapshot in flight — this fills in on the next poll"}
    store["age_s"] = int(time.time() - (store.get("generated_at") or 0))
    return store


def _repos_refresher():
    """The single background walker. One tick per REPO_REFRESH_S, forever."""
    while True:
        try:
            _refresh_repos()
        except Exception:  # noqa: BLE001 — a thread that dies stops the lens
            pass
        time.sleep(REPO_REFRESH_S)


# ----------------------------------------------------------------------------
# files tab — read-only file browser rooted at the session's repo root
#
# The root derives SERVER-SIDE from the session transcript — the same cwd
# derivation /api/git uses, widened to the repo toplevel when the cwd is in a
# git repo (reusing the git snapshot cache, so the two tabs can never disagree
# on the root). The caller only ever names the session and a RELATIVE path;
# no request parameter can move the root (same posture as /api/claudemd).
#
# _confine() is the single gate both endpoints pass every caller path through:
# reject before joining (absolute path, NUL, any `..` segment, `.git`), then
# canonicalize after joining (realpath both ends + commonpath containment,
# which catches symlink escapes). Listings mark escaping symlinks instead of
# following them; /api/file refuses them outright, and serves regular files
# only (a FIFO would hang the handler thread). GET-only, no subprocess, no
# mutation anywhere in this section.
# ----------------------------------------------------------------------------
FILES_TTL_S = 5
FILES_LIST_CAP = 500               # entries per listing; beyond -> truncated
FILE_TEXT_CAP = 256 * 1024         # text preview cap (head or tail)
FILE_RAW_CAP = 5 * 1024 * 1024     # raw image hard cap (413 beyond)
_FILES_CACHE: dict[tuple, tuple] = {}   # (root, rel, hidden) -> (expires, payload)
_FILES_LOCK = threading.Lock()
IMAGE_CTYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp",
                ".svg": "image/svg+xml"}


class ConfineError(ValueError):
    """A caller-supplied relative path that must not be served."""


def _confine(root: str, rel: str) -> str:
    """Resolve `rel` under `root` or raise ConfineError — the only path gate
    for /api/files and /api/file; every caller path passes through here.

    Rejects BEFORE joining: absolute paths, NUL, any `..` segment, `.git`.
    Canonicalizes AFTER joining: realpath both ends + commonpath containment,
    which catches symlink escapes (a link inside root pointing outside it).
    A symlink that lands back inside root but under .git/ is re-checked after
    resolution, so the exclusion can't be laundered through a link.
    """
    rel = rel or ""
    if "\x00" in rel:
        raise ConfineError("NUL in path")
    if rel.startswith(("/", "\\")):
        raise ConfineError("absolute paths not allowed")
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ConfineError("path traversal not allowed")
    if ".git" in parts:
        raise ConfineError(".git is not served")
    real_root = os.path.realpath(root)
    target = os.path.realpath(os.path.join(real_root, *parts))
    try:
        if os.path.commonpath([real_root, target]) != real_root:
            raise ConfineError("path escapes the session root")
    except ValueError as exc:          # mixed absolute/relative or drives
        raise ConfineError("path escapes the session root") from exc
    if target != real_root and \
            ".git" in os.path.relpath(target, real_root).split(os.sep):
        raise ConfineError(".git is not served")
    return target


def _list_dir(root: str, rel: str, hidden: bool = False) -> dict:
    """One directory listing under `root` (session-independent, testable).

    Dirs first then name sort; `.git` never listed; dotfiles excluded unless
    `hidden` (their count always reported). Symlinks are never followed for
    typing: a link is reported as type "symlink" with `target` dir|file when
    it resolves INSIDE the root, `escapes` when it points out of it (the
    client renders those inert), `broken` when it resolves to nothing.
    """
    target = _confine(root, rel)
    if not os.path.isdir(target):
        raise FileNotFoundError(rel or "/")
    real_root = os.path.realpath(root)
    rows, hidden_count = [], 0
    with os.scandir(target) as it:
        entries = sorted(it, key=lambda e: e.name.lower())
    for e in entries:
        if e.name == ".git":
            continue                    # always excluded, never even counted
        if e.name.startswith(".") and not hidden:
            hidden_count += 1
            continue
        row = {"name": e.name}
        try:
            if e.is_symlink():
                row["type"] = "symlink"
                try:
                    tgt = os.path.realpath(e.path)
                    if os.path.commonpath([real_root, tgt]) != real_root:
                        row["escapes"] = True
                    elif os.path.isdir(tgt):
                        row["target"] = "dir"
                    elif os.path.isfile(tgt):
                        row["target"] = "file"
                    else:
                        row["broken"] = True
                except (OSError, ValueError):
                    row["broken"] = True
            elif e.is_dir(follow_symlinks=False):
                row["type"] = "dir"
            else:
                row["type"] = "file"
                st = e.stat(follow_symlinks=False)
                row["size"] = st.st_size
                row["mtime"] = st.st_mtime
        except OSError:                 # raced deletion / permission
            row.setdefault("type", "file")
            row["broken"] = True
        rows.append(row)
    rows.sort(key=lambda r: (
        not (r["type"] == "dir" or r.get("target") == "dir"),
        r["name"].lower()))
    return {"path": rel, "entries": rows[:FILES_LIST_CAP],
            "hidden_count": hidden_count,
            "truncated": len(rows) > FILES_LIST_CAP}


def _files_root(sid: str):
    """(root, cwd, error_payload, code) — the server-side root derivation.

    cwd from the transcript tail (the /api/git derivation), widened to the
    repo toplevel via the same cached git snapshot /api/git serves from.
    """
    path = find_session(sid)
    if path is None:
        return None, None, {"error": "session not found"}, 404
    cwd = _session_cwd(path)
    if not cwd:
        return None, None, {"error": "no cwd recorded in this transcript"}, 404
    if not Path(cwd).is_dir():
        return None, None, {"error": "session cwd no longer exists"}, 404
    snap = _cached_snapshot(cwd, False)
    root = (snap.get("repo") or {}).get("root") or cwd
    return root, cwd, None, None


def files_payload(sid: str, rel: str, hidden=False, refresh=False):
    """(payload, code) for GET /api/files — one lazy directory listing."""
    root, cwd, err, code = _files_root(sid)
    if err:
        return err, code
    key = (root, rel or "", bool(hidden))
    now = time.time()
    if not refresh:
        with _FILES_LOCK:
            hit = _FILES_CACHE.get(key)
            if hit and hit[0] > now:
                return hit[1], 200
    try:
        listing = _list_dir(root, rel or "", hidden)
    except ConfineError as e:
        return {"error": str(e)}, 403
    except (FileNotFoundError, NotADirectoryError):
        return {"error": "directory not found"}, 404
    except OSError as e:
        return {"error": str(e)[:300]}, 500
    cwd_rel = "" if cwd == root else os.path.relpath(cwd, root)
    if cwd_rel == ".":
        cwd_rel = ""
    payload = {"root": root, "cwd_rel": cwd_rel, "generated_at": now, **listing}
    with _FILES_LOCK:
        _FILES_CACHE[key] = (now + FILES_TTL_S, payload)
    return payload, 200


def _file_json(root: str, rel: str, tail=False) -> tuple:
    """(payload, code) — one file's metadata + capped utf-8 text content.

    Binary (NUL in the first 8 KiB) -> metadata only, no content. Text past
    the cap carries truncated:true and part head|tail (tail=1 reads the end).
    Regular files only — S_ISREG, so a FIFO can't hang the handler thread.
    """
    try:
        target = _confine(root, rel)
    except ConfineError as e:
        return {"error": str(e)}, 403
    try:
        st = os.stat(target)
    except OSError:
        return {"error": "file not found"}, 404
    if not stat.S_ISREG(st.st_mode):
        return {"error": "not a regular file"}, 403
    size = st.st_size
    base = {"path": rel, "size": size, "mtime": st.st_mtime}
    with open(target, "rb") as f:
        if b"\x00" in f.read(8192):
            return {**base, "binary": True}, 200
        cap = FILE_TEXT_CAP
        if tail and size > cap:
            f.seek(size - cap)
            data, part = f.read(cap), "tail"
        else:
            f.seek(0)
            data, part = f.read(cap), "head"
    return {**base, "binary": False, "truncated": size > cap, "cap": cap,
            "part": part, "content": data.decode("utf-8", "replace")}, 200


def _file_raw(root: str, rel: str):
    """(bytes, ctype, error_payload, code) — image bytes only, hard-capped.

    raw=1 exists solely so a bare <img> tag works (cookie auth); anything
    that is not a whitelisted image extension is refused before touching disk.
    """
    ctype = IMAGE_CTYPES.get(os.path.splitext(rel or "")[1].lower())
    if not ctype:
        return None, None, {"error": "raw=1 serves image extensions only"}, 400
    try:
        target = _confine(root, rel)
    except ConfineError as e:
        return None, None, {"error": str(e)}, 403
    try:
        st = os.stat(target)
    except OSError:
        return None, None, {"error": "file not found"}, 404
    if not stat.S_ISREG(st.st_mode):
        return None, None, {"error": "not a regular file"}, 403
    if st.st_size > FILE_RAW_CAP:
        return None, None, {"error": f"image exceeds {FILE_RAW_CAP} byte cap"}, 413
    with open(target, "rb") as f:
        return f.read(FILE_RAW_CAP), ctype, None, None


def file_payload(sid: str, rel: str, tail=False):
    root, _cwd, err, code = _files_root(sid)
    if err:
        return err, code
    return _file_json(root, rel, tail)


def file_raw(sid: str, rel: str):
    root, _cwd, err, code = _files_root(sid)
    if err:
        return None, None, err, code
    return _file_raw(root, rel)


# ----------------------------------------------------------------------------
# auth
#
# The console is NOT a read-only surface: /api/answer and /api/fork spawn
# `claude -p --resume` with caller-supplied text in a caller-supplied cwd. On a
# non-loopback bind with no auth that is unauthenticated RCE, and the GETs leak
# every transcript verbatim. So: loopback stays frictionless (TOKEN=None), and
# any other bind REQUIRES a shared secret unless the operator opts out loudly.
# ----------------------------------------------------------------------------
TOKEN = None          # set by serve(); None = auth disabled
COOKIE = "csd_console"


def _loopback(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost", "")


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
class Handler(SimpleHTTPRequestHandler):
    # HTTP/1.1 keep-alive: the client polls every few seconds, and 1.0's
    # connection-per-request costs a TCP handshake per poll (worst on phone
    # wifi). Every response path below sends an exact Content-Length, which
    # keep-alive requires.
    protocol_version = "HTTP/1.1"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, *a):
        pass

    def _maybe_gzip(self, body: bytes):
        """gzip a response body when the client accepts it and it's worth it."""
        if len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            return gzip.compress(body, 5), True
        return body, False

    def _json(self, payload, code=200, extra_headers=None):
        body = json.dumps(payload, separators=(",", ":")).encode()
        body, gz = self._maybe_gzip(body)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if gz:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- auth ------------------------------------------------------------
    def _presented_token(self):
        q = parse_qs(urlparse(self.path).query).get("token")
        if q:
            return q[0], True          # from query → worth setting a cookie
        raw = self.headers.get("Cookie")
        if raw:
            c = SimpleCookie(raw)
            if COOKIE in c:
                return c[COOKIE].value, False
        return None, False

    def _authed(self):
        """True if the request may proceed. Emits its own 401 when not."""
        if TOKEN is None:
            return True
        tok, from_query = self._presented_token()
        if tok and hmac.compare_digest(tok, TOKEN):
            self._set_cookie = from_query
            return True
        # A navigational page load gets an HTML login form (a phone can't
        # hand-edit the URL); API paths keep the JSON 401 so a token-protected
        # bind's 401 doesn't trip the client-side JSON.parse guard.
        path = urlparse(self.path).path
        if (self.command == "GET" and not path.startswith("/api/")
                and "text/html" in (self.headers.get("Accept") or "")):
            body = LOGIN_PAGE.encode()
            self.send_response(401)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return False
        self._json({"error": "unauthorized — append ?token=<secret>"}, 401)
        return False

    def end_headers(self):
        # no-cache = "store but revalidate before use". Without it, index.html
        # falls through to SimpleHTTPRequestHandler which sends only
        # Last-Modified; browsers then apply RFC 7234 heuristic freshness and
        # serve a stale (old-JS) page WITHOUT revalidating, so a normal reload
        # runs pre-deploy client code. This forces a conditional GET each load.
        self.send_header("Cache-Control", "no-cache")
        if getattr(self, "_set_cookie", False):
            self.send_header(
                "Set-Cookie",
                f"{COOKIE}={TOKEN}; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800")
            self._set_cookie = False
        super().end_headers()

    # -- outer safety net -------------------------------------------------
    # A handler that raises leaves the client with a closed/bodyless response,
    # which the fetch caller then tries to JSON.parse -> a masking
    # "SyntaxError: unexpected character". These wrappers guarantee EVERY code
    # path answers with a JSON body, even an unforeseen exception.
    def _safe_500(self, exc):
        try:
            self._json({"error": str(exc)[:300]}, 500)
        except Exception:
            pass          # response already partly sent — nothing else to do

    def do_GET(self):
        try:
            self._do_GET()
        except Exception as e:
            self._safe_500(e)

    def do_POST(self):
        try:
            self._do_POST()
        except Exception as e:
            self._safe_500(e)

    def _do_GET(self):
        if not self._authed():
            return
        u = urlparse(self.path)
        if u.path == "/api/sessions":
            arch = (parse_qs(u.query).get("archived") or ["0"])[0] == "1"
            try:
                return self._json({"sessions": discover_sessions(archived=arch),
                                   "archived_count": len(_read_archive()),
                                   "summarizing": SUMMARIZING,
                                   "topics": managed_topics(),
                                   "generated_at": time.time()})
            except Exception as e:
                return self._json({"error": str(e)[:300]}, 500)
        if u.path == "/api/topics":
            # The managed topic → subtopics list, for the sidebar autocomplete.
            try:
                return self._json({"topics": managed_topics()})
            except Exception as e:
                return self._json({"error": str(e)[:300]}, 500)
        if u.path == "/api/session":
            sid = (parse_qs(u.query).get("id") or [""])[0]
            if not sid:
                return self._json({"error": "id required"}, 400)
            # Conditional poll: the transcript's (mtime, size) is a natural
            # validator, and a 304 skips both the full JSONL re-parse and the
            # multi-MB payload the client re-downloads every few seconds. A 30s
            # time bucket is folded in because parts of the payload move
            # without the file changing (state/mtime_age_s, tldr cache, angles
            # rail, SUMMARIZING) — worst-case staleness is one bucket, and
            # client actions force an unconditional refresh.
            etag = None
            p = find_session(sid)
            if p is not None:
                try:
                    st = p.stat()
                    # Fold the reply-queue file's signature in so a queued /
                    # cancelled / failed message repaints on the next poll
                    # instead of waiting out the 30s bucket.
                    try:
                        qsig = QUEUE_FILE.stat().st_mtime_ns
                    except OSError:
                        qsig = 0
                    etag = (f'"{st.st_mtime_ns:x}-{st.st_size:x}-{qsig:x}-'
                            f'{int(time.time() // 30):x}"')
                    if self.headers.get("If-None-Match") == etag:
                        self.send_response(304)
                        self.send_header("ETag", etag)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                except OSError:
                    etag = None
            try:
                s = build_session(sid)
                return self._json(s, extra_headers={"ETag": etag} if etag else None) \
                    if s else self._json({"error": "not found"}, 404)
            except Exception as e:
                return self._json({"error": str(e)[:300]}, 500)
        if u.path == "/api/version":
            # Running identity (captured at server start) vs what is on disk
            # NOW. A launchd-respawned console keeps executing the bytes it
            # booted with; this is how the operator sees that.
            try:
                return self._json(vinfo.version_report())
            except Exception as e:
                return self._json({"error": str(e)[:300]}, 500)
        if u.path == "/api/changelog":
            # Raw markdown; the client renders it (headings/lists only).
            txt = vinfo.changelog_text()
            if txt is None:
                return self._json({"error": "CHANGELOG.md not found"}, 404)
            return self._json({"markdown": txt, "version": vinfo.VERSION})
        if u.path == "/api/detail":
            q = parse_qs(u.query)
            sid = (q.get("id") or [""])[0]
            item = (q.get("item") or [""])[0]
            if not sid or not item:
                return self._json({"error": "id and item required"}, 400)
            d = angle_detail(sid, item)
            return self._json(d) if d else self._json(
                {"error": f"{item} not mined for {sid}"}, 404)
        if u.path == "/api/angles/catalog":
            q = parse_qs(u.query)
            try:
                return self._json(angle_catalog((q.get("id") or [""])[0]))
            except Exception as e:
                return self._json({"error": str(e)[:300]}, 500)
        if u.path == "/api/tool_result":
            q = parse_qs(u.query)
            sid = (q.get("id") or [""])[0]
            if not sid:
                return self._json({"error": "id required"}, 400)
            try:
                payload, code = tool_result_payload(sid, (q.get("tid") or [""])[0])
                return self._json(payload, code)
            except Exception as e:  # noqa: BLE001 — surfaced, never a bodyless 500
                return self._json({"error": str(e)[:300]}, 500)
        if u.path == "/api/git":
            q = parse_qs(u.query)
            sid = (q.get("id") or [""])[0]
            if not sid:
                return self._json({"error": "id required"}, 400)
            try:
                payload, code = git_payload(
                    sid, refresh=(q.get("refresh") or ["0"])[0] == "1")
                return self._json(payload, code)
            except Exception as e:
                return self._json({"error": str(e)[:300]}, 500)
        if u.path == "/api/repo":
            # Addressed by SESSION id (root derived server-side) or by a root
            # the registry already knows. A caller-supplied path is never run.
            q = parse_qs(u.query)
            try:
                payload, code = repo_detail_payload(
                    (q.get("id") or [""])[0], (q.get("root") or [""])[0])
                return self._json(payload, code)
            except Exception as e:
                return self._json({"error": str(e)[:300]}, 500)
        if u.path == "/api/repos":
            # Pure read: serves the background refresher's store off disk.
            try:
                return self._json(repos_payload())
            except Exception as e:
                return self._json({"error": str(e)[:300]}, 500)
        if u.path == "/api/files":
            q = parse_qs(u.query)
            sid = (q.get("id") or [""])[0]
            if not sid:
                return self._json({"error": "id required"}, 400)
            try:
                payload, code = files_payload(
                    sid, (q.get("path") or [""])[0],
                    hidden=(q.get("hidden") or ["0"])[0] == "1",
                    refresh=(q.get("refresh") or ["0"])[0] == "1")
                return self._json(payload, code)
            except Exception as e:
                return self._json({"error": str(e)[:300]}, 500)
        if u.path == "/api/file":
            q = parse_qs(u.query)
            sid = (q.get("id") or [""])[0]
            if not sid:
                return self._json({"error": "id required"}, 400)
            rel = (q.get("path") or [""])[0]
            try:
                if (q.get("raw") or ["0"])[0] == "1":
                    data, ctype, err, code = file_raw(sid, rel)
                    if err is not None:
                        return self._json(err, code)
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                payload, code = file_payload(
                    sid, rel, tail=(q.get("tail") or ["0"])[0] == "1")
                return self._json(payload, code)
            except Exception as e:
                return self._json({"error": str(e)[:300]}, 500)
        if u.path == "/api/claudemd":
            q = parse_qs(u.query)
            sid = (q.get("id") or [""])[0]
            if not sid:
                return self._json({"error": "id required"}, 400)
            try:
                n = int((q.get("n") or ["0"])[0])
            except ValueError:
                return self._json({"error": "bad n"}, 400)
            try:
                payload, code = claudemd_payload(sid, n)
                return self._json(payload, code)
            except Exception as e:
                return self._json({"error": str(e)[:300]}, 500)
        if u.path == "/api/mgmt":
            q = parse_qs(u.query)
            try:
                return self._json(mgmt_payload(
                    int((q.get("days") or ["7"])[0]),
                    int((q.get("live_min") or ["15"])[0])))
            except Exception as e:
                return self._json({"error": str(e)[:300]}, 500)
        if u.path == "/api/digest":
            q = parse_qs(u.query)
            sid = (q.get("id") or [""])[0]
            if not sid:
                return self._json({"error": "id required"}, 400)
            text, code = digest_payload(
                sid,
                delta=(q.get("delta") or ["0"])[0] == "1",
                head=int(q["head"][0]) if "head" in q else None,
                tail=int(q["tail"][0]) if "tail" in q else None,
                full=(q.get("full") or ["0"])[0] == "1")
            body, gz = self._maybe_gzip(text.encode())
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            if gz:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/timeline":
            sid = (parse_qs(u.query).get("id") or [""])[0]
            if not sid:
                return self._json({"error": "id required"}, 400)
            p = find_session(sid)
            if p is None:
                return self._json({"error": "not found"}, 404)
            return self._json({"timeline": session_timeline.payload(sid, p)})
        if u.path == "/api/summary-scope":
            # The Summarize label's data for ONE session (the reader overlay
            # can be open on a session the detail pane isn't showing).
            sid = (parse_qs(u.query).get("id") or [""])[0]
            if not sid:
                return self._json({"error": "id required"}, 400)
            return self._json({"session_id": sid,
                               "scope": None if ":" in sid
                                        else summary_scope(sid)})

        if u.path == "/api/tldr":
            # Pure read for the reader popover: cached store + staleness +
            # worker status. Never enqueues (peek, not request).
            sid = (parse_qs(u.query).get("id") or [""])[0]
            if not sid:
                return self._json({"error": "id required"}, 400)
            p = find_session(sid)
            if p is None:
                return self._json({"error": "not found"}, 404)
            return self._json({"tldr": tldr.peek(sid, p)})
        if u.path == "/api/batch":
            return self._json(batches_payload())
        if u.path == "/api/cr/manifest":
            sid = (parse_qs(u.query).get("id") or [""])[0]
            if not sid:
                return self._json({"error": "id required"}, 400)
            try:
                payload, code = cr_manifest_payload(sid)
                return self._json(payload, code)
            except Exception as e:
                return self._json({"error": str(e)[:300]}, 500)
        return super().do_GET()

    def _do_POST(self):
        if not self._authed():
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json({"error": "bad JSON"}, 400)
        # Route on the PATH alone: a POST may legitimately carry ?token=.
        route = urlparse(self.path).path

        # The one FORCED walk. The refresher owns the cadence; this is the ⟳
        # the operator presses when they have just merged something and do not
        # want to wait out the tick. Registry included — a repo that appeared
        # since boot should show up without a console restart.
        if route == "/api/repos/refresh":
            try:
                store = _refresh_repos(force_registry=True)
                return self._json({"ok": True, "count": len(store["repos"]),
                                   "generated_at": store["generated_at"]})
            except Exception as e:
                return self._json({"error": str(e)[:300]}, 500)

        # Content search is session-independent: it matches the archive DB text
        # (assistant content_blocks + user prompts) scoped to the visible ids.
        if route == "/api/search":
            q = (body.get("q") or "").strip()
            ids = body.get("ids") or []
            ids = ids[:500] if isinstance(ids, list) else []
            # An id-shaped query resolves off disk (archived / past the nav
            # cutoff / past the row cap are all still findable by id). It is
            # DB-free, so it answers even when the archive is unreachable.
            try:
                id_hits = [r for r in lookup_sessions_by_id(q)
                           if r["session_id"] not in ids]
            except Exception:                   # never let a glob break search
                id_hits = []
            if len(q) < 2 or not ids or not CSD_DSN:
                return self._json({"matches": {}, "id_hits": id_hits})
            from .. import session_mgmt as mgmt
            try:
                return self._json({"matches": mgmt.search_sessions(CSD_DSN, ids, q),
                                   "id_hits": id_hits})
            except Exception as e:              # DB unreachable → degrade, never 500
                return self._json({"matches": {}, "id_hits": id_hits, "error": str(e)})

        # CR search/compile are session-independent (the cart is client-side
        # state): route them before the session_id requirement below.
        if route == "/api/cr/search":
            q = (body.get("q") or "").strip()
            if len(q) < 2:
                return self._json({"error": "q too short", "results": []}, 400)
            return self._json(cr_search(q, (body.get("app") or "").strip()
                                        or None))
        if route == "/api/cr/compile":
            return self._json(cr_compile(body.get("refs")))

        # Batch ops are session-set-level, not single-session: route them
        # before the session_id requirement below.
        if route == "/api/batch":
            payload, code = create_batch(body.get("actions"),
                                         body.get("session_ids"),
                                         scope=body.get("scope"),
                                         options=body.get("options"))
            return self._json(payload, code)
        if route == "/api/batch/cancel":
            r = cancel_batch(body.get("batch_id", ""))
            return self._json(r, 200 if r["ok"] else 404)

        sid = body.get("session_id", "")
        cwd = body.get("cwd")
        if not sid:
            return self._json({"error": "session_id required"}, 400)

        # --- endpoints that act on the session, no text needed --------------
        if route == "/api/stop":
            r = stop_session(sid)
            return self._json(r, 200 if r["ok"] else 409)

        if route == "/api/queue/cancel":
            r = cancel_queued(sid, body.get("queue_id", ""))
            return self._json(r, 200 if r["ok"] else 409)

        if route == "/api/archive":
            return self._json(set_archived(sid, bool(body.get("archived", True)),
                                           body.get("reason", "")))

        if route == "/api/priority":
            pr = body.get("priority") or None
            if pr is not None and pr not in PRIORITIES:
                return self._json(
                    {"error": f"priority must be one of {list(PRIORITIES)} "
                              "or null to clear"}, 400)
            return self._json(set_priority(sid, pr))

        if route == "/api/title":
            # Title override is an index-only overlay (meta.json), never a
            # mutation of ~/.claude/projects. A child (subagent) key inherits
            # the parent's identity; title the parent instead.
            if ":" in sid:
                return self._json(
                    {"error": "child (subagent) sessions cannot be titled — "
                              "title the parent session"}, 400)
            return self._json(set_title(sid, body.get("title")))

        if route == "/api/topic":
            # Assign/clear the reusable topic → subtopic taxonomy (overlay only,
            # never a transcript mutation). Values are remembered in topics.json
            # so they're offered as autocomplete next time.
            if ":" in sid:
                return self._json(
                    {"error": "child (subagent) sessions inherit the parent's "
                              "topic — set it on the parent session"}, 400)
            return self._json(set_topic(sid, body.get("topic"),
                                        body.get("subtopic")))

        if route == "/api/tldr":
            # Ensure semantics: run-if-absent-or-stale (fresh is a no-op);
            # body.force regenerates unconditionally (the ⟳ affordance).
            # Never blocks: the fresh tldr lands on a later poll.
            p = find_session(sid)
            if p is None:
                return self._json({"error": "not found"}, 404)
            r = tldr.ensure(sid, p, force=bool(body.get("force")))
            return self._json({"ok": r["ok"], "ensure": r,
                               "tldr": tldr.peek(sid, p),
                               "status": tldr.STATUS.get(sid)})

        if route == "/api/timeline":
            # Whole-session catch-up, same ensure semantics: run-if-absent-
            # or-stale; body.force regenerates a fresh (or error) store too.
            # The result lands on a later poll of GET /api/timeline.
            p = find_session(sid)
            if p is None:
                return self._json({"error": "not found"}, 404)
            r = session_timeline.ensure(sid, p, force=bool(body.get("force")))
            return self._json({"ok": r["ok"], "ensure": r,
                               "timeline": session_timeline.payload(sid, p)})

        if route == "/api/title/dismiss":
            # Dismiss the tldr's proposed title (stored by value in the meta
            # overlay; a future different proposal surfaces again).
            if ":" in sid:
                return self._json({"error": "child (subagent) sessions carry "
                                            "no title proposal"}, 400)
            r = dismiss_title_proposal(sid, body.get("proposal"))
            return self._json(r, 200 if r["ok"] else 400)

        if route == "/api/summarize":
            if ":" in sid:
                return self._json(
                    {"error": "child (subagent) sessions are not summarized "
                              "on their own — summarize the parent"}, 400)
            # body.archive=false → summary only (the digest reader's plain
            # Summarize). Absent/true keeps the historical archive-on-dispatch.
            # body.delta: auto (default) | force | off — how the pass is scoped.
            mode = body.get("delta", "auto")
            mode = {True: "force", False: "off"}.get(mode, mode)
            if mode not in DELTA_MODES:
                return self._json(
                    {"error": f"delta must be one of {', '.join(DELTA_MODES)}"},
                    400)
            r = summarize_session(sid, cwd,
                                  archive=body.get("archive", True) is not False,
                                  delta=mode, dry_run=bool(body.get("dry_run")))
            return self._json(r, 200 if r["ok"] else 409)

        if route == "/api/angles/mine":
            wanted = body.get("angles")
            if wanted is not None:
                if (not isinstance(wanted, list) or not wanted
                        or not all(isinstance(a, str) for a in wanted)):
                    return self._json(
                        {"ok": False,
                         "error": "angles must be a non-empty list of angle "
                                  f"keys (have: {', '.join(ANGLE_SPECS)})"}, 400)
                unknown = [a for a in wanted if a not in ANGLE_SPECS]
                if unknown:
                    return self._json(
                        {"ok": False,
                         "error": f"unknown angle(s): {', '.join(unknown)} "
                                  f"(have: {', '.join(ANGLE_SPECS)})"}, 400)
            r = mine_angles(sid, bool(body.get("no_probes")), angles=wanted)
            return self._json(r, 200 if r["ok"] else 500)

        if route == "/api/cr":
            # Two-phase context-reduction fork (see cr_apply). Child transcripts
            # have no resumable session of their own — CR the parent.
            if ":" in sid:
                return self._json(
                    {"error": "child (subagent) sessions cannot be CR-forked — "
                              "reduce the parent session"}, 400)
            try:
                payload, code = cr_apply(sid, body.get("stub"),
                                         body.get("refs"),
                                         bool(body.get("confirm")))
                return self._json(payload, code)
            except crlib.CRUnsupported as e:
                return self._json({"error": str(e)[:300]}, 409)
            except Exception as e:
                return self._json({"error": str(e)[:300]}, 500)

        if route == "/api/angles/curate":
            try:
                r = curate(sid, body.get("item_id", ""), body.get("action", ""),
                           body.get("fields") or {}, bool(body.get("confirm")))
                return self._json(r, 200 if r["ok"] else 400)
            except KmcpError as e:
                return self._json({"ok": False, "error": str(e)[:400]}, 400)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:400]}, 500)

        # --- endpoints that send a message ----------------------------------
        if ":" in sid:
            return self._json(
                {"error": "child (subagent) sessions are read-only — "
                          "answer or fork the parent session instead"}, 400)
        text = (body.get("text") or "").strip()
        if not text:
            return self._json({"error": "text required"}, 400)

        if route == "/api/answer":
            # Never refused: sendable now -> spawned exactly as before; busy
            # (two-writer guard / console-spawned run live) or behind earlier
            # queued messages -> enqueued, dispatched by the queue worker the
            # moment answer_blocked() clears. See submit_answer().
            payload, code = submit_answer(sid, cwd, text)
            return self._json(payload, code)

        if route == "/api/fork":
            at = body.get("at_uuid")
            try:
                if at:
                    new_id = point_fork(sid, at)
                    spawn_claude(["-p", "--resume", new_id, text], cwd, new_id)
                    return self._json({"ok": True, "action": "point-fork",
                                       "new_session": new_id})
                # Mint the fork's id ourselves rather than let claude assign one
                # we never learn — same doctrine as point_fork(). Registering the
                # run under the PARENT sid would aim Stop at the wrong session and
                # leave the fork unaddressable. `--session-id` is only accepted
                # alongside --fork-session when resuming (the CLI enforces this).
                new_id = str(uuidlib.uuid4())
                spawn_claude(["-p", "--resume", sid, "--fork-session",
                              "--session-id", new_id, text], cwd, new_id)
                return self._json({"ok": True, "action": "fork",
                                   "new_session": new_id})
            except Exception as e:
                return self._json({"error": str(e)[:300]}, 500)

        return self._json({"error": "unknown endpoint"}, 404)


# ----------------------------------------------------------------------------
# ambient miner — angles/tldr/timeline kept warm for ACTIVE sessions
#
# The AngleWatcher (angles_watch.py) runs in-process: settle-detection means a
# session is touched only when its transcript changed and went quiet (~8s), so
# idle sessions cost one stat per scan and nothing else; the single worker
# means N live sessions cannot stampede Ollama. Doctrine intact — this warms
# the caches the UI already serves off disk (pull stays pull, Generate/⟳ stay
# the only forced runs). Disable with CSD_CONSOLE_AMBIENT=0 or --no-ambient.
# ----------------------------------------------------------------------------
AMBIENT = None                      # the live AngleWatcher, for status surfacing


def _ambient_skip(sid: str) -> bool:
    """Veto ambient mining for a session: archived, or the console has a run
    in flight for it (settle-detect covers most of that window; this closes
    the rest). Child keys ('<parent>:<agent_id>') inherit the parent verdict."""
    main_sid = sid.split(":", 1)[0]
    if main_sid in _read_archive():
        return True
    return bool(_live_procs(main_sid))


def _ambient_after_mine(sid: str) -> None:
    """Post-mine chain: tldr + timeline via their ensure() seams (built for
    exactly this runner). Both enqueue on their own single-worker lanes and
    return immediately; fresh is a no-op and error stores are never retried,
    so this can't loop on a failing session. Main sessions only — the digest
    chips live on main rows, and a child transcript has no tldr/timeline."""
    if ":" in sid:
        return
    p = find_session(sid)
    if not p:
        return
    tldr.ensure(sid, p)
    session_timeline.ensure(sid, p)


def _start_ambient() -> None:
    global AMBIENT
    from ..angles_watch import AngleWatcher, DEFAULT_LIVE_WINDOW_S
    from .. import angles as _A
    window = int(os.environ.get("CSD_CONSOLE_AMBIENT_WINDOW_S",
                                str(DEFAULT_LIVE_WINDOW_S)))
    AMBIENT = AngleWatcher(window_s=window, model=_A.DEFAULT_MODEL,
                           base_url=_A.DEFAULT_OLLAMA_URL, kmcp_dsn=KMCP_DSN,
                           no_probes=False, skip_fn=_ambient_skip,
                           after_mine=_ambient_after_mine)
    AMBIENT.start()
    print(f"  ambient miner: on (window {window}s; CSD_CONSOLE_AMBIENT=0 "
          "or --no-ambient to disable)", flush=True)


def serve(host="127.0.0.1", port=4462, token=None, no_auth=False, kmcp_dsn=None,
          csd_dsn=None, no_ambient=False):
    """Bind and serve. Non-loopback binds are authenticated unless no_auth."""
    global TOKEN, KMCP_DSN, CSD_DSN

    # Freeze the running code's identity BEFORE anything else can matter: from
    # here on, /api/version compares this snapshot against the repo on disk.
    run = vinfo.capture_running()

    KMCP_DSN = kmcp_dsn or os.environ.get("DATABASE_URL")
    CSD_DSN = csd_dsn or os.environ.get("CSD_DATABASE_URL")
    _migrate_legacy_overlays()      # seed meta.json from legacy priority/titles
    # Reply-queue dispatcher: drains queued composer messages once a session's
    # answer_blocked() clears (persisted queue — survives console restarts).
    threading.Thread(target=_queue_dispatcher, daemon=True,
                     name="reply-queue").start()
    _resume_batches()               # restart-safe batch queue (batch.json)
    # Repos lens: ONE background walker keeps repos.json warm, so /api/repos is
    # a disk read and a 30s poll never fans git out over 20 repositories.
    threading.Thread(target=_repos_refresher, daemon=True,
                     name="repos-refresh").start()
    if not no_ambient and os.environ.get("CSD_CONSOLE_AMBIENT", "1") != "0":
        _start_ambient()            # angles/tldr/timeline warm for active sessions

    print(f"csd console {run['version']}"
          + (f" ({run['sha']}{'+dirty' if run.get('dirty') else ''})"
             if run.get("sha") else ""), flush=True)

    if _loopback(host) or no_auth:
        TOKEN = None
    else:
        TOKEN = token or os.environ.get("CSD_CONSOLE_TOKEN") or secrets.token_urlsafe(24)

    lan_ip = host
    if host in ("0.0.0.0", "::"):
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            lan_ip, _ = s.getsockname()
            s.close()
        except OSError:
            lan_ip = host

    # flush=True: the token is the one line the operator needs, and a
    # backgrounded/nohup'd console would otherwise buffer it out of sight.
    if TOKEN:
        print(f"session console → http://{lan_ip}:{port}/?token={TOKEN}", flush=True)
        print("  auth: token required (cookie set on first load).", flush=True)
        print(f"  reuse this token: export CSD_CONSOLE_TOKEN={TOKEN}", flush=True)
    else:
        print(f"session console → http://{lan_ip}:{port}/", flush=True)
        if not _loopback(host):
            print("  *** WARNING: bound to a non-loopback address with NO AUTH.", flush=True)
            print("  *** /api/answer and /api/fork spawn `claude -p --resume`:", flush=True)
            print("  *** anyone who can reach this port can run code as you.", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    serve()
