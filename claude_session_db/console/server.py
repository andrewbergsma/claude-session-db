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

Endpoints
  GET  /api/sessions               light nav list (project, title, state, mtime)
  GET  /api/session?id=<sid>       full transcript as a chronological event stream
  GET  /api/detail?id=<sid>&item=  the persisted detail behind one angle headline
  POST /api/answer                 {session_id, cwd, text} -> claude -p --resume
  POST /api/fork                   {session_id, cwd, text, at_uuid?}

Local: binds 127.0.0.1, no auth. Point-fork writes a NEW session file under
~/.claude/projects (never mutates the original).
"""
import hmac
import json
import os
import re
import secrets
import shutil
import signal
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

ROOT = Path(__file__).parent
PROJECTS = Path.home() / ".claude" / "projects"
NAV_TAIL_BYTES = 256 * 1024      # nav only needs the tail for title/state
FULL_MAX_BYTES = 24 * 1024 * 1024  # guard: tail huge transcripts
MAX_NAV_SESSIONS = 40
MAX_AGE_H = 72
ANSWER_LOG = ROOT / "answers.log"

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
ANGLE_ORDER = ["direction", "events", "files", "kmcp", "commands",
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


def _state(records, mtime_age):
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
        return "idle"
    if last["type"] == "user":
        st = "running"
    else:
        stop = (last.get("message") or {}).get("stop_reason")
        st = "awaiting" if stop in ("end_turn", "stop_sequence") else "running"
    if st == "running" and mtime_age > 600:
        st = "stale"
    return st


def summarize_nav(path: Path):
    recs = tail_records(path, NAV_TAIL_BYTES)
    if not recs:
        return None
    title = cwd = branch = None
    last_user = None
    for r in recs:
        t = r.get("type")
        if t == "ai-title":
            title = r.get("aiTitle") or title
        elif t == "custom-title":
            title = r.get("customTitle") or title
        elif t in ("user", "assistant"):
            cwd = r.get("cwd") or cwd
            branch = r.get("gitBranch") or branch
            if t == "user" and not r.get("isSidechain"):
                txt = _text_of((r.get("message") or {}).get("content"))
                if _is_real_user_turn(r, txt):
                    last_user = txt
    mtime_age = time.time() - path.stat().st_mtime
    if not title:
        title = (last_user[:70] + "…") if last_user else path.stem[:12]
    label = (cwd or str(path.parent.name)).rstrip("/").split("/")[-1]
    return {
        "session_id": path.stem,
        "project": str(path.parent.name),
        "project_label": label,
        "cwd": cwd, "branch": branch, "title": title.strip(),
        "state": _state(recs, mtime_age),
        "mtime": path.stat().st_mtime,
        "mtime_age_s": round(mtime_age),
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
    out = []
    for _, p in cands[:MAX_NAV_SESSIONS]:
        try:
            s = summarize_nav(p)
        except OSError:
            continue
        if s:
            s["archived"] = p.stem in idx
            s["stoppable"] = stoppable(p.stem)
            out.append(s)
    return out


def find_session(sid: str):
    return next(PROJECTS.glob(f"*/{sid}.jsonl"), None)


def build_session(sid: str):
    path = find_session(sid)
    if path is None:
        return None
    records, truncated = all_records(path)
    rmap = _result_map(records)

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
                        other_tools.append({
                            "name": name, "label": label, "detail": detail,
                            "chars": res.get("chars"),
                            "is_error": res.get("is_error", False),
                        })
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

    mtime_age = time.time() - path.stat().st_mtime
    if not title:
        first_user = next((e["text"] for e in events if e["kind"] == "user"), None)
        title = (first_user[:70] + "…") if first_user else sid[:12]

    ctx_tokens = None
    if usage:
        ctx_tokens = (usage.get("input_tokens", 0)
                      + usage.get("cache_read_input_tokens", 0)
                      + usage.get("cache_creation_input_tokens", 0))

    return {
        "session_id": sid,
        "project": str(path.parent.name),
        "cwd": cwd, "branch": branch, "title": title.strip(),
        "model": model, "ctx_tokens": ctx_tokens,
        "state": _state(records, mtime_age),
        "mtime_age_s": round(mtime_age),
        "truncated": truncated,
        "counts": {"reads": n_reads, "searches": n_searches,
                   "events": len(events)},
        "events": events,
        "rail": angle_rail(sid),
        "archived": sid in _read_archive(),
        "stoppable": stoppable(sid),
    }


# ----------------------------------------------------------------------------
# answer / fork (unchanged behaviour from the prototype)
# ----------------------------------------------------------------------------
def spawn_claude(args, cwd, session_id=None):
    """Spawn a claude run, registering it so Stop can signal its process group."""
    with open(ANSWER_LOG, "a") as log:
        log.write(f"\n--- spawn {time.strftime('%H:%M:%S')}: claude {' '.join(args)} (cwd={cwd})\n")
        log.flush()
        proc = subprocess.Popen(
            ["claude"] + args, cwd=cwd or str(Path.home()),
            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    _register(session_id, proc)
    return proc


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


def mine_angles(sid: str, no_probes=False) -> dict:
    """Run the miner for one session, the way we already shell out to claude."""
    csd = _csd_bin()
    cmd = ([csd] if csd else [sys.executable, "-m", "claude_session_db.cli"])
    cmd += ["angles", "--session", sid]
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
# session summary -> archive
#
# Runs the /session-summary skill on the session itself (`claude -p --resume`),
# which captures changelog events + attribution-tagged lessons that patch the
# corpus upstream. It costs a real agent turn on that session and appends to its
# transcript — which is why it archives only AFTER the run exits cleanly. A
# failed summary leaves the session in the sidebar, where you can see it failed.
# ----------------------------------------------------------------------------
SUMMARIZE_PROMPT = "/session-summary"
SUMMARIZING: dict[str, str] = {}     # sid -> "running" | "done" | error text


def _summarize_then_archive(sid: str, proc):
    rc = proc.wait()
    if rc == 0:
        set_archived(sid, True, reason="session-summary")
        SUMMARIZING[sid] = "done"
    else:
        SUMMARIZING[sid] = f"summary failed (rc={rc}); not archived"


def summarize_session(sid: str, cwd: str) -> dict:
    if SUMMARIZING.get(sid) == "running":
        return {"ok": False, "error": "a summary is already running"}
    src = find_session(sid)
    if src and time.time() - src.stat().st_mtime < 15:
        return {"ok": False, "error": "session active in the last 15s — "
                                      "summary refused (two-writer guard)"}
    proc = spawn_claude(["-p", "--resume", sid, SUMMARIZE_PROMPT], cwd, sid)
    SUMMARIZING[sid] = "running"
    threading.Thread(target=_summarize_then_archive, args=(sid, proc),
                     daemon=True, name=f"summarize-{sid[:8]}").start()
    return {"ok": True, "action": "summarize", "session": sid, "pid": proc.pid,
            "note": "archives automatically when the summary exits cleanly"}


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
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, *a):
        pass

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
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
        self.send_response(401)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"401 unauthorized - append ?token=<secret>\n")
        return False

    def end_headers(self):
        if getattr(self, "_set_cookie", False):
            self.send_header(
                "Set-Cookie",
                f"{COOKIE}={TOKEN}; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800")
            self._set_cookie = False
        super().end_headers()

    def do_GET(self):
        if not self._authed():
            return
        u = urlparse(self.path)
        if u.path == "/api/sessions":
            arch = (parse_qs(u.query).get("archived") or ["0"])[0] == "1"
            try:
                return self._json({"sessions": discover_sessions(archived=arch),
                                   "archived_count": len(_read_archive()),
                                   "summarizing": SUMMARIZING,
                                   "generated_at": time.time()})
            except Exception as e:
                return self._json({"error": str(e)[:300]}, 500)
        if u.path == "/api/session":
            sid = (parse_qs(u.query).get("id") or [""])[0]
            if not sid:
                return self._json({"error": "id required"}, 400)
            try:
                s = build_session(sid)
                return self._json(s) if s else self._json({"error": "not found"}, 404)
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
        return super().do_GET()

    def do_POST(self):
        if not self._authed():
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json({"error": "bad JSON"}, 400)
        # Route on the PATH alone: a POST may legitimately carry ?token=.
        route = urlparse(self.path).path
        sid = body.get("session_id", "")
        cwd = body.get("cwd")
        if not sid:
            return self._json({"error": "session_id required"}, 400)

        # --- endpoints that act on the session, no text needed --------------
        if route == "/api/stop":
            r = stop_session(sid)
            return self._json(r, 200 if r["ok"] else 409)

        if route == "/api/archive":
            return self._json(set_archived(sid, bool(body.get("archived", True)),
                                           body.get("reason", "")))

        if route == "/api/summarize":
            r = summarize_session(sid, cwd)
            return self._json(r, 200 if r["ok"] else 409)

        if route == "/api/angles/mine":
            r = mine_angles(sid, bool(body.get("no_probes")))
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
        text = (body.get("text") or "").strip()
        if not text:
            return self._json({"error": "text required"}, 400)

        if route == "/api/answer":
            src = find_session(sid)
            if src and time.time() - src.stat().st_mtime < 15:
                return self._json(
                    {"error": "session active in the last 15s — answer refused "
                              "(two-writer guard); fork instead"}, 409)
            spawn_claude(["-p", "--resume", sid, text], cwd, sid)
            return self._json({"ok": True, "action": "answer", "session": sid})

        if route == "/api/fork":
            at = body.get("at_uuid")
            try:
                if at:
                    new_id = point_fork(sid, at)
                    spawn_claude(["-p", "--resume", new_id, text], cwd, new_id)
                    return self._json({"ok": True, "action": "point-fork",
                                       "new_session": new_id})
                spawn_claude(["-p", "--resume", sid, "--fork-session", text],
                             cwd, sid)
                return self._json({"ok": True, "action": "fork"})
            except Exception as e:
                return self._json({"error": str(e)[:300]}, 500)

        return self._json({"error": "unknown endpoint"}, 404)


def serve(host="127.0.0.1", port=4462, token=None, no_auth=False, kmcp_dsn=None):
    """Bind and serve. Non-loopback binds are authenticated unless no_auth."""
    global TOKEN, KMCP_DSN

    KMCP_DSN = kmcp_dsn or os.environ.get("DATABASE_URL")

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
