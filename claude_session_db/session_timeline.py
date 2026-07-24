"""session_timeline — whole-session, time-stamped catch-up (button-launched).

The "read the whole thread in 20 seconds" angle. Where tldr.py summarizes the
LAST 3 TURNS into one glanceable headline, this walks the ENTIRE session and
emits one timeline row per user-prompt turn:

    14:02  You asked to add sortable columns; agent edited index.html + server.py.
    14:31  You reported the badge misaligned; agent fixed the CSS and committed.
    15:10  You asked for ≈#N equivalence matching; agent implemented it, opened PR.

Doctrine (same as the angles probes, claudecode:design/turn-angles-context-
cockpit): extraction is code, the model only judges. Each turn is segmented and
digested deterministically — **tool CALL names are kept, tool RESULTS are
dropped** (the "minus tool call results" the operator asked for) — then one
small-model call per segment yields a single catch-up sentence. Segment + map:
each call sees exactly one turn, so it scales to any session length and never
overflows a 7B/8K-ctx local model.

This is a *pull*, not a push: nothing auto-generates it. The console's tl;dr
button forces a (re)generation; the request path only ever serves the cached
store off disk. Older turns never change, so their summaries are memoized by
prompt uuid and only new/tail turns cost a model call on a repeat press.

Caching / cadence:
  - store: $CSD_STATE_DIR/timeline/<session_id>.json
  - key:   the same turn_key tldr.py uses ('<last real-prompt uuid>:<open|done>')
  - memo:  {prompt_uuid: summary} — a completed turn is summarized once ever;
    the final (possibly in-flight) turn is always recomputed on a forced run.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional

from .angles import (_is_real_prompt, _one_line, _text_of, subagent_key,
                     load_jsonl)
from .tldr import _tool_line, turn_key

DEFAULT_MODEL = os.environ.get(
    "CSD_TIMELINE_MODEL", os.environ.get("CSD_ANGLES_MODEL", "qwen2.5vl:7b"))
DEFAULT_OLLAMA_URL = os.environ.get("CSD_OLLAMA_URL", "http://localhost:11434")
GEN_TIMEOUT_S = int(os.environ.get("CSD_TIMELINE_TIMEOUT_S", "60"))
NUM_CTX = int(os.environ.get("CSD_TIMELINE_NUM_CTX", "4096"))
MAX_TURNS = int(os.environ.get("CSD_TIMELINE_MAX_TURNS", "150"))
SEG_CAP = 4000
SUMMARY_CAP = 260

# Pure interruption markers: a real "user" record by _is_real_prompt, but no
# instruction — when the turn carries no agent work either, it's a noise row.
_INTERRUPT_MARKERS = {
    "[request interrupted by user]",
    "[request interrupted by user for tool use]",
}


def _state_dir() -> Path:
    base = os.environ.get("CSD_STATE_DIR",
                          str(Path.home() / ".local" / "state" / "claude-session-db"))
    d = Path(base) / "timeline"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- deterministic per-turn segmentation (loads the transcript once) ---------------

def _segments(jsonl_path: Path) -> list[dict]:
    """One dict per user-prompt turn: {uuid, t, user_text, narration, tools}.

    Sidechain records are folded in only for a child transcript (subagent_key);
    on a main session they belong to the child rows, not the parent's timeline.
    Tool RESULTS are never collected — only compressed call names(target)."""
    recs = load_jsonl(jsonl_path)
    child = subagent_key(jsonl_path)
    prompt_idx = [i for i, r in enumerate(recs)
                  if _is_real_prompt(r, allow_sidechain=bool(child))]
    segs: list[dict] = []
    for pos, start in enumerate(prompt_idx):
        end = prompt_idx[pos + 1] if pos + 1 < len(prompt_idx) else len(recs)
        span = recs[start:end]
        opener = span[0]
        narration: list[str] = []
        tools: list[str] = []
        for rec in span:
            if rec.get("isSidechain") and not child:
                continue
            if rec.get("type") != "assistant":
                continue
            content = rec.get("message", {}).get("content")
            t = _text_of(content).strip()
            if t:
                narration.append(t)
            for b in content or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tools.append(_tool_line(b.get("name", "?"), b.get("input", {})))
        user_text = _text_of(opener.get("message", {}).get("content")).strip()
        # Drop a pure interruption marker that carried no work — it's a noise
        # row, not a "what were we working on" line. Span boundaries are left
        # untouched (the marker's content, if any, still folds into its span).
        if (user_text.lower() in _INTERRUPT_MARKERS
                and not narration and not tools):
            continue
        segs.append({
            "uuid": opener.get("uuid") or f"idx{start}",
            "t": opener.get("timestamp", ""),
            "user_text": user_text,
            "narration": narration,
            "tools": tools,
        })
    return segs


def _seg_digest(seg: dict) -> str:
    parts = [f"USER: {_one_line(seg['user_text'], 500)}"]
    narr = " ".join(_one_line(t, 400) for t in seg["narration"][:8])
    if narr:
        parts.append(f"AGENT: {narr[:1400]}")
    if seg["tools"]:
        shown = ", ".join(seg["tools"][:30])
        if len(seg["tools"]) > 30:
            shown += f" (+{len(seg['tools']) - 30} more)"
        parts.append("TOOLS: " + shown)
    return "\n".join(parts)[:SEG_CAP]


# --- model backend (one call per turn) ---------------------------------------------

_SEG_PROMPT = """This is ONE turn of a Claude Code coding-agent session (tool \
CALL names are shown as name(target); tool RESULTS are omitted). In ONE sentence, \
past tense, say what the user asked for and what the agent did in response — a \
catch-up line for a session timeline. Be concrete (name files/actions). Do NOT \
continue the work or address anyone. Return ONLY JSON:
{"summary": "one sentence, max 180 chars"}

