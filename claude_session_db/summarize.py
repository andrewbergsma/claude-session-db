"""Phase-4 roll-up — automated off-session summarization of PENDING sessions.

Drains the `csd reconcile-summaries` PENDING queue through the canonical
digest path, fully unattended:

    v_unsummarized  →  session_digest.render(--full-inputs)  →  local Ollama
    (JSON mode)     →  kmcp `session` entry via knowledge-cli  →  read-back
    verify          →  mark_summarized watermark stamp

This is the pipeline stage that was proven manually on 2026-06-19 (first-ever
unattended off-session summary, session e87f66d8 — see
claudecode:overview/session-management "Off-Session Summarization — Proven
Unattended") and intentionally left unwired. Design decisions inherited from
that decision trail:

  * NEVER `claude --resume` — raw-transcript replay overflows context and a
    headless resume stalls on AskUserQuestion. The digest never replays and
    never resumes, so neither failure mode exists here.
  * Local Ollama is the default tier (free/private); model + endpoint are
    env/flag selectable (CSD_SUMMARIZE_MODEL / CSD_OLLAMA_URL).
  * Truth from the ledger: success is a VERIFIED kmcp row (read-back after
    create), and only then is the summary_state watermark stamped. A claimed
    write that cannot be read back counts as a failure.
  * The kmcp write goes through knowledge-cli (the sanctioned scripted-ops
    surface), never straight SQL into the knowledge tables (R2 invariant).

Auto-written entries carry the `auto-summary` tag and `actor` provenance in
the summary text is avoided — the entry looks like any slim session entry, so
the reconcile gate treats it identically to an in-session /session-summary.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import psycopg
from psycopg.rows import dict_row

from .reconcile import mark_summarized, resolve_kmcp_dsn
from .session_digest import render as render_digest

# --- Tunables (env-overridable; flags override env) --------------------------

DEFAULT_MODEL = os.environ.get("CSD_SUMMARIZE_MODEL", "gemma4:26b-mlx")
DEFAULT_OLLAMA_URL = os.environ.get("CSD_OLLAMA_URL", "http://localhost:11434")
# Sessions per run. Deliberately small: the 700-session backlog drains over
# days under launchd instead of flooding the corpus (and the GPU) in one tick;
# a manual `csd summarize -n 20` is the backfill lever.
DEFAULT_LIMIT = int(os.environ.get("CSD_SUMMARIZE_LIMIT", "2"))
# Quiesce gate: only summarize sessions idle at least this long, so a live
# session is never summarized mid-flight (its digest would silently omit the
# tail — see lesson off-session-digest-of-active-session-omits-tail).
DEFAULT_MIN_IDLE_S = int(os.environ.get("CSD_SUMMARIZE_MIN_IDLE_S", "900"))
# Model context window. The digest must fit: ~4 bytes/token means the char cap
# below stays comfortably inside it alongside the instruction prompt.
DEFAULT_NUM_CTX = int(os.environ.get("CSD_SUMMARIZE_NUM_CTX", "32768"))
DIGEST_MAX_CHARS = int(os.environ.get("CSD_SUMMARIZE_DIGEST_MAX_CHARS", "90000"))
LLM_TIMEOUT_S = int(os.environ.get("CSD_SUMMARIZE_LLM_TIMEOUT_S", "600"))
KMCP_TIMEOUT_S = int(os.environ.get("CSD_SUMMARIZE_KMCP_TIMEOUT_S", "120"))
# Failure backoff: a session that failed recently is not retried every tick,
# and after MAX_ATTEMPTS it leaves the automatic queue entirely (still visible
# in summarize_attempts for a manual look).
RETRY_BACKOFF_S = int(os.environ.get("CSD_SUMMARIZE_RETRY_BACKOFF_S", str(6 * 3600)))
MAX_ATTEMPTS = int(os.environ.get("CSD_SUMMARIZE_MAX_ATTEMPTS", "3"))
# Fallback kmcp application when the session cwd maps to nothing.
DEFAULT_APP = os.environ.get("CSD_SUMMARIZE_DEFAULT_APP", "claudecode")

AUTO_TAG = "auto-summary"

# cwd-basename → kmcp application, for names that don't match an app even
# after dash→underscore normalization. Deterministic on purpose: the model
# does not choose where an entry lands.
APP_ALIASES = {
    "claude-session-db": "claudecode",
    "harness": "orchestration",
    "whisper-diarize": "recordings",
}


# --- Attempt ledger (archive-side, sibling of summary_state) -----------------

_ATTEMPTS_DDL = """
CREATE TABLE IF NOT EXISTS summarize_attempts (
    session_id      TEXT PRIMARY KEY,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error      TEXT
)
"""


def ensure_attempts_table(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(_ATTEMPTS_DDL)
    conn.commit()


def _record_failure(conn: psycopg.Connection, session_id: str, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO summarize_attempts (session_id, attempts, last_attempt_at, last_error)
            VALUES (%s, 1, now(), %s)
            ON CONFLICT (session_id) DO UPDATE SET
                attempts = summarize_attempts.attempts + 1,
                last_attempt_at = now(),
                last_error = EXCLUDED.last_error
            """,
            (session_id, error[:2000]),
        )
    conn.commit()


