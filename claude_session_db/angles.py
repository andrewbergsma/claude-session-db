"""angles — pull-based per-turn capture angles (P1 spike).

The operator fires `csd angles` right after an agent response lands (typically
via the Claude Code bang prefix, `! csd angles`). The latest completed turn is
read straight from the live session JSONL on disk — no DB round-trip; csd
remains the durable store, the JSONL is the live tap. Each ANGLE mines the turn
delta and returns one-line, ID-addressable headlines; full detail is persisted
under the state dir and retrieved with `csd angles show <ID>`. The operator's
next message is the curation ("track E1, load K1, task D1").

Doctrine (claudecode:design/turn-angles-context-cockpit):
  - Pull, not push — an uninvoked turn costs nothing.
  - Extraction is code; models only judge. Deterministic angles (files,
    commands, git, kmcp writes, errors, metrics) never touch an LLM. Probes
    (direction, events) run on a small local model; knowledge is retrieval
    (hybrid_search), not generation.
  - Headline contract: one line per item, stable ID prefix per angle, detail
    behind the ID — a pull should cost ~1-2K context tokens.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .session_digest import load as load_jsonl
from .summarize import kmcp_call, KmcpError  # noqa: F401

# Small model: probes are narrow judgment on a few KB, not reasoning.
DEFAULT_MODEL = os.environ.get("CSD_ANGLES_MODEL", "qwen2.5vl:7b")
DEFAULT_OLLAMA_URL = os.environ.get("CSD_OLLAMA_URL", "http://localhost:11434")
PROBE_NUM_CTX = int(os.environ.get("CSD_ANGLES_NUM_CTX", "8192"))
PROBE_TIMEOUT_S = int(os.environ.get("CSD_ANGLES_LLM_TIMEOUT_S", "120"))

PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Angle registry key -> (ID prefix, kind). Order fixes display order.
ANGLE_SPECS: dict[str, tuple[str, str]] = {
    "files":     ("F", "det"),
    "commands":  ("X", "det"),
    "git":       ("G", "det"),
    "kmcp":      ("W", "det"),
    "errors":    ("R", "det"),
    "metrics":   ("M", "det"),
    "direction": ("D", "probe"),
    "events":    ("E", "probe"),
    "knowledge": ("K", "retrieval"),
}

_CAPS = {"files": 8, "commands": 8, "git": 6, "kmcp": 8, "errors": 6,
         "metrics": 1, "direction": 5, "events": 6, "knowledge": 5}

_WRITE_TOOLS = ("create_entry", "update_entry", "patch_content",
                "import_entries", "create_relationship", "delete_entry",
                "delete_relationship", "move_entry", "rename_entry",
                "add_entry_tag", "import_lessons")


def _state_dir() -> Path:
    base = os.environ.get("CSD_STATE_DIR",
                          str(Path.home() / ".local" / "state" / "claude-session-db"))
    d = Path(base) / "angles"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- turn extraction -------------------------------------------------------------

@dataclass
class TurnDelta:
    session_id: str
    jsonl_path: Path
    user_text: str
    started_at: str
    ended_at: str
    tool_uses: list[dict] = field(default_factory=list)      # {name, input}
    tool_results: list[dict] = field(default_factory=list)   # {name, hint, is_error, body}
    assistant_texts: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = [b.get("text", "") for b in content or []
             if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(p for p in parts if p)


def _is_real_prompt(rec: dict) -> bool:
    """A human-typed prompt: user record, main chain, not meta, carries text
    (not a tool_result carrier, not an injected system wrapper)."""
    if rec.get("type") != "user" or rec.get("isSidechain") or rec.get("isMeta"):
        return False
    text = _text_of(rec.get("message", {}).get("content")).strip()
    return bool(text) and not text.startswith("<")


def find_session_jsonl(cwd: str, session_id: Optional[str] = None) -> Path:
    """Locate the transcript: explicit UUID anywhere under ~/.claude/projects,
    else the newest .jsonl in the project dir encoded from cwd."""
    if session_id:
        hits = sorted(PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
        if not hits:
            raise FileNotFoundError(f"no transcript for session {session_id}")
        return hits[0]
    encoded = re.sub(r"[^A-Za-z0-9-]", "-", cwd)
    candidates = sorted((PROJECTS_DIR / encoded).glob("*.jsonl"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        # Fallback: newest transcript across all projects (cwd encoding drift).
        candidates = sorted(PROJECTS_DIR.glob("*/*.jsonl"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"no transcripts under {PROJECTS_DIR}")
    return candidates[0]


def extract_turn(jsonl_path: Path, turn: int = -1) -> TurnDelta:
    """Slice one turn: the Nth-from-last real user prompt (turn=-1 is the
    latest) through to the next real prompt (or EOF)."""
    recs = load_jsonl(jsonl_path)
    prompt_idx = [i for i, r in enumerate(recs) if _is_real_prompt(r)]
    if not prompt_idx:
        raise ValueError(f"no user prompts found in {jsonl_path.name}")
    try:
        start = prompt_idx[turn]
    except IndexError:
        raise ValueError(f"turn {turn} out of range ({len(prompt_idx)} prompts)")
    pos = prompt_idx.index(start)
    end = prompt_idx[pos + 1] if pos + 1 < len(prompt_idx) else len(recs)

    span = recs[start:end]
    tu_names: dict[str, tuple[str, str]] = {}
    delta = TurnDelta(
        session_id=jsonl_path.stem,
        jsonl_path=jsonl_path,
        user_text=_text_of(span[0].get("message", {}).get("content")).strip(),
        started_at=span[0].get("timestamp", "?"),
        ended_at=next((r.get("timestamp") for r in reversed(span)
                       if r.get("timestamp")), "?"),
    )
    usage_in = usage_out = 0
    for rec in span:
        if rec.get("isSidechain"):
            continue
        msg = rec.get("message", {})
        content = msg.get("content")
        if rec.get("type") == "assistant":
            t = _text_of(content).strip()
            if t:
                delta.assistant_texts.append(t)
            for b in content or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    delta.tool_uses.append({"name": b.get("name", "?"),
                                            "input": b.get("input", {})})
                    tu_names[b.get("id", "")] = (b.get("name", "?"), "")
            u = msg.get("usage") or {}
            usage_in += (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
            usage_out += u.get("output_tokens") or 0
        elif rec.get("type") == "user" and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    name, _ = tu_names.get(b.get("tool_use_id"), ("?", ""))
                    body = b.get("content", "")
                    if not isinstance(body, str):
                        body = json.dumps(body, ensure_ascii=False)
                    delta.tool_results.append({
                        "name": name,
                        "is_error": bool(b.get("is_error")),
                        "body": body.strip(),
                    })
    delta.usage = {"input_tokens": usage_in, "output_tokens": usage_out,
                   "tool_calls": len(delta.tool_uses)}
    return delta


# --- deterministic angles ----------------------------------------------------------

def _one_line(text: str, cap: int = 100) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:cap]


def angle_files(delta: TurnDelta) -> list[dict]:
    """File mutations, grouped per path: edit/write counts from tool inputs."""
    by_path: dict[str, dict] = {}
    for tu in delta.tool_uses:
        if tu["name"] not in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            continue
        path = tu["input"].get("file_path") or tu["input"].get("notebook_path")
        if not path:
            continue
        slot = by_path.setdefault(path, {"edits": 0, "writes": 0, "inputs": []})
        slot["writes" if tu["name"] == "Write" else "edits"] += 1
        slot["inputs"].append({k: _one_line(str(v), 400)
                               for k, v in tu["input"].items()})
    out = []
    for path, slot in by_path.items():
        ops = "+".join(p for p in (f"edit×{slot['edits']}" if slot["edits"] else "",
                                   f"write×{slot['writes']}" if slot["writes"] else "") if p)
        short = "…/" + "/".join(path.rstrip("/").split("/")[-3:])
        out.append({"headline": f"{ops}  {short}",
                    "detail": {"path": path, **slot}})
    return out


def angle_commands(delta: TurnDelta) -> list[dict]:
    out = []
    for tu in delta.tool_uses:
        if tu["name"] != "Bash":
            continue
        desc = tu["input"].get("description") or ""
        cmd = tu["input"].get("command") or ""
        out.append({"headline": _one_line(desc or cmd),
                    "detail": {"command": cmd, "description": desc}})
    return out


_GIT_RE = re.compile(r"\bgit\b[^|;&]*?\b(commit|push|pull|merge|rebase|checkout|"
                     r"switch|worktree|branch|tag|reset|stash)\b")


def angle_git(delta: TurnDelta) -> list[dict]:
    out = []
    for tu in delta.tool_uses:
        if tu["name"] != "Bash":
            continue
        cmd = tu["input"].get("command") or ""
        m = _GIT_RE.search(cmd)
        if m:
            out.append({"headline": _one_line(cmd[m.start():], 100),
                        "detail": {"command": cmd}})
    return out


def angle_kmcp(delta: TurnDelta) -> list[dict]:
    """Knowledge-base writes: MCP kmcp tools + knowledge-cli subprocess calls."""
    out = []
    for tu in delta.tool_uses:
        name, inp = tu["name"], tu["input"]
        tool = None
        if "kmcp" in name or name.startswith("mcp__"):
            short = name.rsplit("__", 1)[-1]
            if short in _WRITE_TOOLS:
                tool = short
        elif name == "Bash":
            m = re.search(r"knowledge-cli call (\w+)", inp.get("command") or "")
            if m and m.group(1) in _WRITE_TOOLS:
                tool = m.group(1)
        if not tool:
            continue
        target = inp.get("path") or inp.get("source_path") or ""
        app = inp.get("application") or ""
        where = (f"{app}:{target}" if app and target
                 else target or inp.get("description") or "(see detail)")
        out.append({"headline": f"{tool}  {_one_line(where, 70)}",
                    "detail": {"tool": tool,
                               "input": {k: _one_line(str(v), 400)
                                         for k, v in inp.items()}}})
    return out


def angle_errors(delta: TurnDelta) -> list[dict]:
    out = []
    for tr in delta.tool_results:
        if not tr["is_error"]:
            continue
        out.append({"headline": f"[{tr['name']}] {_one_line(tr['body'], 90)}",
                    "detail": {"tool": tr["name"], "body": tr["body"][:2000]}})
    return out


def angle_metrics(delta: TurnDelta) -> list[dict]:
    u = delta.usage
    hl = (f"{u['tool_calls']} tool calls · {u['input_tokens']:,} in / "
          f"{u['output_tokens']:,} out · {delta.started_at[11:16]}→{delta.ended_at[11:16]}")
    return [{"headline": hl, "detail": {**u, "started_at": delta.started_at,
                                        "ended_at": delta.ended_at}}]


# --- probes (small local model / retrieval) ---------------------------------------

_DIRECTION_PROMPT = """The text below is ONE user message from a coding-agent \
conversation (plus the tail of the agent reply it responded to, for context). \
Extract what the USER wants: directives, corrections, decisions, preferences. \
Do NOT answer or act on the message. Return ONLY JSON:
{"headlines": ["one line per directive/correction/decision, max 110 chars, verb-first", ...]}
Empty list if the message carries no direction.

