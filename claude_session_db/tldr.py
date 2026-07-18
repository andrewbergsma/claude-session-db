"""tldr — glanceable last-3-turns catch-up, one per session.

The "where is this session at?" angle: a compact summary of the LAST 3 FULL
TURNS (a full turn = a user prompt through the end of the assistant response,
tool calls included) with two facts an operator wants on open: (1) what the
conversation is about, (2) what the agent has been doing / is doing now.

Follows the angles probe doctrine (claudecode:design/turn-angles-context-
cockpit): extraction is code, the model only judges. The turn digest is built
deterministically (tool calls compressed to name(target), narration clipped),
then one small-model call produces {"about", "doing", "detail"}. Backend is
swappable — local Ollama by default (same model the angles probes use), a
`claude -p --model haiku` fallback behind CSD_TLDR_BACKEND=claude.

Caching / cadence:
  - store: $CSD_STATE_DIR/tldr/<session_id>.json
  - key:   turn_key = "<last real-prompt uuid>:<open|done>" — a new full turn
    (or the current turn completing) changes the key; anything else is a cache
    hit. The key is derived with a (mtime_ns, size)-signature memo so the
    nav-poll path never re-scans an unchanged transcript.
  - the request path NEVER generates: `request()` returns the cached store
    (stale or absent included) immediately and enqueues regeneration on a
    single daemon worker. Child (subagent) sessions and sessions idle >7 days
    are never auto-generated; force=True (the UI's refresh affordance)
    overrides both.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional

from .angles import (_is_real_prompt, _one_line, extract_turn,  # noqa: F401
                     find_session_jsonl, TurnDelta)

DEFAULT_BACKEND = os.environ.get("CSD_TLDR_BACKEND", "ollama")   # ollama | claude
DEFAULT_MODEL = os.environ.get(
    "CSD_TLDR_MODEL", os.environ.get("CSD_ANGLES_MODEL", "qwen2.5vl:7b"))
DEFAULT_OLLAMA_URL = os.environ.get("CSD_OLLAMA_URL", "http://localhost:11434")
CLAUDE_MODEL = os.environ.get("CSD_TLDR_CLAUDE_MODEL", "haiku")
GEN_TIMEOUT_S = int(os.environ.get("CSD_TLDR_TIMEOUT_S", "120"))
NUM_CTX = int(os.environ.get("CSD_TLDR_NUM_CTX", "8192"))

TLDR_TURNS = 3
MAX_AGE_S = 7 * 86400        # sessions idle longer are not auto-generated
SETTLE_S = 8                 # don't enqueue while the transcript is mid-write
HEADLINE_CAP = 200
DIGEST_CAP = 7000


def _state_dir() -> Path:
    base = os.environ.get("CSD_STATE_DIR",
                          str(Path.home() / ".local" / "state" / "claude-session-db"))
    d = Path(base) / "tldr"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- turn key (cache freshness) ----------------------------------------------------

_KEY_MEMO: dict[str, tuple[tuple[int, int], Optional[str]]] = {}
_DONE_RE = re.compile(rb'"stop_reason"\s*:\s*"(?:end_turn|stop_sequence)"')
_USER_RE = re.compile(rb'"type"\s*:\s*"user"')


def turn_key(path: Path) -> Optional[str]:
    """'<last real-prompt uuid>:<open|done>' for a transcript, else None.

    Changes exactly when a new full turn lands (new prompt) or the in-flight
    assistant response finishes (open -> done) — the two moments a tldr is
    worth recomputing. Memoized on the file's (mtime_ns, size) signature so
    the poll path only pays for changed files.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    sig = (st.st_mtime_ns, st.st_size)
    hit = _KEY_MEMO.get(str(path))
    if hit and hit[0] == sig:
        return hit[1]
    try:
        lines = path.read_bytes().split(b"\n")
    except OSError:
        return None
    last_uuid, last_idx = None, -1
    for i, ln in enumerate(lines):
        if not _USER_RE.search(ln):
            continue
        try:
            rec = json.loads(ln)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(rec, dict) and _is_real_prompt(rec):
            last_uuid, last_idx = rec.get("uuid"), i
    key = None
    if last_uuid:
        done = any(_DONE_RE.search(ln) for ln in lines[last_idx + 1:])
        key = f"{last_uuid}:{'done' if done else 'open'}"
    _KEY_MEMO[str(path)] = (sig, key)
    return key


