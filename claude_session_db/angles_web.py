"""angles_web — ambient multi-session angles dashboard (spike).

An out-of-band surface for the turn-angles loop: a watcher tails every live
Claude Code transcript under ~/.claude/projects, re-mines a session's latest
turn (via angles.run_angles) whenever its JSONL settles, and a tiny stdlib
HTTP server renders one row per session on the LAN — direction, files, errors,
kmcp writes, token burn — with every headline's detail one click away.

Doctrine note (claudecode:design/turn-angles-context-cockpit): pull-not-push
governs the CONVERSATION surface; this dashboard is the ambient exception —
zero context tokens, zero interruption, glanceable. Probes run through a
single-worker queue so N live sessions never stampede the local Ollama.

No auth: LAN-trusted, read-only over the angles state dir. Do not expose
beyond the local network.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from . import angles as A

SCAN_INTERVAL_S = 5          # transcript poll cadence
DEBOUNCE_S = 8               # file must be quiet this long before mining
DEFAULT_LIVE_WINDOW_S = 1800  # transcript mtime within this = live session
DEFAULT_PORT = 8791


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
        for p in A.PROJECTS_DIR.glob("*/*.jsonl"):
            try:
                st = p.stat()
            except OSError:
                continue
            if now - st.st_mtime > self.window_s:
                continue
            if now - st.st_mtime < DEBOUNCE_S:
                continue  # still being written; next scan will see it settled
            sid = p.stem
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
        transcript = next(iter(A.PROJECTS_DIR.glob(f"*/{sid}.jsonl")), None)
        t_mtime = transcript.stat().st_mtime if transcript else 0
        if now - t_mtime > 86400:  # drop day-old sessions from the board
            continue
        headlines: dict[str, list[dict]] = {}
        for iid, item in store.get("items", {}).items():
            headlines.setdefault(item.get("angle", "?"), []).append(
                {"id": iid, "headline": item.get("headline", "")})
        out.append({
            "session_id": sid,
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
    out.sort(key=lambda s: (not s["live"], s["pull_age_s"]))
    return out


def _detail_payload(sid: str, item_id: str) -> dict[str, Any]:
    f = A._state_dir() / f"{sid}.json"
    if not f.exists():
        return {"error": f"no angles for {sid}"}
    store = json.loads(f.read_text())
    return store.get("items", {}).get(item_id.upper(),
                                      {"error": f"{item_id} not found"})


# --- HTTP -----------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    watcher: AngleWatcher  # injected by serve()

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
        path = urlparse(self.path).path
        try:
            if path == "/":
                self._send(200, _PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/sessions":
                self._json(_sessions_payload(self.watcher))
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
          kmcp_dsn: Optional[str], no_probes: bool) -> None:
    watcher = AngleWatcher(window_s, model, base_url, kmcp_dsn, no_probes)
    watcher.start()
    Handler.watcher = watcher
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"angles dashboard on http://{host}:{port}/  "
          f"(live window {window_s}s, probes {'OFF' if no_probes else model})")
    httpd.serve_forever()


# --- UI (inline, self-contained) --------------------------------------------------

_ANGLE_ORDER = ["direction", "events", "files", "kmcp", "commands",
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
  #tabs { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:0; }
  .tab { background:var(--card); border:1px solid var(--line);
         border-bottom:none; border-radius:8px 8px 0 0; padding:7px 12px 6px;
         cursor:pointer; color:var(--dim); display:flex; gap:7px;
         align-items:center; max-width:240px; }
  .tab .t-slug { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .tab.active { color:var(--fg); background:#1b2330; border-color:var(--acc); }
  .tab.stale { opacity:.5; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%;
         background:var(--dim); flex:none; }
  .live .dot { background:var(--ok); }
  .mining .dot { background:var(--warn); animation:pulse 1s infinite; }
  @keyframes pulse { 50% { opacity:.3; } }
  #panel { background:#1b2330; border:1px solid var(--acc); border-radius:0 8px 8px 8px;
           padding:14px 16px; }
  .kv { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr));
        gap:4px 22px; margin-bottom:10px; padding-bottom:10px;
        border-bottom:1px solid var(--line); }
  .kv div { display:flex; gap:8px; min-width:0; }
  .kv .k { color:var(--dim); flex:none; width:86px; text-align:right;
           font-size:12px; padding-top:1px; }
  .kv .v { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .prompt { color:var(--fg); margin:2px 0 10px; font-style:italic; }
  .angle { display:flex; gap:8px; margin:2px 0; }
  .aname { color:var(--dim); width:82px; flex:none; text-align:right;
           font-size:12px; padding-top:1px; }
  .items { flex:1; min-width:0; }
  .hl { cursor:pointer; white-space:nowrap; overflow:hidden;
        text-overflow:ellipsis; }
  .hl:hover { color:var(--acc); }
  .hl .id { color:var(--warn); margin-right:6px; }
  .angle.errors .hl { color:var(--err); }
  .angle.direction .hl { color:#e3b341; }
  .angle.knowledge .hl .id { color:var(--acc); }
  #detail { position:fixed; inset:auto 16px 16px auto; width:min(680px,90vw);
            max-height:70vh; overflow:auto; background:#0a0d12;
            border:1px solid var(--acc); border-radius:8px; padding:14px;
            display:none; z-index:9; }
  #detail pre { white-space:pre-wrap; word-break:break-all; font-size:12px; }
  #detail .x { float:right; cursor:pointer; color:var(--dim); }
  .empty { color:var(--dim); padding:40px; text-align:center; }
</style></head><body>
<h1><b>angles</b> · ambient turn dashboard · <span id="stat">…</span></h1>
<div id="tabs"></div><div id="panel"><div class="empty">loading…</div></div>
<div id="detail"><span class="x" onclick="hide()">✕ close</span><pre id="dbody"></pre></div>
<script>
const ORDER = %ANGLE_ORDER%;
let rows = [], sel = null, lastJson = "";
function age(s){ if(s==null) return "?";
  return s<60? s+"s" : s<3600? Math.round(s/60)+"m" : Math.round(s/3600)+"h"; }
function kb(n){ return n>1048576? (n/1048576).toFixed(1)+" MB" :
  Math.round((n||0)/1024)+" KB"; }
function ts(t){ if(!t) return "?"; const d=new Date(t);
  return d.toLocaleDateString(undefined,{month:"short",day:"numeric"})+" "+
         d.toLocaleTimeString(undefined,{hour:"2-digit",minute:"2-digit"}); }
function dur(a,b){ if(!a) return "?"; const ms=(b?new Date(b):new Date())-new Date(a);
  const m=Math.round(ms/60000); return m<60? m+"m" : (m/60).toFixed(1)+"h"; }
function esc(t){ const d=document.createElement("i"); d.textContent=t??"";
  return d.innerHTML; }
function pick(sid){ sel=sid; render(); }
function render(){
  const tabs=document.getElementById("tabs"),
        panel=document.getElementById("panel");
  if(!rows.length){ tabs.innerHTML="";
    panel.innerHTML='<div class="empty">no live sessions — the watcher mines '+
      'each transcript as it settles</div>'; return; }
  if(!rows.some(s=>s.session_id===sel)) sel=rows[0].session_id;
  tabs.innerHTML=rows.map(s=>{
    const mining=s.status==="mining";
    return `<div class="tab ${s.live?"live":"stale"} ${mining?"mining":""} `+
      `${s.session_id===sel?"active":""}" onclick="pick('${s.session_id}')">`+
      `<span class="dot"></span><span class="t-slug">`+
      `${esc(s.slug||((s.cwd||"").split("/").pop()||"")+" "+s.session_id.slice(0,6))}</span></div>`;
  }).join("");
  const s=rows.find(r=>r.session_id===sel); if(!s) return;
  const u=s.usage||{}, mining=s.status==="mining";
  const kv=[
    ["directory", s.cwd||"?"],
    ["branch", s.git_branch||"—"],
    ["started", ts(s.session_started_at)],
    ["duration", dur(s.session_started_at, s.live?null:s.turn_span[1])],
    ["last turn", age(s.transcript_age_s)+" ago"],
    ["pulled", age(s.pull_age_s)+" ago"+(mining?" · ⛏ mining":"")],
    ["prompts", (s.prompt_count||"?")+" · "+(s.record_count||"?")+" records"],
    ["turn cost", (u.tool_calls??"?")+" tools · "+
      (u.input_tokens??0).toLocaleString()+" in / "+
      (u.output_tokens??0).toLocaleString()+" out"],
    ["model", (s.model||"?")+(s.cc_version?" · cc "+s.cc_version:"")],
    ["transcript", kb(s.transcript_bytes)],
    ["session", s.session_id],
  ];
  const meta=kv.map(([k,v])=>`<div><span class="k">${k}</span>`+
    `<span class="v" title="${esc(v)}">${esc(v)}</span></div>`).join("");
  const angles=ORDER.filter(a=>s.headlines[a]).map(a=>{
    const items=s.headlines[a].map(h=>
      `<div class="hl" onclick="show('${s.session_id}','${h.id}')">`+
      `<span class="id">${h.id}</span>${esc(h.headline)}</div>`).join("");
    return `<div class="angle ${a}"><div class="aname">${a}</div>`+
           `<div class="items">${items}</div></div>`;
  }).join("") || '<div class="empty">no angles yet</div>';
  const warn=s.status&&s.status!=="ok"&&!mining
    ? `<div style="color:var(--err);margin-bottom:8px">⚠ ${esc(s.status)}</div>`:"";
  panel.innerHTML=`<div class="kv">${meta}</div>${warn}`+
    `<div class="prompt">“${esc(s.user_text)}”</div>${angles}`;
}
async function tick(){
  try{
    const r=await fetch("/api/sessions"); const txt=await r.text();
    document.getElementById("stat").textContent=new Date().toLocaleTimeString();
    if(txt===lastJson) return; lastJson=txt;
    rows=JSON.parse(txt); render();
  }catch(e){ document.getElementById("stat").textContent="offline: "+e; }
}
async function show(sid,id){
  const r=await fetch(`/api/detail/${sid}/${id}`);
  document.getElementById("dbody").textContent=
    JSON.stringify(await r.json(),null,2);
  document.getElementById("detail").style.display="block";
}
function hide(){ document.getElementById("detail").style.display="none"; }
document.addEventListener("keydown",e=>{ if(e.key==="Escape") hide(); });
tick(); setInterval(tick, 4000);
</script></body></html>
""".replace("%ANGLE_ORDER%", json.dumps(_ANGLE_ORDER))
