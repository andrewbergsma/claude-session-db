#!/usr/bin/env python3
"""Native-CC session console — full-screen chat + inline kmcp reads (Direction A).

The reply-capable cockpit surface. Everything the UI shows is derived from the
session's OWN transcript (~/.claude/projects/<project>/<session>.jsonl):

  - chat turns              from user/assistant message records
  - kmcp reads (inline)     from mcp__*__(get_entry|get_section|get_entries)
                            tool_use blocks — app/path/mode/sections from `input`,
                            plus `knowledge-cli call <tool>` invoked through Bash
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
  GET  /api/files?id=<sid>&path=   one directory listing under the session's
                                   repo root / cwd (read-only, root-confined)
  GET  /api/file?id=<sid>&path=    one file's text/metadata (raw=1: image bytes)
  GET  /api/claudemd?id=<sid>&n=   one CLAUDE.md memory file's content (read-only)
  GET  /api/timeline?id=<sid>      cached whole-session tl;dr timeline (never generates)
  POST /api/answer                 {session_id, cwd, text} -> claude -p --resume
                                   (busy session -> message queued, never refused)
  POST /api/queue/cancel           {session_id, queue_id} -> drop a queued message
  POST /api/fork                   {session_id, cwd, text, at_uuid?}
  POST /api/priority               {session_id, priority: low|med|high|critical|null}
  POST /api/title                  {session_id, title: str|null} -> set/clear a title
  POST /api/topic                  {session_id, topic, subtopic} -> set/clear taxonomy
  GET  /api/topics                 managed topic -> subtopics list (autocomplete)
  POST /api/tldr                   {session_id} -> force-queue a tldr regeneration
  POST /api/timeline               {session_id} -> force-queue a whole-session timeline
  POST /api/batch                  {actions, session_ids|scope, options?:{force}}
                                   -> queue a fan-out (see the batch-ops section)

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

from .. import tldr
from .. import session_timeline
from ..angles import ANGLE_SPECS, ANGLE_LABELS

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
META_FIELDS = ("title", "priority", "topic", "subtopic")
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


def _parse_search_result(text):
    """Pull (total, type_counts, top hits) out of a search tool_result — the
    surfacing telemetry: what the base OFFERED the session for this query."""
    if not text:
        return None
    try:
        d = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    res = d.get("results") or d.get("entries") or d.get("hits") or []
    hits = []
    for e in res[:12]:
        if isinstance(e, dict):
            hits.append({"app": e.get("application"), "path": e.get("path"),
                         "title": e.get("title"), "score": e.get("score"),
                         "etype": e.get("entity_type")})
    return {"total": d.get("total"), "type_counts": d.get("type_counts"),
            "hits": hits}


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
    if base not in READ_TOOLS and base not in SURFACE_TOOLS:
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
    if not title:
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
        try:
            s = summarize_nav(p)
        except OSError:
            continue
        if s:
            m = _meta_of(meta, p.stem)
            s["archived"] = p.stem in idx
            # stoppable + agents already computed in summarize_nav (state override)
            s["priority"] = m.get("priority")
            s["user_title"] = m.get("title")
            s["topic"] = m.get("topic")
            s["subtopic"] = m.get("subtopic")
            # Cached-or-nothing; stale rows queue an async regeneration.
            s["tldr"] = tldr.payload(p.stem, p)
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
    n_reads = n_searches = 0

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
                            "tool": base, "via": via,
                            "query": (inp.get("query") or inp.get("path")
                                      or inp.get("application") or ""),
                            "app": inp.get("application"),
                            "chars": (rmap.get(tid) or {}).get("chars"),
                            "result": _parse_search_result(
                                (rmap.get(tid) or {}).get("text")),
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
                   "events": len(events)},
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
        "priority": _m.get("priority"),
        "user_title": _m.get("title"),
        "topic": _m.get("topic"),
        "subtopic": _m.get("subtopic"),
    }
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
SUMMARIZE_PROMPT = "/session-summary"
SUMMARIZING: dict[str, str] = {}     # sid -> "running" | "done" | error text
SUMMARY_MIN_OUTPUT_BYTES = 40        # child output past the header ⇒ it ran
_SUMMARY_LOG_DIR = CONSOLE_STATE / "summaries"


def _await_summary(sid: str, proc, log_path: Path, base_size: int):
    """Resolve a dispatched summary. rc!=0 → failed. rc==0 does NOT prove a kmcp
    write happened — but a child that produced NO output past the spawn header
    is the observed silent no-op, so it is downgraded rather than called done."""
    rc = proc.wait()
    if rc != 0:
        SUMMARIZING[sid] = f"summary failed (rc={rc})"
        return
    try:
        produced = log_path.stat().st_size - base_size
    except OSError:
        produced = SUMMARY_MIN_OUTPUT_BYTES + 1     # can't measure → don't accuse
    SUMMARIZING[sid] = ("done" if produced > SUMMARY_MIN_OUTPUT_BYTES
                        else "summary produced no output")


def summarize_session(sid: str, cwd: str) -> dict:
    if SUMMARIZING.get(sid) == "running":
        return {"ok": False, "error": "a summary is already running"}
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
    # a headless run cannot get approved.
    src = find_session(sid)
    proc = spawn_claude(["-p", f"{SUMMARIZE_PROMPT} {sid}"], cwd, sid,
                        log_path=log_path, action="summarize",
                        envelope_ctx={"session_id": sid,
                                      "transcript": str(src) if src else None})
    try:
        base_size = log_path.stat().st_size          # header only, pre-output
    except OSError:
        base_size = 0
    SUMMARIZING[sid] = "running"
    set_archived(sid, True, reason="session-summary")
    threading.Thread(target=_await_summary,
                     args=(sid, proc, log_path, base_size),
                     daemon=True, name=f"summarize-{sid[:8]}").start()
    return {"ok": True, "action": "summarize", "session": sid, "pid": proc.pid,
            "envelope": getattr(proc, "envelope_note", None),
            "note": "independent off-session summary dispatched; session archived"}


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
    gh = shutil.which("gh")
    if not gh:
        payload = {"available": False, "reason": "gh CLI not installed"}
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
        gh = _gh_prs(snap["repo"]["root"], refresh)
        # only stamp pr/None on commits when a real listing was fetched —
        # otherwise "no PR" would be indistinguishable from "gh unavailable"
        if gh.get("available") and not gh.get("error"):
            _attribute_commits_to_prs(snap, gh)
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
        if u.path == "/api/batch":
            return self._json(batches_payload())
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

        # Content search is session-independent: it matches the archive DB text
        # (assistant content_blocks + user prompts) scoped to the visible ids.
        if route == "/api/search":
            q = (body.get("q") or "").strip()
            ids = body.get("ids") or []
            ids = ids[:500] if isinstance(ids, list) else []
            if len(q) < 2 or not ids or not CSD_DSN:
                return self._json({"matches": {}})
            from .. import session_mgmt as mgmt
            try:
                return self._json({"matches": mgmt.search_sessions(CSD_DSN, ids, q)})
            except Exception as e:              # DB unreachable → degrade, never 500
                return self._json({"matches": {}, "error": str(e)})

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
            # Force-queue a regeneration (the per-session refresh affordance).
            # Never blocks: the fresh tldr lands on a later /api/session poll.
            p = find_session(sid)
            if p is None:
                return self._json({"error": "not found"}, 404)
            return self._json({"ok": True, "tldr": tldr.payload(sid, p, force=True),
                               "status": tldr.STATUS.get(sid)})

        if route == "/api/timeline":
            # Button-launched whole-session catch-up. Force-enqueues a
            # (re)generation; the fresh timeline lands on a later poll of the
            # GET /api/timeline endpoint. Never blocks.
            p = find_session(sid)
            if p is None:
                return self._json({"error": "not found"}, 404)
            return self._json(
                {"ok": True, "timeline": session_timeline.payload(sid, p, force=True)})

        if route == "/api/summarize":
            if ":" in sid:
                return self._json(
                    {"error": "child (subagent) sessions are not summarized "
                              "on their own — summarize the parent"}, 400)
            r = summarize_session(sid, cwd)
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


def serve(host="127.0.0.1", port=4462, token=None, no_auth=False, kmcp_dsn=None,
          csd_dsn=None):
    """Bind and serve. Non-loopback binds are authenticated unless no_auth."""
    global TOKEN, KMCP_DSN, CSD_DSN

    KMCP_DSN = kmcp_dsn or os.environ.get("DATABASE_URL")
    CSD_DSN = csd_dsn or os.environ.get("CSD_DATABASE_URL")
    _migrate_legacy_overlays()      # seed meta.json from legacy priority/titles
    # Reply-queue dispatcher: drains queued composer messages once a session's
    # answer_blocked() clears (persisted queue — survives console restarts).
    threading.Thread(target=_queue_dispatcher, daemon=True,
                     name="reply-queue").start()
    _resume_batches()               # restart-safe batch queue (batch.json)

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