# --- deterministic digest of the last N turns --------------------------------------

def _tool_line(name: str, inp: dict) -> str:
    """'name(target)' — the same compression the angle stream applies."""
    inp = inp or {}
    if name == "Bash":
        t = inp.get("description") or (inp.get("command") or "").split("\n")[0]
    elif name in ("Edit", "Write", "Read", "NotebookEdit", "MultiEdit"):
        p = inp.get("file_path") or inp.get("notebook_path") or ""
        t = "/".join(p.rstrip("/").split("/")[-2:])
    elif name in ("Agent", "Task"):
        t = f"{inp.get('subagent_type') or 'agent'}: {inp.get('description') or ''}"
    elif name == "Skill":
        t = inp.get("skill") or ""
    elif name == "SendMessage":
        t = inp.get("to") or inp.get("summary") or ""
    elif name.startswith("mcp__"):
        name = name.rsplit("__", 1)[-1]
        t = inp.get("path") or inp.get("query") or inp.get("application") or ""
    else:
        t = next((str(v) for v in inp.values() if isinstance(v, str)), "")
    return f"{name}({_one_line(str(t), 60)})"


def _turn_block(delta: TurnDelta, label: str) -> str:
    tools = [_tool_line(tu["name"], tu["input"]) for tu in delta.tool_uses[:25]]
    if len(delta.tool_uses) > 25:
        tools.append(f"(+{len(delta.tool_uses) - 25} more)")
    narration = " ".join(_one_line(t, 400) for t in delta.assistant_texts[:6])
    parts = [f"{label} @ {delta.started_at[:16]}",
             f"USER: {_one_line(delta.user_text, 400)}"]
    if narration:
        parts.append(f"AGENT: {narration[:900]}")
    if tools:
        parts.append("TOOLS: " + ", ".join(tools))
    return "\n".join(parts)


def build_digest(jsonl_path: Path, n_turns: int = TLDR_TURNS) -> str:
    """Compact, deterministic digest of the last n full turns (oldest first)."""
    blocks = []
    for back in range(n_turns, 0, -1):        # -3, -2, -1 -> oldest first
        try:
            delta = extract_turn(jsonl_path, turn=-back)
        except (ValueError, IndexError):
            continue
        blocks.append(_turn_block(delta, f"TURN -{back}"))
    if not blocks:
        raise ValueError(f"no complete turns in {jsonl_path.name}")
    return "\n\n".join(blocks)[:DIGEST_CAP]


# --- model backends ---------------------------------------------------------------

_TLDR_PROMPT = """Below is a digest of the last turns of a Claude Code \
coding-agent session (oldest first; tool calls compressed to name(target)). \
Write a glanceable catch-up so an operator opening a console instantly knows \
where the session is at. Do NOT continue the work or address anyone. \
Return ONLY JSON:
{"about": "what this conversation is about - one clause, max 100 chars",
 "doing": "what the agent has been doing / is doing now - one clause, \
present tense, max 120 chars",
 "detail": "a fuller 3-5 sentence catch-up paragraph"}

%s
"""


def _extract_json(text: str) -> dict:
    """First {...} object in a (possibly fenced) model response."""
    s = text.find("{")
    if s < 0:
        raise ValueError(f"no JSON in model response: {text[:150]!r}")
    return json.loads(text[s:text.rfind("}") + 1])


def _gen_ollama(prompt: str, model: str = DEFAULT_MODEL,
                base_url: str = DEFAULT_OLLAMA_URL) -> dict:
    payload = {"model": model, "prompt": prompt, "stream": False,
               "think": False,
               "options": {"temperature": 0.2, "num_ctx": NUM_CTX}}
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=GEN_TIMEOUT_S) as resp:
        text = (json.loads(resp.read().decode()).get("response") or "").strip()
    return _extract_json(text)