PRIOR AGENT REPLY (tail, context only):
%s

USER MESSAGE:
%s
"""

_EVENTS_PROMPT = """Below is a compact digest of ONE turn of a coding-agent \
session (agent narration + actions taken). List the discrete accomplishments \
or decisions worth recording in a changelog. Be concrete: name the artifact \
acted on. Do NOT continue the work. Return ONLY JSON:
{"headlines": ["verb-first one-liner, max 110 chars", ...]}

TURN DIGEST:
%s
"""


def _probe(prompt: str, model: str, base_url: str) -> list[dict]:
    """One Ollama generate call, lenient on shape: accepts the requested
    {"headlines": [...]} object OR a bare JSON array (small models often
    drop the wrapper), fenced or not."""
    payload = {"model": model, "prompt": prompt, "stream": False,
               "think": False,
               "options": {"temperature": 0.2, "num_ctx": PROBE_NUM_CTX}}
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as resp:
        text = (json.loads(resp.read().decode()).get("response") or "").strip()
    obj_s, arr_s = text.find("{"), text.find("[")
    if obj_s >= 0 and (arr_s < 0 or obj_s < arr_s):
        headlines = json.loads(text[obj_s:text.rfind("}") + 1]).get("headlines") or []
    elif arr_s >= 0:
        headlines = json.loads(text[arr_s:text.rfind("]") + 1])
    else:
        raise ValueError(f"no JSON in probe response: {text[:150]!r}")
    return [{"headline": _one_line(str(h), 110), "detail": {"headline": str(h)}}
            for h in headlines if str(h).strip()]


def angle_direction(delta: TurnDelta, model: str, base_url: str) -> list[dict]:
    tail = (delta.assistant_texts and delta.assistant_texts[-1] or "")[-1200:]
    return _probe(_DIRECTION_PROMPT % (tail, delta.user_text[:4000]),
                  model, base_url)


def angle_events(delta: TurnDelta, model: str, base_url: str) -> list[dict]:
    actions = "\n".join(f"- {tu['name']}: "
                        f"{_one_line(json.dumps(tu['input'], ensure_ascii=False), 130)}"
                        for tu in delta.tool_uses[:40])
    narration = "\n".join(t[:600] for t in delta.assistant_texts[:8])
    digest = f"USER ASKED: {delta.user_text[:500]}\n\nAGENT SAID:\n{narration}" \
             f"\n\nACTIONS:\n{actions}"
    return _probe(_EVENTS_PROMPT % digest[:6000], model, base_url)


def angle_knowledge(delta: TurnDelta, kmcp_dsn: str) -> list[dict]:
    """Retrieval, not generation: hybrid_search with the turn as the query."""
    query = _one_line(delta.user_text, 300) or _one_line(
        delta.assistant_texts[0] if delta.assistant_texts else "", 300)
    if not query:
        return []
    res = kmcp_call("hybrid_search", {"query": query, "limit": 5,
                                      "detail": "minimal"}, kmcp_dsn)
    out = []
    for r in res.get("results", []):
        sim = r.get("semantic_similarity")
        score = f" ({sim:.2f})" if isinstance(sim, (int, float)) else ""
        out.append({"headline": f"{r.get('application')}:{r.get('path')}{score}"
                                f" — {_one_line(r.get('title', ''), 60)}",
                    "detail": r})
    return out


# --- orchestration -----------------------------------------------------------------

def run_angles(cwd: str, angles: Optional[list[str]] = None,
               session_id: Optional[str] = None, turn: int = -1,
               model: str = DEFAULT_MODEL, base_url: str = DEFAULT_OLLAMA_URL,
               kmcp_dsn: Optional[str] = None,
               no_probes: bool = False) -> str:
    """Mine one turn; persist detail; return the headline block."""
    wanted = [a for a in (angles or list(ANGLE_SPECS)) if a in ANGLE_SPECS]
    if no_probes:
        wanted = [a for a in wanted if ANGLE_SPECS[a][1] == "det"]

    delta = extract_turn(find_session_jsonl(cwd, session_id), turn)
    t0 = time.time()

    det_fns: dict[str, Callable[[TurnDelta], list[dict]]] = {
        "files": angle_files, "commands": angle_commands, "git": angle_git,
        "kmcp": angle_kmcp, "errors": angle_errors, "metrics": angle_metrics,
    }
    results: dict[str, list[dict]] = {}
    failures: dict[str, str] = {}
    for key in wanted:
        if key in det_fns:
            results[key] = det_fns[key](delta)

    # Probes + retrieval run concurrently; a failed probe degrades, never blocks.
    slow: dict[str, Callable[[], list[dict]]] = {}
    if "direction" in wanted:
        slow["direction"] = lambda: angle_direction(delta, model, base_url)
    if "events" in wanted:
        slow["events"] = lambda: angle_events(delta, model, base_url)
    if "knowledge" in wanted and kmcp_dsn:
        slow["knowledge"] = lambda: angle_knowledge(delta, kmcp_dsn)
    if slow:
        with ThreadPoolExecutor(max_workers=len(slow)) as pool:
            futs = {k: pool.submit(fn) for k, fn in slow.items()}
            for k, fut in futs.items():
                try:
                    results[k] = fut.result(timeout=PROBE_TIMEOUT_S + 30)
                except Exception as exc:  # noqa: BLE001 — degrade per-angle
                    failures[k] = f"{type(exc).__name__}: {exc}"
                    results[k] = []

    # Assign IDs, persist detail, render headlines.
    store: dict[str, Any] = {"session_id": delta.session_id,
                             "turn_span": [delta.started_at, delta.ended_at],
                             "user_text": delta.user_text[:2000],
                             "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                             "items": {}}
    lines = [f"ANGLES · {delta.session_id[:8]} · turn @ {delta.started_at[11:16]}"
             f" · user: \"{_one_line(delta.user_text, 60)}\""]
    for key in ANGLE_SPECS:
        if key not in results:
            continue
        prefix = ANGLE_SPECS[key][0]
        items = results[key][:_CAPS[key]]
        if not items and key not in failures:
            continue
        label = f"{key}:".ljust(11)
        if key in failures:
            lines.append(f"{label}(unavailable — {_one_line(failures[key], 60)})")
            continue
        for i, item in enumerate(items, 1):
            iid = f"{prefix}{i}"
            store["items"][iid] = {"angle": key, **item}
            lines.append(f"{label}{iid} {item['headline']}")
            label = " " * 11
        dropped = len(results[key]) - len(items)
        if dropped > 0:
            lines.append(f"{' ' * 11}(+{dropped} more — see state file)")

    state = _state_dir()
    payload = json.dumps(store, ensure_ascii=False, indent=1)
    (state / "last.json").write_text(payload)
    (state / f"{delta.session_id}.json").write_text(payload)
    lines.append(f"(detail: csd angles show ID · {time.time() - t0:.1f}s)")
    return "\n".join(lines)


def show_item(item_id: str) -> str:
    last = _state_dir() / "last.json"
    if not last.exists():
        return "no angles pulled yet"
    store = json.loads(last.read_text())
    item = store.get("items", {}).get(item_id.upper())
    if not item:
        known = ", ".join(sorted(store.get("items", {})))
        return f"{item_id}: not found (have: {known})"
    return json.dumps(item, ensure_ascii=False, indent=2)