def _clear_attempts(conn: psycopg.Connection, session_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM summarize_attempts WHERE session_id = %s", (session_id,))
    conn.commit()


# --- Work queue ---------------------------------------------------------------

_PICK_SQL = """
    SELECT u.session_id, u.project_name, u.project_path, u.title, u.first_prompt,
           u.created_at, u.modified_at, u.message_count, u.tool_use_count,
           u.user_prompt_count, u.error_count, u.total_output_tokens, u.reason,
           s.file_path, s.cwd, s.git_branch, s.duration_seconds, s.total_input_tokens
    FROM v_unsummarized u
    JOIN sessions s USING (session_id)
    LEFT JOIN summarize_attempts a USING (session_id)
    WHERE u.modified_at < now() - make_interval(secs => %(min_idle)s)
      AND (a.session_id IS NULL
           OR (a.attempts < %(max_attempts)s
               AND a.last_attempt_at < now() - make_interval(secs => %(backoff)s)))
      AND (%(only_session)s::text IS NULL OR u.session_id = %(only_session)s)
    ORDER BY u.modified_at DESC
    LIMIT %(limit)s
"""


def pick_pending(conn: psycopg.Connection, limit: int, min_idle_s: int,
                 only_session: Optional[str] = None) -> list[dict[str, Any]]:
    """Newest-first PENDING sessions that are quiesced and not in failure backoff."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_PICK_SQL, {
            "min_idle": min_idle_s,
            "max_attempts": MAX_ATTEMPTS,
            "backoff": RETRY_BACKOFF_S,
            "only_session": only_session,
            "limit": limit,
        })
        rows = cur.fetchall()
    conn.commit()  # release the read txn — never idle-in-transaction
    return rows


# --- LLM roll-up ---------------------------------------------------------------

_PROMPT = """You are summarizing one Claude Code (AI coding agent) session for a \
knowledge base. Below is a compact digest of the session transcript: [USER] and \
[ASSISTANT] turns, tool calls (→ name(args)) and truncated tool results (⮑).

Return ONLY a single JSON object with exactly these keys:
- "title": short descriptive title of the session, max 70 chars, no "Session:" prefix
- "topic_slug": 3-6 word kebab-case slug of the main theme (e.g. "prometheus-retention-bump")
- "description": one sentence describing what the session did
- "summary": 2-4 sentences — what was attempted, what was accomplished, and any key decisions with their why
- "tools_used": array of up to 8 notable tools/commands used (strings)
- "errors_encountered": array of {"error": "...", "resolution": "..."} objects; [] if none
- "follow_up": array of strings — unfinished work or explicit next steps; [] if none

Rules: state only what the digest supports — never invent facts, paths, or outcomes. \
Be specific ("bumped Prometheus retention 15d->1y in prometheus.yml", not "changed a config"). \
Empty arrays are fine. If the session was cut off, say so in follow_up.

DIGEST:
{digest}
"""


def _elide_middle(text: str, max_chars: int) -> str:
    """Keep the head and tail of an over-long digest; the middle is the
    droppable part (the opening frames the task, the tail carries the
    conclusion)."""
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.6)
    tail = max_chars - head
    dropped = len(text) - max_chars
    return (text[:head] + f"\n\n[... digest elided: {dropped} chars omitted ...]\n\n"
            + text[-tail:])


def call_ollama(prompt: str, model: str, base_url: str,
                num_ctx: int = DEFAULT_NUM_CTX,
                timeout_s: int = LLM_TIMEOUT_S) -> dict[str, Any]:
    """POST /api/generate in JSON mode; returns the parsed JSON object plus
    token counts under the reserved key ``_usage``."""
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        # No `format: "json"` — the MLX runner ignores the grammar constraint
        # and (with thinking models) can return an EMPTY response under it;
        # _extract_json_object handles the fenced output instead. Thinking is
        # disabled: the roll-up needs extraction, not reasoning, and thinking
        # tokens otherwise swallow the whole budget on long digests.
        "think": False,
        "options": {"temperature": 0.2, "num_ctx": num_ctx},
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        body = json.loads(resp.read().decode())
    parsed = _extract_json_object(body.get("response", ""))
    parsed["_usage"] = {
        "prompt_tokens": body.get("prompt_eval_count"),
        "output_tokens": body.get("eval_count"),
        "duration_s": round((body.get("total_duration") or 0) / 1e9, 1),
    }
    return parsed


def _extract_json_object(text: str) -> dict[str, Any]:
    """Parse the model's response into a dict, tolerating markdown fences.

    Ollama's `format: "json"` grammar constraint is not honored by every
    backend — the MLX runner returns ```json fenced``` prose (observed with
    gemma4:26b-mlx). Take the outermost {...} span rather than trusting the
    raw string."""
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"no JSON object in model response: {text[:200]!r}")
    parsed = json.loads(text[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError(f"model returned non-object JSON: {type(parsed).__name__}")
    return parsed


def _clean_llm_output(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate + coerce the model's JSON into the shape the entry needs.
    Raises ValueError when the required narrative fields are unusable."""
    title = str(raw.get("title") or "").strip()
    summary = str(raw.get("summary") or "").strip()
    if not title or not summary:
        raise ValueError("model output missing title/summary")

    def _strs(key: str, cap: int) -> list[str]:
        val = raw.get(key) or []
        if isinstance(val, str):
            val = [val]
        return [str(v).strip() for v in val if str(v).strip()][:cap]

    errors: list[dict[str, str]] = []
    for e in (raw.get("errors_encountered") or [])[:10]:
        if isinstance(e, dict) and e.get("error"):
            errors.append({"error": str(e["error"]).strip(),
                           "resolution": str(e.get("resolution") or "").strip()})
        elif isinstance(e, str) and e.strip():
            errors.append({"error": e.strip(), "resolution": ""})
    return {
        "title": title[:120],
        "topic_slug": str(raw.get("topic_slug") or "").strip(),
        "description": str(raw.get("description") or "").strip() or summary.split(". ")[0],
        "summary": summary,
        "tools_used": _strs("tools_used", 8),
        "follow_up": _strs("follow_up", 10),
        "errors_encountered": errors,
        "_usage": raw.get("_usage") or {},
    }


# --- kmcp surface (knowledge-cli subprocess) -----------------------------------

class KmcpError(RuntimeError):
    pass


def _find_knowledge_cli() -> str:
    explicit = os.environ.get("CSD_KNOWLEDGE_CLI")
    if explicit:
        return explicit
    found = shutil.which("knowledge-cli")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "knowledge-cli"
    if fallback.exists():
        return str(fallback)
    raise KmcpError("knowledge-cli not found (set CSD_KNOWLEDGE_CLI)")


def kmcp_call(tool: str, args: dict[str, Any], kmcp_dsn: str,
              timeout_s: int = KMCP_TIMEOUT_S) -> dict[str, Any]:
    """Invoke a knowledge-mcp tool via knowledge-cli in local-trusted mode.

    The CLI runs the kmcp server in-process against DATABASE_URL; since the
    2026-07 multi-user auth change an unauthenticated caller fails EMPTY unless
    KNOWLEDGE_ALLOW_UNAUTH_LOCAL=1 (single-user trusted host — this Studio).
    """
    cli = _find_knowledge_cli()
    env = dict(os.environ)
    env["DATABASE_URL"] = kmcp_dsn
    env["KNOWLEDGE_ALLOW_UNAUTH_LOCAL"] = "1"
    # Keep the CLI's ./data scratch out of arbitrary cwds.
    state_dir = Path(os.environ.get("CSD_STATE_DIR",
                     Path.home() / ".local" / "state" / "claude-session-db"))
    (state_dir / "kmcp-data").mkdir(parents=True, exist_ok=True)
    env.setdefault("KNOWLEDGE_DATA_DIR", str(state_dir / "kmcp-data"))
    try:
        proc = subprocess.run(
            [cli, "call", tool, "-"],
            input=json.dumps(args), capture_output=True, text=True,
            timeout=timeout_s, env=env, cwd=str(state_dir),
        )
    except subprocess.TimeoutExpired as exc:
        raise KmcpError(f"{tool}: knowledge-cli timed out after {timeout_s}s") from exc
    out = proc.stdout.strip()
    brace = out.find("{")
    if brace >= 0:
        try:
            # Tool-level errors ({"error": ...}, nonzero rc) are STILL returned —
            # a "Not found" get_entry is signal, not failure; call sites decide.
            return json.loads(out[brace:])
        except json.JSONDecodeError:
            pass
    raise KmcpError(f"{tool}: rc={proc.returncode} "
                    f"stdout={out[:300]!r} stderr={proc.stderr.strip()[:300]!r}")


def infer_application(cwd: Optional[str], kmcp_dsn: str,
                      app_cache: dict[str, bool]) -> str:
    """Map the session cwd to a kmcp application, deterministically.

    basename → alias table → dash/underscore normalization, validated against
    live applications; DEFAULT_APP when nothing matches. The model never picks.
    """
    if not cwd:
        return DEFAULT_APP
    base = Path(cwd).name.lower()
    for cand in (APP_ALIASES.get(base), base, base.replace("-", "_")):
        if not cand:
            continue
        if cand not in app_cache:
            res = kmcp_call("get_application", {"name": cand}, kmcp_dsn)
            app_cache[cand] = "error" not in res
        if app_cache[cand]:
            return cand
    return DEFAULT_APP


def _slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return re.sub(r"-{2,}", "-", text)[:60] or "session"


def _entry_exists(application: str, path: str, kmcp_dsn: str) -> bool:
    res = kmcp_call("get_entry", {"application": application, "path": path,
                                  "summary": True}, kmcp_dsn)
    return "error" not in res


# --- Per-session pipeline --------------------------------------------------------


@dataclass
class SummarizeStats:
    picked: int = 0
    written: list[str] = field(default_factory=list)   # "app:path"
    failed: list[str] = field(default_factory=list)    # "sid: error"
    dry_run: bool = False

    def summary(self) -> str:
        lines = [f"Phase-4 roll-up: {self.picked} picked, "
                 f"{len(self.written)} written, {len(self.failed)} failed"
                 + (" [dry-run]" if self.dry_run else "")]
        lines += [f"  ✓ {w}" for w in self.written]
        lines += [f"  ✗ {f}" for f in self.failed]
        return "\n".join(lines)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def summarize_one(row: dict[str, Any], kmcp_dsn: str, model: str, ollama_url: str,
                  num_ctx: int, app_cache: dict[str, bool],
                  log: Callable[[str], None]) -> tuple[str, str]:
    """Digest → LLM → verified kmcp write for one session. Returns (app, path).
    Raises on any failure; the caller records the attempt."""
    sid = row["session_id"]
    jsonl = Path(row["file_path"] or "")
    if not jsonl.is_file():
        raise FileNotFoundError(f"transcript missing: {jsonl}")

    digest = render_digest(jsonl, result_head=250, full_inputs=True)
    digest = _elide_middle(digest, DIGEST_MAX_CHARS)
    log(f"  digest: {jsonl.stat().st_size // 1024}KB jsonl -> {len(digest) // 1024}KB")

    raw = call_ollama(_PROMPT.replace("{digest}", digest), model, ollama_url, num_ctx)
    out = _clean_llm_output(raw)
    usage = out.pop("_usage", {})
    log(f"  llm: {model} {usage.get('duration_s', '?')}s "
        f"({usage.get('prompt_tokens', '?')} in / {usage.get('output_tokens', '?')} out)")

    application = infer_application(row.get("cwd") or row.get("project_path"),
                                    kmcp_dsn, app_cache)
    date = (_iso(row["created_at"]) or "")[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    base_slug = _slugify(out["topic_slug"] or out["title"])
    path = f"session/{date}/{base_slug}"
    for n in range(2, 10):
        if not _entry_exists(application, path, kmcp_dsn):
            break
        path = f"session/{date}/{base_slug}-{n}"

    metrics: dict[str, Any] = {}
    if row.get("duration_seconds"):
        metrics["duration_minutes"] = round(row["duration_seconds"] / 60, 1)
    if row.get("tool_use_count") is not None:
        metrics["tool_calls"] = row["tool_use_count"]
    if row.get("total_input_tokens"):
        metrics["input_tokens"] = row["total_input_tokens"]
    if row.get("total_output_tokens"):
        metrics["output_tokens"] = row["total_output_tokens"]

    content: dict[str, Any] = {
        "summary": out["summary"],
        # Invariant: verbatim ARCHIVE session id — never the summarizer's own.
        "session_id": sid,
        "project_path": row.get("cwd") or row.get("project_path"),
        "started_at": _iso(row["created_at"]),
        "ended_at": _iso(row["modified_at"]),
        "tools_used": out["tools_used"],
        "errors_encountered": out["errors_encountered"],
        "follow_up": out["follow_up"],
    }
    if metrics:
        content["metrics"] = metrics

    created = kmcp_call("create_entry", {
        "application": application,
        "path": path,
        "entity_type": "session",
        "title": f"Session: {out['title']}",
        "description": out["description"][:300],
        "content": content,
        "tags": [AUTO_TAG],
    }, kmcp_dsn)
    if "error" in created:
        raise KmcpError(f"create_entry failed: {json.dumps(created)[:300]}")

    # Truth from the ledger: verify the row actually exists and carries OUR id.
    back = kmcp_call("get_entry", {"application": application, "path": path,
                                   "sections": ["session_id"]}, kmcp_dsn)
    got_sid = (back.get("content") or {}).get("session_id")
    if got_sid != sid:
        raise KmcpError(f"read-back verify failed: session_id={got_sid!r} != {sid!r}")

    return application, path


def run_summarize(archive_conn: psycopg.Connection, csd_dsn: str,
                  limit: int = DEFAULT_LIMIT, min_idle_s: int = DEFAULT_MIN_IDLE_S,
                  model: str = DEFAULT_MODEL, ollama_url: str = DEFAULT_OLLAMA_URL,
                  num_ctx: int = DEFAULT_NUM_CTX, only_session: Optional[str] = None,
                  dry_run: bool = False, kmcp_dsn: Optional[str] = None,
                  log: Optional[Callable[[str], None]] = None) -> SummarizeStats:
    emit = log if callable(log) else (lambda _m: None)
    kmcp_dsn = resolve_kmcp_dsn(csd_dsn, kmcp_dsn)
    ensure_attempts_table(archive_conn)

    rows = pick_pending(archive_conn, limit, min_idle_s, only_session)
    stats = SummarizeStats(picked=len(rows), dry_run=dry_run)
    if not rows:
        emit("queue empty — nothing pending, quiesced, and retry-eligible")
        return stats

    app_cache: dict[str, bool] = {}
    for row in rows:
        sid = row["session_id"]
        emit(f"{sid}  {row.get('project_name') or ''}  "
             f"{row.get('message_count')}msg  [{row.get('reason') or 'pending'}]")
        if dry_run:
            continue
        started = time.monotonic()
        try:
            app, path = summarize_one(row, kmcp_dsn, model, ollama_url,
                                      num_ctx, app_cache, emit)
            mark_summarized(archive_conn, sid, app, path)
            _clear_attempts(archive_conn, sid)
            stats.written.append(f"{app}:{path}")
            emit(f"  ✓ {app}:{path}  ({time.monotonic() - started:.0f}s)")
        except Exception as exc:  # noqa: BLE001 — per-session isolation
            archive_conn.rollback()
            err = f"{type(exc).__name__}: {exc}"
            _record_failure(archive_conn, sid, err)
            stats.failed.append(f"{sid}: {err}")
            emit(f"  ✗ {err}")
    return stats