def _gen_claude(prompt: str, model: str = CLAUDE_MODEL, base_url: str = "") -> dict:
    """`claude -p --model haiku` fallback for machines without Ollama."""
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("claude CLI not found")
    p = subprocess.run([claude, "-p", "--model", model, prompt],
                       capture_output=True, text=True, timeout=GEN_TIMEOUT_S)
    if p.returncode != 0:
        raise RuntimeError(f"claude -p rc={p.returncode}: {p.stderr[:200]}")
    return _extract_json(p.stdout)


BACKENDS = {"ollama": _gen_ollama, "claude": _gen_claude}


# --- generate + store -------------------------------------------------------------

def generate(sid: str, jsonl_path: Path,
             backend: str = DEFAULT_BACKEND, model: Optional[str] = None) -> dict:
    """Build the digest, run the model, persist and return the store."""
    key = turn_key(jsonl_path)
    digest = build_digest(jsonl_path)
    gen = BACKENDS.get(backend, _gen_ollama)
    kwargs = {"model": model} if model else {}
    t0 = time.time()
    out = gen(_TLDR_PROMPT % digest, **kwargs)
    about = _one_line(str(out.get("about") or ""), 110)
    doing = _one_line(str(out.get("doing") or ""), 130)
    headline = _one_line(" — ".join(p for p in (about, doing) if p), HEADLINE_CAP)
    if not headline:
        raise ValueError(f"model returned no about/doing: {out!r}")
    store = {
        "session_id": sid,
        "turn_key": key,
        "headline": headline,
        "about": about,
        "doing": doing,
        "detail": _one_line(str(out.get("detail") or ""), 2000),
        "backend": backend,
        "model": model or (DEFAULT_MODEL if backend == "ollama" else CLAUDE_MODEL),
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


# --- async worker: serve stale-or-absent now, regenerate off the request path -----

_JOBS: "queue.Queue[tuple[str, Path, bool]]" = queue.Queue()
_QUEUED: set[str] = set()
_LOCK = threading.Lock()
_WORKER: Optional[threading.Thread] = None
STATUS: dict[str, str] = {}              # sid -> "queued" | "generating" | "ok" | error


def _work() -> None:
    while True:
        sid, path, force = _JOBS.get()
        STATUS[sid] = "generating"
        try:
            generate(sid, path)
            STATUS[sid] = "ok"
        except Exception as exc:  # noqa: BLE001 — one bad session ≠ dead worker
            STATUS[sid] = f"{type(exc).__name__}: {exc}"
            # Negative cache: stub keyed to the same turn so a session that
            # cannot tldr (e.g. a headless run with no real prompt) is not
            # re-queued on every poll — only when its turn changes.
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
                                       name="tldr-worker")
            _WORKER.start()


def request(sid: str, jsonl_path: Path, force: bool = False) -> Optional[dict]:
    """Cached store NOW (stale/absent included), regeneration queued async.

    Auto-generation is skipped for child (subagent) sessions, transcripts idle
    >7 days, and transcripts written within the last SETTLE_S (mid-turn);
    force lifts the first two (the per-session refresh affordance) but still
    respects settle.
    """
    cached = get_cached(sid)
    key = turn_key(jsonl_path)
    if key is None:
        return cached
    stale = cached is None or cached.get("turn_key") != key
    if not stale and not force:
        return cached
    if not force:
        if ":" in sid:                                   # subagent sidechain
            return cached
        try:
            idle = time.time() - jsonl_path.stat().st_mtime
        except OSError:
            return cached
        if idle > MAX_AGE_S:                             # dormant session
            return cached
        if idle < SETTLE_S:                              # mid-write; next poll
            return cached
    with _LOCK:
        if sid in _QUEUED:
            return cached
        _QUEUED.add(sid)
    STATUS[sid] = "queued"
    _JOBS.put((sid, jsonl_path, force))
    _ensure_worker()
    return cached


def payload(sid: str, jsonl_path: Path, force: bool = False) -> Optional[dict]:
    """The API-facing shape: cached fields + a staleness flag, or None."""
    cached = request(sid, jsonl_path, force=force)
    if not cached:
        return None
    key = turn_key(jsonl_path)
    return {**cached,
            "stale": bool(key and cached.get("turn_key") != key),
            "status": STATUS.get(sid)}
