"""angles_web — ambient multi-session angles dashboard (spike).

An out-of-band surface for the turn-angles loop, three levels deep:

  folders (projects) → session data-table → fullscreen session focus

A watcher tails every live Claude Code transcript under ~/.claude/projects,
re-mines a session's latest turn (via angles.run_angles) whenever its JSONL
settles. The stdlib HTTP server renders project folders as parents; selecting
a folder shows a metadata table of its sessions (sorted by last turn);
selecting a session opens a fullscreen focus view: the full message stream
(user/assistant text + tool chips, tool-result bodies excluded), the kmcp
context the session loaded, and the latest turn's angle headlines.

Doctrine note (claudecode:design/turn-angles-context-cockpit): pull-not-push
governs the CONVERSATION surface; this dashboard is the ambient exception —
zero context tokens, zero interruption, glanceable. Probes run through a
single-worker queue so N live sessions never stampede the local Ollama.

No auth: LAN-trusted, read-only over the angles state dir + transcripts.
Do not expose beyond the local network.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from pathlib import Path

from . import angles as A
from . import session_mgmt as mgmt
from .session_digest import input_hint
from .subagent import read_agent_meta

SCAN_INTERVAL_S = 5          # transcript poll cadence
DEBOUNCE_S = 8               # file must be quiet this long before mining
DEFAULT_LIVE_WINDOW_S = 1800  # transcript mtime within this = live session
DEFAULT_PORT = 8791
BOARD_WINDOW_S = 86400       # sessions older than this drop off the board
MSG_TEXT_CAP = 6000          # chars of one message shown in focus view

# kmcp READ tools — how a session loads knowledge context (writes are the
# angles `kmcp` angle; reads are the "context loaded" rail in focus view).
_KMCP_READ_TOOLS = ("get_entry", "get_entries", "get_section", "search",
                    "hybrid_search", "list_entries", "list_by_tag",
                    "get_process_step", "traverse_graph", "query_view",
                    "list_children", "get_application", "list_applications")


def _resolve_transcript(sid: str) -> Optional[Path]:
    """Main-session uuid -> <proj>/<uuid>.jsonl; child key '<parent>:<agent>'
    -> the subagents/**/agent-<id>.jsonl sidechain file."""
    if ":" in sid:
        parent, aid = sid.split(":", 1)
        return next(iter(A.PROJECTS_DIR.glob(
            f"*/{parent}/subagents/**/agent-{aid}.jsonl")), None)
    return next(iter(A.PROJECTS_DIR.glob(f"*/{sid}.jsonl")), None)


class AngleWatcher(threading.Thread):
    """Polls live transcripts; queues one mining job per settled change."""

    def __init__(self, window_s: int, model: str, base_url: str,
                 kmcp_dsn: Optional[str], no_probes: bool):
        super().__init__(daemon=True, name="angle-watcher")
        self.window_s = window_s
        self.model = model
        self.base_url = base_url
        self.kmcp_dsn = kmcp_dsn
        self.no_probes = no_probes
        self.mined_sig: dict[str, tuple[int, int]] = {}
        self.status: dict[str, str] = {}   # sid -> "mining" | "ok" | error text
        self.jobs: "queue.Queue[tuple[str, tuple[int, int]]]" = queue.Queue()
        self.queued: set[str] = set()
        self._worker = threading.Thread(target=self._work, daemon=True,
                                        name="angle-worker")

    # -- scan ------------------------------------------------------------
    def run(self) -> None:
        self._worker.start()
        while True:
            try:
                self._scan_once()
            except Exception as exc:  # noqa: BLE001 — watcher must survive
                self.status["_scan"] = f"{type(exc).__name__}: {exc}"
            time.sleep(SCAN_INTERVAL_S)

    def _scan_once(self) -> None:
        now = time.time()
        # Main transcripts + live sidechains (a running background child shows
        # up as its own row under the parent). Sidechain sids are child keys
        # '<parent>:<agent_id>' — the same address the archive uses.
        candidates = ((p, p.stem) for p in A.PROJECTS_DIR.glob("*/*.jsonl"))
        sub = ((p, A.subagent_key(p)) for p in A.PROJECTS_DIR.glob(
            "*/*/subagents/**/agent-*.jsonl"))
        for p, sid in (*candidates, *sub):
            if not sid:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if now - st.st_mtime > self.window_s:
                continue
            if now - st.st_mtime < DEBOUNCE_S:
                continue  # still being written; next scan will see it settled
            sig = (st.st_mtime_ns, st.st_size)
            if self.mined_sig.get(sid) == sig or sid in self.queued:
                continue
            self.queued.add(sid)
            self.jobs.put((sid, sig))

    # -- mine ------------------------------------------------------------
    def _work(self) -> None:
        while True:
            sid, sig = self.jobs.get()
            self.status[sid] = "mining"
            try:
                A.run_angles(cwd="", session_id=sid, turn=-1,
                             model=self.model, base_url=self.base_url,
                             kmcp_dsn=self.kmcp_dsn, no_probes=self.no_probes)
                self.mined_sig[sid] = sig
                self.status[sid] = "ok"
            except Exception as exc:  # noqa: BLE001 — one bad session ≠ dead worker
                self.status[sid] = f"{type(exc).__name__}: {exc}"
            finally:
                self.queued.discard(sid)


# --- API ------------------------------------------------------------------------

def _sessions_payload(watcher: AngleWatcher) -> list[dict[str, Any]]:
    out = []
    now = time.time()
    for f in sorted(A._state_dir().glob("*.json")):
        if f.name == "last.json":
            continue
        try:
            store = json.loads(f.read_text())
        except (ValueError, OSError):
            continue
        sid = store.get("session_id", f.stem)
        transcript = _resolve_transcript(sid)
        t_mtime = transcript.stat().st_mtime if transcript else 0
        if now - t_mtime > BOARD_WINDOW_S:
            continue
        headlines: dict[str, list[dict]] = {}
        for iid, item in store.get("items", {}).items():
            headlines.setdefault(item.get("angle", "?"), []).append(
                {"id": iid, "headline": item.get("headline", "")})
        is_sub = ":" in sid
        meta = (read_agent_meta(transcript)
                if is_sub and transcript is not None else {})
        out.append({
            "session_id": sid,
            "is_subagent": is_sub,
            "parent_session_id": sid.split(":", 1)[0] if is_sub else None,
            "agent_id": sid.split(":", 1)[1] if is_sub else None,
            "agent_type": meta.get("agentType", ""),
            "agent_description": meta.get("description", ""),
            "slug": store.get("slug", ""),
            "cwd": store.get("cwd", ""),
            "git_branch": store.get("git_branch", ""),
            "session_started_at": store.get("session_started_at", ""),
            "prompt_count": store.get("prompt_count", 0),
            "record_count": store.get("record_count", 0),
            "model": store.get("model", ""),
            "cc_version": store.get("cc_version", ""),
            "transcript_bytes": transcript.stat().st_size if transcript else 0,
            "user_text": store.get("user_text", "")[:200],
            "turn_span": store.get("turn_span", []),
            "usage": store.get("usage", {}),
            "generated_at": store.get("generated_at", ""),
            "pull_age_s": int(now - f.stat().st_mtime),
            "live": bool(transcript) and (now - t_mtime) <= watcher.window_s,
            "transcript_age_s": int(now - t_mtime) if transcript else None,
            "status": watcher.status.get(sid, ""),
            "headlines": headlines,
        })
    # Last turn first — the operator's question is "what moved most recently".
    out.sort(key=lambda s: (s["transcript_age_s"] is None,
                            s["transcript_age_s"] or 0))
    return out


def _detail_payload(sid: str, item_id: str) -> dict[str, Any]:
    f = A._state_dir() / f"{sid}.json"
    if not f.exists():
        return {"error": f"no angles for {sid}"}
    store = json.loads(f.read_text())
    return store.get("items", {}).get(item_id.upper(),
                                      {"error": f"{item_id} not found"})


def _kmcp_target(name: str, inp: dict) -> Optional[tuple[str, str]]:
    """(tool, target) when this tool_use is a kmcp READ; else None."""
    short = name.rsplit("__", 1)[-1] if name.startswith("mcp__") else ""
    if short in _KMCP_READ_TOOLS and ("kmcp" in name or "knowledge" in name):
        app, path = inp.get("application", ""), inp.get("path", "")
        target = (f"{app}:{path}" if app and path
                  else inp.get("query") or path or app or "")
        return short, str(target)[:120]
    if name == "Bash":
        cmd = inp.get("command") or ""
        if "knowledge-cli call" in cmd:
            for tool in _KMCP_READ_TOOLS:
                if f"call {tool}" in cmd:
                    return tool, (inp.get("description") or "")[:120]
    return None


def _agent_result_map(recs: list[dict]) -> dict[str, dict[str, Any]]:
    """tool_use_id -> {agent_id, agent_type, status} from record-level
    toolUseResult carriers (the harness's own record of each Agent spawn)."""
    out: dict[str, dict[str, Any]] = {}
    for rec in recs:
        tur = rec.get("toolUseResult")
        if rec.get("type") != "user" or not isinstance(tur, dict) \
                or not tur.get("agentId"):
            continue
        for b in rec.get("message", {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_result" \
                    and b.get("tool_use_id"):
                out[b["tool_use_id"]] = {
                    "agent_id": tur.get("agentId"),
                    "agent_type": tur.get("agentType", ""),
                    "status": tur.get("status", ""),
                }
    return out


def _spawn_anchor(parent_recs: list[dict], agent_id: str) -> Optional[str]:
    """uuid of the parent message to jump to for a child's back-link: the
    assistant message carrying the Agent tool_use (via the result carrier's
    sourceToolAssistantUuid/parentUuid), from the archived parent transcript."""
    for rec in parent_recs:
        tur = rec.get("toolUseResult")
        if rec.get("type") == "user" and isinstance(tur, dict) \
                and tur.get("agentId") == agent_id:
            return rec.get("sourceToolAssistantUuid") or rec.get("parentUuid")
    return None


def _session_payload(sid: str) -> dict[str, Any]:
    """Focus view: full message stream + kmcp context loaded + angle items.

    sid may be a main-session uuid or a child key '<parent>:<agent_id>' — for a
    child the isSidechain skip is lifted (every record in a sidechain file is
    sidechain) and the payload carries a `subagent` header block with the
    meta.json identity + the parent anchor uuid for the back-link.
    """
    transcript = _resolve_transcript(sid)
    if not transcript:
        return {"error": f"no transcript for {sid}"}
    is_child = ":" in sid
    recs = A.load_jsonl(transcript)
    agent_results = _agent_result_map(recs)
    base_sid = sid.split(":", 1)[0]

    messages: list[dict[str, Any]] = []
    kmcp_loaded: dict[tuple[str, str], int] = {}
    for rec in recs:
        if rec.get("isSidechain") and not is_child:
            continue
        typ = rec.get("type")
        msg = rec.get("message", {})
        content = msg.get("content")
        if typ == "user":
            text = A._text_of(content).strip()
            # Skip meta records and harness-injected wrappers (<command-…>,
            # <local-command-…>, <system-reminder>) — noise in a reading view.
            if not text or rec.get("isMeta") or text.startswith("<"):
                continue
            messages.append({
                "role": "user", "ts": rec.get("timestamp", ""),
                "uuid": rec.get("uuid", ""),
                "compact": bool(rec.get("isCompactSummary")
                                or text.startswith("This session is being continued")),
                "text": text[:MSG_TEXT_CAP],
                "truncated": len(text) > MSG_TEXT_CAP,
                "tools": [],
            })
        elif typ == "assistant":
            text = A._text_of(content).strip()
            tools = []
            for b in content or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    name, inp = b.get("name", "?"), b.get("input", {}) or {}
                    chip: dict[str, Any] = {"name": name,
                                            "hint": input_hint(inp)[:90]}
                    spawn = agent_results.get(b.get("id", ""))
                    if name == "Agent" and spawn:
                        # The chip becomes a link to the child focus view.
                        chip["child"] = f"{base_sid}:{spawn['agent_id']}"
                        chip["status"] = spawn.get("status", "")
                        chip["hint"] = (inp.get("description")
                                        or chip["hint"])[:90]
                    tools.append(chip)
                    hit = _kmcp_target(name, inp)
                    if hit:
                        kmcp_loaded[hit] = kmcp_loaded.get(hit, 0) + 1
            if not text and not tools:
                continue
            messages.append({"role": "assistant",
                             "ts": rec.get("timestamp", ""),
                             "uuid": rec.get("uuid", ""),
                             "text": text[:MSG_TEXT_CAP],
                             "truncated": len(text) > MSG_TEXT_CAP,
                             "tools": tools})

    store: dict[str, Any] = {}
    f = A._state_dir() / f"{sid}.json"
    if f.exists():
        try:
            store = json.loads(f.read_text())
        except (ValueError, OSError):
            store = {}
    payload: dict[str, Any] = {
        "session_id": sid,
        "messages": messages,
        "kmcp_loaded": [{"tool": t, "target": tgt, "n": n}
                        for (t, tgt), n in kmcp_loaded.items()],
        "items": store.get("items", {}),
    }
    if is_child:
        parent, aid = sid.split(":", 1)
        meta = read_agent_meta(transcript)
        anchor = None
        parent_path = _resolve_transcript(parent)
        if parent_path is not None:
            anchor = _spawn_anchor(A.load_jsonl(parent_path), aid)
        payload["subagent"] = {
            "parent_session_id": parent,
            "agent_id": aid,
            "agent_type": meta.get("agentType", ""),
            "description": meta.get("description", ""),
            "spawn_depth": meta.get("spawnDepth"),
            "anchor_uuid": anchor,
        }
    return payload


def _mgmt_payload(csd_dsn: Optional[str], kmcp_dsn: Optional[str],
                  window_days: int, live_min: int) -> Any:
    """Session-management lens payload. DB-degraded: an unreachable archive
    returns {"error": ...} instead of a 500 — the lens page reports it."""
    if not csd_dsn:
        return {"error": "no archive DSN configured (start angles-serve with a DSN)"}
    try:
        rows = mgmt.inventory(csd_dsn, kmcp_dsn, window_days=window_days,
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


# --- HTTP -----------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    watcher: AngleWatcher  # injected by serve()
    csd_dsn: Optional[str] = None   # injected by serve()
    kmcp_dsn: Optional[str] = None  # injected by serve()

    def log_message(self, *args: Any) -> None:  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/":
                self._send(200, _PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/sessions":
                self._json(_sessions_payload(self.watcher))
            elif path == "/api/mgmt":
                q = parse_qs(parsed.query)
                self._json(_mgmt_payload(
                    self.csd_dsn, self.kmcp_dsn,
                    int(q.get("days", ["7"])[0]),
                    int(q.get("live_min", [str(mgmt.LIVE_MIN_DEFAULT)])[0])))
            elif path.startswith("/api/digest/"):
                q = parse_qs(parsed.query)
                try:
                    text = mgmt.digest_for(
                        path.split("/")[3], dsn=self.csd_dsn,
                        kmcp_dsn=self.kmcp_dsn,
                        delta=q.get("delta", ["0"])[0] == "1",
                        head=int(q["head"][0]) if "head" in q else None,
                        tail=int(q["tail"][0]) if "tail" in q else None,
                        full=q.get("full", ["0"])[0] == "1")
                    self._send(200, text.encode(), "text/plain; charset=utf-8")
                except ValueError as exc:
                    self._send(404, f"digest: {exc}".encode(),
                               "text/plain; charset=utf-8")
            elif path.startswith("/api/session/"):
                self._json(_session_payload(path.split("/")[3]))
            elif path.startswith("/api/detail/"):
                parts = path.split("/")
                if len(parts) == 5:
                    self._json(_detail_payload(parts[3], parts[4]))
                else:
                    self._json({"error": "usage: /api/detail/SID/ID"}, 400)
            else:
                self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)


def serve(host: str, port: int, window_s: int, model: str, base_url: str,
          kmcp_dsn: Optional[str], no_probes: bool,
          csd_dsn: Optional[str] = None) -> None:
    watcher = AngleWatcher(window_s, model, base_url, kmcp_dsn, no_probes)
    watcher.start()
    Handler.watcher = watcher
    Handler.csd_dsn = csd_dsn
    Handler.kmcp_dsn = kmcp_dsn
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"angles dashboard on http://{host}:{port}/  "
          f"(live window {window_s}s, probes {'OFF' if no_probes else model})")
    httpd.serve_forever()


# --- UI (inline, self-contained) --------------------------------------------------

_ANGLE_ORDER = ["direction", "events", "agents", "files", "kmcp", "commands",
                "git", "errors", "knowledge", "metrics"]

_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>angles</title>
<style>
  :root { --bg:#0e1116; --card:#161b22; --line:#242c37; --fg:#c9d1d9;
          --dim:#8b949e; --acc:#58a6ff; --err:#f85149; --ok:#3fb950;
          --warn:#d29922; }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--bg); color:var(--fg); font:14px/1.45 ui-monospace,
         SFMono-Regular,Menlo,monospace; padding:14px; }
  h1 { font-size:15px; color:var(--dim); font-weight:normal; margin-bottom:10px; }
  h1 b { color:var(--fg); }
  .crumb { color:var(--acc); cursor:pointer; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%;
         background:var(--dim); flex:none; }
  .dot.on { background:var(--ok); }
  .mining .dot { background:var(--warn); animation:pulse 1s infinite; }
  @keyframes pulse { 50% { opacity:.3; } }
  /* folder tabs */
  #tabs { display:flex; flex-wrap:wrap; gap:6px; }
  .tab { background:var(--card); border:1px solid var(--line);
         border-bottom:none; border-radius:8px 8px 0 0; padding:7px 12px 6px;
         cursor:pointer; color:var(--dim); display:flex; gap:7px;
         align-items:center; max-width:260px; }
  .tab.active { color:var(--fg); background:#1b2330; border-color:var(--acc); }
  .tab .n { color:var(--dim); font-size:12px; }
  /* session table */
  #panel { background:#1b2330; border:1px solid var(--acc);
           border-radius:0 8px 8px 8px; padding:10px 12px; overflow-x:auto; }
  table { border-collapse:collapse; width:100%; font-size:13px; }
  th { color:var(--dim); text-align:left; font-weight:normal; padding:4px 10px;
       border-bottom:1px solid var(--line); white-space:nowrap; }
  td { padding:5px 10px; border-bottom:1px solid var(--line);
       white-space:nowrap; max-width:340px; overflow:hidden;
       text-overflow:ellipsis; }
  tr.row { cursor:pointer; }
  tr.row:hover td { background:#20293a; }
  tr.stale { opacity:.5; }
  td.num { text-align:right; }
  /* focus view */
  #focus { position:fixed; inset:0; background:var(--bg); display:none;
           z-index:5; padding:14px; overflow:hidden; flex-direction:column; }
  #focus.open { display:flex; }
  .kv { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr));
        gap:2px 22px; padding:8px 0 10px; border-bottom:1px solid var(--line); }
  .kv div { display:flex; gap:8px; min-width:0; }
  .kv .k { color:var(--dim); flex:none; width:86px; text-align:right;
           font-size:12px; padding-top:1px; }
  .kv .v { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  #fbody { flex:1; display:flex; gap:14px; min-height:0; margin-top:10px; }
  #msgs { flex:1; overflow-y:auto; min-width:0; }
  .msg { margin:0 0 10px; padding:8px 10px; border-radius:6px;
         background:var(--card); border-left:3px solid var(--line); }
  .msg.user { border-left-color:var(--acc); background:#182030; }
  .msg.compact { border-left-color:var(--warn); opacity:.7; }
  .msg .mh { color:var(--dim); font-size:11px; margin-bottom:4px; }
  .msg .mt { white-space:pre-wrap; word-break:break-word; }
  .chips { margin-top:6px; display:flex; flex-wrap:wrap; gap:4px; }
  .chip { background:#0e1420; border:1px solid var(--line); border-radius:10px;
          padding:1px 8px; font-size:11px; color:var(--dim); max-width:420px;
          overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .chip b { color:var(--fg); font-weight:normal; }
  #rail { width:360px; flex:none; overflow-y:auto; display:flex;
          flex-direction:column; gap:12px; }
  .box { background:var(--card); border:1px solid var(--line); border-radius:8px;
         padding:10px 12px; }
  .box h2 { font-size:12px; color:var(--dim); font-weight:normal;
            margin-bottom:6px; text-transform:uppercase; letter-spacing:.06em; }
  .angle { display:flex; gap:8px; margin:2px 0; }
  .aname { color:var(--dim); width:70px; flex:none; text-align:right;
           font-size:11px; padding-top:2px; }
  .items { flex:1; min-width:0; }
  .hl { cursor:pointer; white-space:nowrap; overflow:hidden;
        text-overflow:ellipsis; font-size:12px; }
  .hl:hover { color:var(--acc); }
  .hl .id { color:var(--warn); margin-right:5px; }
  .angle.errors .hl { color:var(--err); }
  .angle.direction .hl { color:#e3b341; }
  .kentry { font-size:12px; white-space:nowrap; overflow:hidden;
            text-overflow:ellipsis; }
  .kentry .kt { color:var(--dim); }
  #detail { position:fixed; inset:auto 16px 16px auto; width:min(680px,90vw);
            max-height:70vh; overflow:auto; background:#0a0d12;
            border:1px solid var(--acc); border-radius:8px; padding:14px;
            display:none; z-index:9; }
  #detail pre { white-space:pre-wrap; word-break:break-all; font-size:12px; }
  #detail .x { float:right; cursor:pointer; color:var(--dim); }
  .empty { color:var(--dim); padding:40px; text-align:center; }
  .abadge { color:var(--acc); cursor:pointer; font-size:11px; margin-left:6px; }
  tr.child td { background:#141a24; font-size:12px; }
  .chip.agent { border-color:var(--acc); color:var(--acc); cursor:pointer; }
  .chip.agent b { color:var(--acc); }
  .v-LIVE { color:var(--ok); } .v-OPEN { color:var(--warn); }
  .v-OPENdelta { color:var(--err); font-weight:bold; }
  .v-OPENq { color:var(--warn); } .v-CLOSED { color:var(--dim); }
  .modes { color:var(--dim); }
  .modes span { cursor:pointer; }
  .modes span.on { color:var(--fg); border-bottom:1px solid var(--acc); }
  @media (max-width:900px){ #rail { display:none; } }
</style></head><body>
<h1><span class="modes"><span id="m-angles" class="on" onclick="setMode('angles')">angles</span>
    · <span id="m-mgmt" onclick="setMode('mgmt')">sessions</span></span>
    · <span id="crumb"></span>
    <span id="stat" style="float:right"></span></h1>
<div id="tabs"></div><div id="panel"><div class="empty">loading…</div></div>
<div id="focus"></div>
<div id="detail"><span class="x" onclick="hideDetail()">✕ close</span><pre id="dbody"></pre></div>
<script>
const ORDER = %ANGLE_ORDER%;
let rows = [], folder = null, sid = null, lastJson = "";
function age(s){ if(s==null) return "?";
  return s<60? s+"s" : s<3600? Math.round(s/60)+"m" : Math.round(s/3600)+"h"; }
function kb(n){ return n>1048576? (n/1048576).toFixed(1)+" MB" :
  Math.round((n||0)/1024)+" KB"; }
function ts(t){ if(!t) return "?"; const d=new Date(t);
  return d.toLocaleDateString(undefined,{month:"short",day:"numeric"})+" "+
         d.toLocaleTimeString(undefined,{hour:"2-digit",minute:"2-digit"}); }
function tshort(t){ return t? new Date(t).toLocaleTimeString(undefined,
  {hour:"2-digit",minute:"2-digit"}) : ""; }
function dur(a,liveEnd){ if(!a) return "?";
  const ms=(liveEnd?new Date(liveEnd):new Date())-new Date(a);
  const m=Math.round(ms/60000); return m<60? m+"m" : (m/60).toFixed(1)+"h"; }
function esc(t){ const d=document.createElement("i"); d.textContent=t??"";
  return d.innerHTML; }
function label(s){ return s.slug ||
  ((s.cwd||"").split("/").pop()||"?")+" "+(s.session_id||"").slice(0,6); }
function folders(){
  const m=new Map();
  for(const s of rows){
    if(s.is_subagent) continue;
    const key=s.cwd||"(unknown)";
    if(!m.has(key)) m.set(key,{cwd:key,sessions:[],live:0,minAge:1e12});
    const f=m.get(key); f.sessions.push(s);
    if(s.live) f.live++;
    f.minAge=Math.min(f.minAge, s.transcript_age_s??1e12);
  }
  return [...m.values()].sort((a,b)=>a.minAge-b.minAge);
}
let expanded=new Set();
function kids(id){ return rows.filter(r=>r.is_subagent&&r.parent_session_id===id); }
function toggleKids(id){ expanded.has(id)?expanded.delete(id):expanded.add(id); render(); }
async function openChild(k){ sid=k; render(); await renderFocus(true); }
async function openParent(p,anchor){ sid=p; render(); await renderFocus(false);
  if(anchor){ const el=document.getElementById("m-"+anchor);
    if(el){ el.scrollIntoView({block:"center"}); el.style.outline="1px solid var(--acc)";
      setTimeout(()=>el.style.outline="",2500); } } }
function goRoot(){ sid=null; render(); }
function pickFolder(cwd){ folder=cwd; sid=null; render(); }
async function pickSession(id){ sid=id; render(); await renderFocus(true); }
let mode="angles", mgmtRows=null, mgmtErr=null, mgmtAt=0;
function setMode(m){ mode=m; sid=null;
  document.getElementById("m-angles").classList.toggle("on", m==="angles");
  document.getElementById("m-mgmt").classList.toggle("on", m==="mgmt");
  if(m==="mgmt") refreshMgmt(true); else render(); }
async function refreshMgmt(force){
  if(!force && Date.now()-mgmtAt<30000){ renderMgmt(); return; }
  mgmtAt=Date.now();
  document.getElementById("crumb").textContent="session management";
  try{
    const r=await fetch("/api/mgmt"); const d=await r.json();
    if(d.error){ mgmtErr=d.error; mgmtRows=null; }
    else { mgmtRows=d.sessions; mgmtErr=null; }
  }catch(e){ mgmtErr=String(e); }
  renderMgmt();
}
function fmtDelta(d){
  if(!d) return "";
  if(d.class==="none") return "none";
  if(d.class==="unknown") return "unknown"+(d.note?` (${d.note})`:"");
  const body=`${d.records}rec ${d.prompts}p ${d.tool_calls}t`;
  if(d.class==="real")
    return `REAL ${body}${d.signals&&d.signals[0]? " · "+d.signals[0]:""}`;
  return {confirmation_only:"confirm-only",
          auto_compaction_only:"compaction-only"}[d.class]+` ${body}`;
}
function agentsBadge(a){
  if(!a||!a.total) return "";
  let t=String(a.total);
  if(a.running) t+=` <span class="v-OPEN">${a.running}r</span>`;
  if(a.failed) t+=` <span class="v-OPENdelta">${a.failed}f</span>`;
  return t;
}
function renderMgmt(){
  if(mode!=="mgmt") return;
  document.getElementById("tabs").innerHTML="";
  document.getElementById("focus").classList.remove("open");
  const panel=document.getElementById("panel");
  if(mgmtErr){ panel.innerHTML=`<div class="empty">archive unavailable — ${esc(mgmtErr)}</div>`; return; }
  if(!mgmtRows){ panel.innerHTML='<div class="empty">loading…</div>'; return; }
  const trs=mgmtRows.map(s=>{
    const vcls="v-"+s.verdict.replace("-delta","delta").replace("?","q");
    const wantDelta=s.verdict==="OPEN-delta";
    const state=(s.state||"—")+(s.reason?"/"+s.reason:"");
    return `<tr class="row" onclick="showDigest('${s.session_id}',${wantDelta})">
      <td class="${vcls}">${esc(s.verdict)}</td>
      <td title="${esc(s.session_id)}">${s.session_id.slice(0,8)}</td>
      <td>${esc(s.project_name||"?")}</td>
      <td>${esc(s.git_branch||"—")}</td>
      <td>${ts(s.last_ts)}</td>
      <td class="num">${age(s.idle_s)}</td>
      <td class="num">${s.message_count||0}</td>
      <td>${agentsBadge(s.agents)}</td>
      <td title="${esc(s.kmcp_target||"")}">${esc(state)}</td>
      <td class="${s.delta&&s.delta.class==="real"?"v-OPENdelta":""}"
          title="${esc((s.delta&&s.delta.signals||[]).join("\n"))}">${esc(fmtDelta(s.delta))}</td>
      </tr>`;
  }).join("");
  panel.innerHTML=`<table><thead><tr><th>verdict</th><th>session</th>
    <th>project</th><th>branch</th><th>last activity</th><th>idle</th>
    <th>msgs</th><th>agents</th><th>summary</th><th>delta after summary</th></tr></thead>
    <tbody>${trs}</tbody></table>
    <div style="color:var(--dim);padding:6px 2px;font-size:12px">
      last activity = max(messages.ts) from the archive (never file mtime) ·
      click a row for its digest (delta digest when OPEN-delta)</div>`;
}
async function showDigest(id,delta){
  const q=delta?"delta=1":"head=30&tail=80";
  document.getElementById("dbody").textContent="loading digest…";
  document.getElementById("detail").style.display="block";
  const r=await fetch(`/api/digest/${id}?${q}`);
  document.getElementById("dbody").textContent=await r.text();
}
function childRow(c){
  const u=c.usage||{}, mining=c.status==="mining";
  const name=(c.agent_type||"agent")+" · "+
    ((c.agent_description||"").slice(0,36)||(c.agent_id||"").slice(0,8));
  return `<tr class="row child ${c.live?"":"stale"} ${mining?"mining":""}"
    onclick="pickSession('${c.session_id}')">
    <td><span class="dot ${c.live?"on":""}"></span></td>
    <td title="${esc(c.user_text)}" style="padding-left:24px">↳ ${esc(name)}</td>
    <td>${age(c.transcript_age_s)} ago${mining?" ⛏":""}</td>
    <td>${ts(c.session_started_at)}</td>
    <td class="num">${dur(c.session_started_at, c.live?null:(c.turn_span||[])[1])}</td>
    <td class="num">${c.prompt_count||"?"}</td>
    <td class="num">${u.tool_calls??"?"}</td>
    <td class="num">${(u.output_tokens??0).toLocaleString()}</td>
    <td>—</td>
    <td>${esc((c.model||"?").replace("claude-",""))}</td>
    <td class="num">${kb(c.transcript_bytes)}</td>
    <td>${age(c.pull_age_s)}</td></tr>`;
}
function render(){
  if(mode==="mgmt"){ renderMgmt(); return; }
  const fs=folders();
  const tabs=document.getElementById("tabs"),
        panel=document.getElementById("panel"),
        focus=document.getElementById("focus");
  focus.classList.toggle("open", !!sid);
  if(sid) return;
  if(!fs.length){ tabs.innerHTML="";
    panel.innerHTML='<div class="empty">no live sessions — the watcher mines '+
      'each transcript as it settles</div>'; return; }
  if(!fs.some(f=>f.cwd===folder)) folder=fs[0].cwd;
  document.getElementById("crumb").textContent=
    folder.split("/").slice(-2).join("/");
  tabs.innerHTML=fs.map(f=>{
    const name=f.cwd.split("/").pop()||f.cwd;
    return `<div class="tab ${f.cwd===folder?"active":""}"
      onclick="pickFolder('${esc(f.cwd)}')"><span class="dot ${f.live?"on":""}"></span>
      ${esc(name)} <span class="n">${f.live}/${f.sessions.length}</span></div>`;
  }).join("");
  const f=fs.find(x=>x.cwd===folder);
  const trs=f.sessions.map(s=>{
    const u=s.usage||{}, mining=s.status==="mining";
    const ch=kids(s.session_id);
    const badge=ch.length?`<span class="abadge" onclick="event.stopPropagation();toggleKids('${s.session_id}')">${expanded.has(s.session_id)?"▾":"▸"} agents ${ch.length}</span>`:"";
    let row=`<tr class="row ${s.live?"":"stale"} ${mining?"mining":""}"
      onclick="pickSession('${s.session_id}')">
      <td><span class="dot ${s.live?"on":""}"></span></td>
      <td title="${esc(s.user_text)}">${esc(label(s))}${badge}</td>
      <td>${age(s.transcript_age_s)} ago${mining?" ⛏":""}</td>
      <td>${ts(s.session_started_at)}</td>
      <td class="num">${dur(s.session_started_at, s.live?null:(s.turn_span||[])[1])}</td>
      <td class="num">${s.prompt_count||"?"}</td>
      <td class="num">${u.tool_calls??"?"}</td>
      <td class="num">${(u.output_tokens??0).toLocaleString()}</td>
      <td>${esc(s.git_branch||"—")}</td>
      <td>${esc((s.model||"?").replace("claude-",""))}</td>
      <td class="num">${kb(s.transcript_bytes)}</td>
      <td>${age(s.pull_age_s)}</td></tr>`;
    if(expanded.has(s.session_id)) row+=ch.map(childRow).join("");
    return row;
  }).join("");
  panel.innerHTML=`<table><thead><tr><th></th><th>session</th><th>last turn</th>
    <th>started</th><th>dur</th><th>prompts</th><th>turn tools</th>
    <th>turn out</th><th>branch</th><th>model</th><th>size</th><th>pulled</th>
    </tr></thead><tbody>${trs}</tbody></table>`;
}
async function renderFocus(scroll){
  if(!sid) return;
  const s=rows.find(r=>r.session_id===sid)||{session_id:sid};
  const r=await fetch(`/api/session/${sid}`); const d=await r.json();
  const sub=d.subagent;
  const u=s.usage||{};
  const kv=[["directory",s.cwd||"?"],["branch",s.git_branch||"—"],
    ["started",ts(s.session_started_at)],
    ["duration",dur(s.session_started_at,s.live?null:(s.turn_span||[])[1])],
    ["last turn",age(s.transcript_age_s)+" ago"],
    ["prompts",(s.prompt_count||"?")+" · "+(s.record_count||"?")+" records"],
    ["turn cost",(u.tool_calls??"?")+" tools · "+
      (u.output_tokens??0).toLocaleString()+" out"],
    ["model",(s.model||"?")+(s.cc_version?" · cc "+s.cc_version:"")],
    ["transcript",kb(s.transcript_bytes)],["session",sid]];
  const meta=kv.map(([k,v])=>`<div><span class="k">${k}</span>`+
    `<span class="v" title="${esc(v)}">${esc(v)}</span></div>`).join("");
  const msgs=(d.messages||[]).map(m=>{
    const chips=(m.tools||[]).map(t=> t.child
      ? `<span class="chip agent" onclick="openChild('${t.child}')"><b>Agent</b>`+
        `${t.hint?" "+esc(t.hint):""}${t.status?" · "+esc(t.status):""} ↗</span>`
      : `<span class="chip"><b>${esc(t.name)}</b>`+
        `${t.hint?" "+esc(t.hint):""}</span>`).join("");
    return `<div class="msg ${m.role} ${m.compact?"compact":""}"${m.uuid?` id="m-${m.uuid}"`:""}>
      <div class="mh">${m.role}${m.compact?" · compaction":""} · ${tshort(m.ts)}</div>
      ${m.text?`<div class="mt">${esc(m.text)}${m.truncated?" …":""}</div>`:""}
      ${chips?`<div class="chips">${chips}</div>`:""}</div>`;
  }).join("");
  const hl={};
  for(const [iid,it] of Object.entries(d.items||{}))
    (hl[it.angle]=hl[it.angle]||[]).push({id:iid,headline:it.headline});
  const angles=ORDER.filter(a=>hl[a]).map(a=>{
    const items=hl[a].map(h=>`<div class="hl" `+
      `onclick="showDetail('${sid}','${h.id}')">`+
      `<span class="id">${h.id}</span>${esc(h.headline)}</div>`).join("");
    return `<div class="angle ${a}"><div class="aname">${a}</div>`+
           `<div class="items">${items}</div></div>`;
  }).join("")||'<div class="empty" style="padding:8px">not mined yet</div>';
  const kmcp=(d.kmcp_loaded||[]).map(e=>`<div class="kentry">`+
    `<span class="kt">${esc(e.tool)}${e.n>1?"×"+e.n:""}</span> `+
    `${esc(e.target)}</div>`).join("")||
    '<div class="empty" style="padding:8px">none loaded</div>';
  const el=document.getElementById("focus");
  const keep=el.querySelector("#msgs");
  const nearBottom=!keep||keep.scrollHeight-keep.scrollTop-keep.clientHeight<80;
  const keepTop=keep?keep.scrollTop:0;
  const crumb = sub
    ? `<span class="crumb" onclick="openParent('${sub.parent_session_id}','${sub.anchor_uuid||""}')">← parent ${sub.parent_session_id.slice(0,8)}</span>
       &nbsp; <b>${esc(sub.agent_type||"agent")}</b>
       <span style="color:var(--dim)"> · ${esc(sub.description||"")}${sub.spawn_depth!=null?` · depth ${sub.spawn_depth}`:""} · agent ${esc((sub.agent_id||"").slice(0,8))}</span>`
    : `<span class="crumb" onclick="goRoot()">← ${esc((s.cwd||"").split("/").pop()||"back")}</span>
       &nbsp; <b>${esc(label(s))}</b>
       <span style="color:var(--dim)"> · “${esc(s.user_text||"")}”</span>`;
  el.innerHTML=`
    <div>${crumb}</div>
    <div class="kv">${meta}</div>
    <div id="fbody">
      <div id="msgs">${msgs||'<div class="empty">no messages</div>'}</div>
      <div id="rail">
        <div class="box"><h2>latest turn · angles</h2>${angles}</div>
        <div class="box"><h2>kmcp context loaded</h2>${kmcp}</div>
      </div></div>`;
  const box=el.querySelector("#msgs");
  if(box) box.scrollTop=(scroll||nearBottom)?box.scrollHeight:keepTop;
}
async function tick(){
  try{
    if(mode==="mgmt"){ await refreshMgmt(false);
      document.getElementById("stat").textContent=new Date().toLocaleTimeString();
      return; }
    const r=await fetch("/api/sessions"); const txt=await r.text();
    document.getElementById("stat").textContent=new Date().toLocaleTimeString();
    if(txt!==lastJson){ lastJson=txt; rows=JSON.parse(txt); render(); }
    if(sid) await renderFocus(false);
  }catch(e){ document.getElementById("stat").textContent="offline: "+e; }
}
async function showDetail(s,id){
  const r=await fetch(`/api/detail/${s}/${id}`);
  document.getElementById("dbody").textContent=
    JSON.stringify(await r.json(),null,2);
  document.getElementById("detail").style.display="block";
}
function hideDetail(){ document.getElementById("detail").style.display="none"; }
document.addEventListener("keydown",e=>{
  if(e.key==="Escape"){
    if(document.getElementById("detail").style.display==="block") hideDetail();
    else if(sid) goRoot();
  }});
tick(); setInterval(tick, 4000);
</script></body></html>
""".replace("%ANGLE_ORDER%", json.dumps(_ANGLE_ORDER))
