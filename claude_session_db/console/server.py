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
import json
import os
import re
import subprocess
import time
import uuid as uuidlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
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
                rec = {"chars": len(txt)}
                if len(txt) <= 65536:
                    rec["text"] = txt
                out[tid] = rec
    return out


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


def discover_sessions():
    cutoff = time.time() - MAX_AGE_H * 3600
    cands = []
    for p in PROJECTS.glob("*/*.jsonl"):
        if "subagents" in p.parts:
            continue
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if m >= cutoff:
            cands.append((m, p))
    cands.sort(reverse=True)
    out = []
    for _, p in cands[:MAX_NAV_SESSIONS]:
        try:
            s = summarize_nav(p)
        except OSError:
            continue
        if s:
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
                        events.append({
                            "kind": "read", "ts": ts, "uuid": uid, "sub": sub,
                            "tool": base, "app": app_, "path": path_,
                            "mode": mode, "sections": secs, "via": via,
                            "etype": inp.get("entity_type") or _etype_hint(path_),
                            "chars": (rmap.get(tid) or {}).get("chars"),
                            "count": len(inp.get("entries") or inp.get("paths") or [])
                                     if base == "get_entries" else None,
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
                        other_tools.append(name)
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
    }


# ----------------------------------------------------------------------------
# answer / fork (unchanged behaviour from the prototype)
# ----------------------------------------------------------------------------
def spawn_claude(args, cwd):
    with open(ANSWER_LOG, "a") as log:
        log.write(f"\n--- spawn {time.strftime('%H:%M:%S')}: claude {' '.join(args)} (cwd={cwd})\n")
        log.flush()
        subprocess.Popen(
            ["claude"] + args, cwd=cwd or str(Path.home()),
            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )


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

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/sessions":
            try:
                return self._json({"sessions": discover_sessions(),
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
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json({"error": "bad JSON"}, 400)
        sid = body.get("session_id", "")
        text = (body.get("text") or "").strip()
        cwd = body.get("cwd")
        if not sid or not text:
            return self._json({"error": "session_id and text required"}, 400)

        if self.path == "/api/answer":
            src = find_session(sid)
            if src and time.time() - src.stat().st_mtime < 15:
                return self._json(
                    {"error": "session active in the last 15s — answer refused "
                              "(two-writer guard); fork instead"}, 409)
            spawn_claude(["-p", "--resume", sid, text], cwd)
            return self._json({"ok": True, "action": "answer", "session": sid})

        if self.path == "/api/fork":
            at = body.get("at_uuid")
            try:
                if at:
                    new_id = point_fork(sid, at)
                    spawn_claude(["-p", "--resume", new_id, text], cwd)
                    return self._json({"ok": True, "action": "point-fork",
                                       "new_session": new_id})
                spawn_claude(["-p", "--resume", sid, "--fork-session", text], cwd)
                return self._json({"ok": True, "action": "fork"})
            except Exception as e:
                return self._json({"error": str(e)[:300]}, 500)

        return self._json({"error": "unknown endpoint"}, 404)


if __name__ == "__main__":
    # 127.0.0.1: local prototype only (LAN exposure is a separate, later decision).
    print("session console → http://127.0.0.1:4462/")
    ThreadingHTTPServer(("127.0.0.1", 4462), Handler).serve_forever()