%s
"""


def _clip(text: str, cap: int) -> str:
    """Whitespace-normalize, then truncate at a word boundary (never mid-word)
    with an ellipsis if it overruns the cap."""
    s = _one_line(text, 10_000)
    if len(s) <= cap:
        return s
    cut = s[:cap].rsplit(" ", 1)[0].rstrip(",.;:")
    return (cut or s[:cap]) + "…"


def _extract_json(text: str) -> dict:
    s = text.find("{")
    if s < 0:
        raise ValueError(f"no JSON in model response: {text[:150]!r}")
    return json.loads(text[s:text.rfind("}") + 1])


def _summarize_segment(seg: dict, model: str, base_url: str) -> str:
    payload = {"model": model, "prompt": _SEG_PROMPT % _seg_digest(seg),
               "stream": False, "think": False,
               "options": {"temperature": 0.2, "num_ctx": NUM_CTX}}
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=GEN_TIMEOUT_S) as resp:
        text = (json.loads(resp.read().decode()).get("response") or "").strip()
    return _clip(str(_extract_json(text).get("summary") or ""), SUMMARY_CAP)


# --- generate + store --------------------------------------------------------------

def generate(sid: str, jsonl_path: Path, model: str = DEFAULT_MODEL,
             base_url: str = DEFAULT_OLLAMA_URL) -> dict:
    """Walk every turn, summarize each (reusing memoized completed turns), and
    persist the timeline. The final turn is always recomputed — it may be the
    in-flight one, and its summary changes as the response lands."""
    key = turn_key(jsonl_path)
    segs = _segments(jsonl_path)
    if not segs:
        raise ValueError(f"no user prompts in {jsonl_path.name}")
    truncated_older = 0
    if len(segs) > MAX_TURNS:                 # bound the model calls; keep the tail
        truncated_older = len(segs) - MAX_TURNS
        segs = segs[-MAX_TURNS:]

    prev = get_cached(sid) or {}
    memo: dict[str, str] = dict(prev.get("memo") or {})
    t0 = time.time()
    rows: list[dict] = []
    last_i = len(segs) - 1
    for i, seg in enumerate(segs):
        STATUS[sid] = f"generating {i + 1}/{len(segs)}"
        cached = memo.get(seg["uuid"])
        if cached is not None and i != last_i:     # completed turn — reuse
            summary = cached
        else:
            try:
                summary = _summarize_segment(seg, model, base_url)
            except Exception as exc:  # noqa: BLE001 — one bad turn ≠ dead timeline
                summary = f"(unavailable: {type(exc).__name__})"
            if not summary.startswith("(unavailable"):
                memo[seg["uuid"]] = summary
        rows.append({"uuid": seg["uuid"], "t": seg["t"], "summary": summary})

    store = {
        "session_id": sid,
        "turn_key": key,
        "rows": rows,
        "memo": memo,
        "turn_count": len(rows),
        "truncated_older": truncated_older,
        "model": model,
        "latency_s": round(time.time() - t0, 1),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    _persist(sid, store)
    return store


def _persist(sid: str, store: dict) -> None:
    f = _state_dir() / f"{_safe_name(sid)}.json"
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=1))
    tmp.replace(f)                       # atomic; a reader never sees a torn store


def _safe_name(sid: str) -> str:
    return sid.replace(":", "__")        # child keys carry ':'


def get_cached(sid: str) -> Optional[dict]:
    try:
        return json.loads((_state_dir() / f"{_safe_name(sid)}.json").read_text())
    except (OSError, ValueError):
        return None


# --- async worker: serve cached now, generate off the request path ------------------

_JOBS: "queue.Queue[tuple[str, Path]]" = queue.Queue()
_QUEUED: set[str] = set()
_LOCK = threading.Lock()
_WORKER: Optional[threading.Thread] = None
STATUS: dict[str, str] = {}              # sid -> "queued" | "generating i/n" | "ok" | error


def _work() -> None:
    while True:
        sid, path = _JOBS.get()
        try:
            generate(sid, path)
            STATUS[sid] = "ok"
        except Exception as exc:  # noqa: BLE001 — one bad session ≠ dead worker
            STATUS[sid] = f"{type(exc).__name__}: {exc}"
            _persist(sid, {"session_id": sid, "turn_key": turn_key(path),
                           "error": f"{type(exc).__name__}: {exc}"[:300],
                           "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        finally:
            with _LOCK:
                _QUEUED.discard(sid)


def _ensure_worker() -> None:
    global _WORKER
    with _LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_work, daemon=True,
                                       name="timeline-worker")
            _WORKER.start()


def _enqueue(sid: str, jsonl_path: Path) -> None:
    with _LOCK:
        if sid in _QUEUED:
            return
        _QUEUED.add(sid)
    STATUS[sid] = "queued"
    _JOBS.put((sid, jsonl_path))
    _ensure_worker()


def payload(sid: str, jsonl_path: Path, force: bool = False) -> Optional[dict]:
    """The API shape. Unlike tldr, generation is NEVER automatic: `force`
    (the button) is the only thing that enqueues. Without it we return the
    cached store (or None) plus a staleness flag so the UI can offer refresh."""
    key = turn_key(jsonl_path)
    if force and key is not None:
        _enqueue(sid, jsonl_path)
    cached = get_cached(sid)
    status = STATUS.get(sid)
    generating = (status or "").startswith(("queued", "generating"))
    if not cached:
        if not force:
            return None
        return {"session_id": sid, "rows": [], "status": status,
                "generating": True, "stale": True}
    return {**cached,
            "stale": bool(key and cached.get("turn_key") != key),
            "generating": generating,
            "status": status}
