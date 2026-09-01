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

import hashlib
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import psycopg
from psycopg.rows import dict_row

from .postgres import SUMMARY_PASSES_DDL
from .reconcile import mark_summarized, resolve_kmcp_dsn
from .session_digest import load as load_jsonl
from .session_digest import render as render_digest
from .session_mgmt import (WATERMARK_SLACK_S, DeltaReport, _parse_ts,
                           _watermark_for, classify_delta, resolve_session_ref,
                           resolve_transcript)

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

# --- Repeatable (delta) capture ------------------------------------------------
# A session that grows past its watermark flips back to pending/grown. The second
# pass summarizes ONLY the tail — never the whole transcript again. Two bounds:
#   MIN_DELTA_RECORDS — a floor on the tail's size, counted from the TRANSCRIPT
#     (classify_delta), NEVER from sessions.message_count: on a main session that
#     column is a ROLL-UP that includes subagent children, so a busy sidechain
#     alone would clear any count-based floor with zero new main-chain work.
#   MAX_PASSES — a session cannot spawn unbounded entries; past this it stays
#     pending and visible rather than fanning out further.
MIN_DELTA_RECORDS = int(os.environ.get("CSD_SUMMARIZE_MIN_DELTA_RECORDS", "20"))
MAX_PASSES = int(os.environ.get("CSD_SUMMARIZE_MAX_PASSES", "6"))

AUTO_TAG = "auto-summary"
DELTA_TAG = "delta-capture"

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


# --- Pass ledger + single-dispatch claim ---------------------------------------

def ensure_passes_table(conn: psycopg.Connection) -> None:
    """Self-heal summary_passes without paying initialize()'s full-schema DDL
    (same reasoning as ensure_attempts_table / ensure_gate_objects)."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.summary_passes')")
        if cur.fetchone()[0] is None:
            cur.execute(SUMMARY_PASSES_DDL)
    conn.commit()


def _lock_key(session_id: str) -> int:
    """Stable 63-bit advisory-lock key for a session id (hashed, not sliced —
    two sessions sharing a short-id prefix must not share a lock)."""
    digest = hashlib.blake2b(session_id.encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") >> 1


def claim_session(conn: psycopg.Connection, session_id: str) -> bool:
    """Session-scoped advisory lock: True iff THIS connection may summarize it.

    The console can dispatch a summary for the same session the launchd timer is
    mid-way through; both would digest the same tail and write two entries. The
    lock is held for the whole pass (not per-transaction — the pipeline commits
    between phases) and released in run_summarize's finally.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (_lock_key(session_id),))
        got = bool(cur.fetchone()[0])
    conn.commit()
    return got


def release_session(conn: psycopg.Connection, session_id: str) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_lock_key(session_id),))
        conn.commit()
    except psycopg.Error:
        pass  # the lock dies with the connection anyway


def open_pass(conn: psycopg.Connection, session_id: str, pass_no: int) -> None:
    """Mark a pass in flight BEFORE the LLM runs (visible, restartable)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO summary_passes (session_id, pass, status)
            VALUES (%s, %s, 'in_flight')
            ON CONFLICT (session_id, pass) DO UPDATE SET
                status = 'in_flight', updated_at = now()
            """,
            (session_id, pass_no),
        )
    conn.commit()


def record_pass(conn: psycopg.Connection, session_id: str, pass_no: int,
                application: Optional[str] = None, path: Optional[str] = None,
                message_count_at_summary: Optional[int] = None,
                leaf_uuid_at_summary: Optional[str] = None,
                status: str = "written", detail: Optional[str] = None) -> None:
    """Settle a pass in the ledger (idempotent by (session_id, pass))."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO summary_passes
                (session_id, pass, application, path, message_count_at_summary,
                 leaf_uuid_at_summary, status, detail, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (session_id, pass) DO UPDATE SET
                application = EXCLUDED.application,
                path = EXCLUDED.path,
                message_count_at_summary = EXCLUDED.message_count_at_summary,
                leaf_uuid_at_summary = EXCLUDED.leaf_uuid_at_summary,
                status = EXCLUDED.status,
                detail = EXCLUDED.detail,
                updated_at = now()
            """,
            (session_id, pass_no, application, path, message_count_at_summary,
             leaf_uuid_at_summary, status, (detail or "")[:2000] or None),
        )
    conn.commit()


# --- Work queue ---------------------------------------------------------------

# The prior-capture columns are joined HERE rather than widened into
# v_unsummarized: the view is the queue's public contract (console + `csd
# unsummarized` read it), and a pending session's watermark is a detail of how
# THIS pipeline renders its digest, not of what is pending.
_PICK_SQL = """
    SELECT u.session_id, u.project_name, u.project_path, u.title, u.first_prompt,
           u.created_at, u.modified_at, u.message_count, u.tool_use_count,
           u.user_prompt_count, u.error_count, u.total_output_tokens, u.reason,
           s.file_path, s.cwd, s.git_branch, s.duration_seconds, s.total_input_tokens,
           -- prior capture (carried by reconcile even when the kmcp entry no
           -- longer resolves): where the last pass stopped, and where it landed.
           ss.message_count_at_summary AS prev_wm,
           ss.leaf_uuid_at_summary     AS prev_leaf,
           ss.kmcp_application         AS prev_app,
           ss.kmcp_path                AS prev_path,
           coalesce(sp.max_pass, 0)    AS prev_pass
    FROM v_unsummarized u
    JOIN sessions s USING (session_id)
    LEFT JOIN summary_state ss USING (session_id)
    LEFT JOIN summarize_attempts a USING (session_id)
    LEFT JOIN LATERAL (
        SELECT max(pass) AS max_pass FROM summary_passes sp2
        WHERE sp2.session_id = u.session_id AND sp2.status = 'written'
    ) sp ON true
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
    """Newest-first PENDING sessions that are quiesced and not in failure backoff.

    Each row carries its PRIOR capture (prev_wm / prev_leaf / prev_app /
    prev_path / prev_pass) so the delta gate can decide window vs full scope
    without a second round-trip.
    """
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


# --- Delta gate ------------------------------------------------------------------


@dataclass
class DeltaGate:
    """Verdict on HOW (and whether) to summarize one pending session."""
    mode: str = "full"                       # full | delta
    pass_no: int = 1
    watermark: Optional[datetime] = None
    source: str = "none"                     # leaf | count | kmcp | none
    report: Optional[DeltaReport] = None
    end_ts: Optional[datetime] = None        # last record IN the delta window
    prev_ref: Optional[str] = None           # "app:path" of the prior pass
    skip: Optional[str] = None               # set => do not summarize this run

    @property
    def is_delta(self) -> bool:
        return self.mode == "delta" and self.watermark is not None


def _last_ts_after(recs: list[dict], watermark: datetime) -> Optional[datetime]:
    """Timestamp of the last main-chain record inside the delta window."""
    cutoff = watermark + timedelta(seconds=WATERMARK_SLACK_S)
    out: Optional[datetime] = None
    for rec in recs:
        if rec.get("isSidechain"):
            continue
        ts = _parse_ts(rec.get("timestamp"))
        if ts is not None and ts > cutoff:
            out = ts
    return out


def _delta_gate(row: dict[str, Any], dsn: str, kmcp_dsn: Optional[str],
                min_delta_records: int = MIN_DELTA_RECORDS,
                enabled: bool = True) -> DeltaGate:
    """Decide the scope of this session's next pass.

    NEVER claims delta without a resolvable watermark: with no watermark the
    only honest scope is the FULL transcript, written as a standalone summary
    rather than a continuation. That is the failure mode this gate exists to
    prevent — rendering everything while telling the model (and the entry) it
    is reading only new work.

    For rows the reconcile gate flipped back as `grown`, delta is additionally
    gated on the tail being substantive: classify_delta must call it `real`, and
    it must carry at least `min_delta_records` main-chain records. Both come
    from the TRANSCRIPT, never from sessions.message_count (a subagent roll-up).
    """
    prev_pass = int(row.get("prev_pass") or 0)
    prev_app, prev_path = row.get("prev_app"), row.get("prev_path")
    prev_ref = f"{prev_app}:{prev_path}" if prev_app and prev_path else None
    captured_before = bool(prev_pass or prev_ref or row.get("prev_wm") is not None
                           or row.get("reason") == "grown")

    if not enabled or not captured_before:
        return DeltaGate(mode="full", pass_no=prev_pass + 1, prev_ref=prev_ref)

    if prev_pass >= MAX_PASSES:
        return DeltaGate(mode="delta", pass_no=prev_pass + 1, prev_ref=prev_ref,
                         skip=f"pass ceiling reached ({prev_pass}/{MAX_PASSES})")

    watermark, source = (None, "none")
    try:
        watermark, source = _watermark_for(row["session_id"], dsn, kmcp_dsn)
    except Exception:  # noqa: BLE001 — degrade to full scope, never crash the run
        watermark, source = None, "none"
    if watermark is None:
        # No watermark => no honest window. Full transcript, written as a
        # STANDALONE summary (no cont. title, no delta tag, no back-link) —
        # never full scope under a continuation label. The ledger pass number
        # still advances so a re-capture cannot overwrite an earlier pass row.
        return DeltaGate(mode="full", pass_no=prev_pass + 1, prev_ref=prev_ref,
                         source="none")

    path = resolve_transcript(row["session_id"], row.get("file_path"))
    if path is None:
        return DeltaGate(mode="delta", pass_no=prev_pass + 1, watermark=watermark,
                         source=source, prev_ref=prev_ref,
                         skip="transcript not found on disk")
    recs = load_jsonl(path)
    report = classify_delta(recs, watermark)
    report.watermark_source = source
    gate = DeltaGate(mode="delta", pass_no=prev_pass + 1, watermark=watermark,
                     source=source, report=report, prev_ref=prev_ref,
                     end_ts=_last_ts_after(recs, watermark))

    if row.get("reason") == "grown":
        if report.klass != "real":
            gate.skip = (f"delta not substantive (class={report.klass}, "
                         f"{report.records} records)")
        elif report.records < min_delta_records:
            gate.skip = (f"delta below floor ({report.records} < "
                         f"{min_delta_records} records)")
    elif report.records == 0:
        gate.skip = "nothing after the watermark"
    return gate


# --- Next-pass scope (the ONE grader: console button, CLI, skill) --------------
#
# `resolve_summary_scope` used to live in console/server.py. It is the same
# question the /session-summary skill asks before it writes ("has this session
# been captured, and what would a NEXT pass cover?"), so it lives beside the
# gate it wraps and BOTH surfaces call it — the console keeps a thin wrapper
# that supplies its module-level DSNs, `csd summary-scope` calls it directly.
#
# DOCTRINE, unchanged in the move: it can never block a caller. No DSN, an
# unreachable archive, a missing summary_passes table, a raising gate — every
# failure degrades to FULL pass-1 scope with the reason surfaced instead of
# swallowed. Nothing here raises.

PRIOR_SQL = """
    SELECT s.file_path, ss.state, ss.reason,
           ss.message_count_at_summary AS prev_wm,
           ss.leaf_uuid_at_summary     AS prev_leaf,
           ss.kmcp_application         AS prev_app,
           ss.kmcp_path                AS prev_path
    FROM sessions s
    LEFT JOIN summary_state ss USING (session_id)
    WHERE s.session_id = %s
"""

# Human labels for DeltaGate.source (leaf | count | kmcp | none) — the raw
# value stays in the JSON, the label is what a report prints.
WATERMARK_SOURCES = {"leaf": "leaf_uuid", "count": "message_count",
                     "kmcp": "kmcp_entry", "none": "none"}


def prior_capture(sid: str, dsn: Optional[str]) -> dict[str, Any]:
    """The row `_delta_gate` grades, for ONE session. RAISES (callers degrade)."""
    if not dsn:
        raise RuntimeError("no archive DSN configured")
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as conn:
        conn.read_only = True
        row = dict(conn.execute(PRIOR_SQL, (sid,)).fetchone() or {})
        ledger = 0
        if conn.execute("SELECT to_regclass('public.summary_passes') AS t"
                        ).fetchone()["t"]:
            hit = conn.execute("SELECT max(pass) AS p FROM summary_passes "
                               "WHERE session_id = %s AND status = 'written'",
                               (sid,)).fetchone()
            ledger = int((hit or {}).get("p") or 0)
    row["session_id"] = sid
    # A session captured BEFORE the ledger existed has no row for its pass 1 —
    # only a watermark. Count that capture, so its continuation is numbered
    # (and titled) pass 2 instead of re-using pass 1's number.
    captured = bool(row.get("prev_wm") or row.get("prev_leaf")
                    or row.get("prev_app") or row.get("state") == "summarized")
    row["prev_pass"] = max(ledger, 1 if captured else 0)
    return row


def _grade_scope(sid: str, dsn: Optional[str], kmcp_dsn: Optional[str],
                 mode: str = "auto", prior_capture_fn=None):
    """(scope dict, gate|None, row|None). The body of resolve_summary_scope;
    split out so a reporter can also read the gate's own detail (the watermark
    behind a `none` verdict) without widening the scope dict."""
    out = {"delta": False, "pass": 1, "since": None, "source": "none",
           "prior": None, "mode": mode, "records": None,
           "note": None, "warning": None}
    capture = prior_capture_fn or (lambda s: prior_capture(s, dsn))
    try:
        row = capture(sid)
        gate = _delta_gate(row, dsn, kmcp_dsn, enabled=(mode != "off"))
    except Exception as exc:  # noqa: BLE001 — degrade to full, never block
        out["note"] = (f"prior capture unresolved ({type(exc).__name__}: "
                       f"{exc}) — full scope")
        return out, None, None

    out["pass"] = gate.pass_no
    out["prior"] = gate.prev_ref
    out["source"] = gate.source
    out["records"] = gate.report.records if gate.report else None

    if mode == "off":
        out["note"] = "delta disabled for this dispatch — full scope"
        return out, gate, row
    if gate.watermark is None:
        # No window => no honest continuation. Full transcript, standalone —
        # never full scope wearing a continuation label (_delta_gate's
        # invariant, and the reason this branch does not "force" anything).
        out["note"] = ("no summary watermark resolvable — full scope"
                       if row.get("prev_pass") else None)
        return out, gate, row

    skip = gate.skip or ""
    ceiling = skip.startswith("pass ceiling")
    if not skip or ceiling or mode == "force":
        out["delta"] = True
        out["since"] = _iso(gate.watermark)
        if skip:
            out["warning"] = f"{skip} — windowed and dispatched anyway"
    else:
        # Deliberate: the gate found nothing substantive since the last pass, so
        # a delta entry would say nothing. Falling back to full scope RESTATES
        # pass 1, which is the surprising half — so it is surfaced, not hidden.
        out["warning"] = (f"{skip} — summarizing the FULL session again "
                          "(delta:\"force\" windows it to the tail regardless)")
    return out, gate, row


def resolve_summary_scope(sid: str, dsn: Optional[str],
                          kmcp_dsn: Optional[str] = None, mode: str = "auto",
                          prior_capture_fn=None) -> dict:
    """How the next pass should be scoped. NEVER raises.

    auto  — delta when a watermark resolves AND the gate calls the tail real.
    force — delta from the watermark whatever the gate thinks of the tail.
    off   — full scope, the historic behaviour.
    """
    return _grade_scope(sid, dsn, kmcp_dsn, mode, prior_capture_fn)[0]


def summary_scope_report(ref: str, dsn: Optional[str] = None,
                         kmcp_dsn: Optional[str] = None,
                         mode: str = "auto") -> dict:
    """`csd summary-scope`: does a kmcp summary already exist for this session,
    and what would the NEXT pass cover? Never raises.

    scope is one of:
      full  — no prior capture (pass 1), or a prior capture with no resolvable
              watermark: the honest scope is the whole transcript.
      delta — a window opens at `since`; digest it with the printed command.
      none  — captured already, and the tail since is not substantive (below
              CSD_SUMMARIZE_MIN_DELTA_RECORDS, or not classified `real`).
              Nothing new to write; `--mode force` windows it anyway.
    """
    sid, reason = ref, None
    try:
        sid, _fp = resolve_session_ref(ref, dsn)
    except Exception as exc:  # noqa: BLE001 — an unresolved ref is not fatal
        reason = f"session ref not resolved ({exc}) — using it verbatim"

    scope, gate, _row = _grade_scope(sid, dsn, kmcp_dsn, mode)
    watermark = _iso(getattr(gate, "watermark", None)) if gate else None
    if scope["delta"]:
        kind = "delta"
    elif scope["warning"]:
        kind = "none"           # captured, tail not substantive
    else:
        kind = "full"

    prior_exists = bool(scope["prior"] or scope["pass"] > 1)
    why = scope["warning"] or scope["note"]
    if kind == "full" and not prior_exists and not why:
        why = "no prior summary found"
    rep = {
        "session": sid, "ref": ref, "mode": mode,
        "scope": kind, "pass": scope["pass"],
        "summarized": prior_exists,
        "since": scope["since"] or (watermark if kind == "none" else None),
        "source": scope["source"],
        "source_label": WATERMARK_SOURCES.get(scope["source"], scope["source"]),
        "prior": scope["prior"],
        "records": scope["records"],
        "reason": " · ".join(x for x in (reason, why) if x) or None,
    }
    rep["digest"] = (f"csd digest {sid} --since {rep['since']}"
                     if kind == "delta" and rep["since"]
                     else f"csd digest {sid}")
    return rep


def format_scope_report(rep: dict) -> str:
    """One human block — the shape the /session-summary skill reads."""
    head = (f"session: {rep['session']}   pass: {rep['pass']}   "
            f"scope: {rep['scope']}")
    if rep["scope"] != "delta" and rep["reason"]:
        head += f"   ({rep['reason']})" if rep["scope"] == "full" \
            else f" — {rep['reason']}"
    lines = [head]
    if rep["since"]:
        lines.append(f"since:   {rep['since']}   "
                     f"(watermark source: {rep['source_label']})")
    if rep["prior"]:
        lines.append(f"prior:   {rep['prior']}")
    if rep["records"] is not None:
        lines.append(f"records: {rep['records']} after the watermark")
    if rep["scope"] == "delta" and rep["reason"]:
        lines.append(f"note:    {rep['reason']}")
    lines.append(f"digest:  {rep['digest']}")
    if rep["scope"] == "none":
        lines.append("         (nothing new to capture; `--mode force` windows "
                     "the tail anyway)")
    return "\n".join(lines)


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

END OF DIGEST. You are NOT a participant in that conversation — do not answer its \
questions or continue its work. Return ONLY the JSON summary object described above, \
nothing else.
"""

# Prepended (before _PROMPT) for a CONTINUATION pass. The digest it frames is the
# post-watermark tail only, so the model must be told that explicitly — otherwise
# it writes a whole-session summary out of a partial transcript, and the entry
# reads as if the earlier work happened in this window.
_DELTA_HEADER = """CONTINUATION SUMMARY. This session was already summarized once. \
The digest below is ONLY the tail written AFTER that prior summary's watermark \
({watermark}) — everything before it is already captured in another entry.

Summarize ONLY the new work in this tail:
- Do NOT restate, re-describe, or re-title the earlier part of the session.
- If the tail continues earlier work, say what CHANGED or LANDED in it, not what \
the work is.
- A line like "⋯ conversation compacted here ⋯" is auto-compaction bookkeeping, \
not new work — ignore it.
- If the tail is thin, say so plainly; a short honest summary beats an invented one.

"""

# Appended on retry after a non-JSON response. Dominant observed failure: a
# digest that ENDS with an open question lures the model into answering the
# conversation in prose instead of summarizing it (2/27 in the first batch).
_RETRY_SUFFIX = ("\nYour previous reply was prose, not JSON. Respond with ONLY the "
                 "JSON object — start your reply with the character { .\n")


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
    skipped: list[str] = field(default_factory=list)   # "sid: why" (gate/claim)
    deltas: int = 0                                    # written passes >= 2
    dry_run: bool = False

    def summary(self) -> str:
        lines = [f"Phase-4 roll-up: {self.picked} picked, "
                 f"{len(self.written)} written"
                 + (f" ({self.deltas} delta)" if self.deltas else "")
                 + f", {len(self.skipped)} skipped, {len(self.failed)} failed"
                 + (" [dry-run]" if self.dry_run else "")]
        lines += [f"  ✓ {w}" for w in self.written]
        lines += [f"  – {s}" for s in self.skipped]
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
                  log: Callable[[str], None],
                  gate: Optional[DeltaGate] = None) -> tuple[str, str]:
    """Digest → LLM → verified kmcp write for one session. Returns (app, path).
    Raises on any failure; the caller records the attempt.

    With a delta `gate` the digest is windowed to the post-watermark tail and
    the entry is written as a CONTINUATION: dated to the window's end, spanning
    the window (not the session), linked back to the pass it continues, and
    tagged `delta-capture`. Without one — or with a gate that could not resolve
    a watermark — the scope is the full transcript and the entry is a plain
    standalone summary.
    """
    sid = row["session_id"]
    jsonl = Path(row["file_path"] or "")
    if not jsonl.is_file():
        raise FileNotFoundError(f"transcript missing: {jsonl}")

    delta = bool(gate and gate.is_delta)
    if delta:
        note = (f"DELTA after summary watermark {_iso(gate.watermark)} "
                f"(source: {gate.source}, pass {gate.pass_no})")
        digest = render_digest(jsonl, result_head=250, full_inputs=True,
                               since=gate.watermark, note=note)
    else:
        digest = render_digest(jsonl, result_head=250, full_inputs=True)
    digest = _elide_middle(digest, DIGEST_MAX_CHARS)
    log(f"  digest: {jsonl.stat().st_size // 1024}KB jsonl -> {len(digest) // 1024}KB"
        + (f"  [delta pass {gate.pass_no}, {gate.report.records if gate.report else '?'}"
           f" records after watermark]" if delta else ""))

    prompt = _PROMPT.replace("{digest}", digest)
    if delta:
        prompt = _DELTA_HEADER.format(watermark=_iso(gate.watermark)) + prompt
    try:
        raw = call_ollama(prompt, model, ollama_url, num_ctx)
    except ValueError:
        log("  llm returned prose — retrying once with corrective suffix")
        raw = call_ollama(prompt + _RETRY_SUFFIX, model, ollama_url, num_ctx)
    out = _clean_llm_output(raw)
    usage = out.pop("_usage", {})
    log(f"  llm: {model} {usage.get('duration_s', '?')}s "
        f"({usage.get('prompt_tokens', '?')} in / {usage.get('output_tokens', '?')} out)")

    application = infer_application(row.get("cwd") or row.get("project_path"),
                                    kmcp_dsn, app_cache)
    # A continuation is dated to the END of the window it covers, not to the
    # session's birth: a thread resumed weeks later would otherwise file its new
    # work under the old date, beside (and indistinguishable from) pass 1.
    anchor = ((gate.end_ts or row.get("modified_at")) if delta
              else row.get("created_at"))
    date = (_iso(anchor) or "")[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
        # The span is the SCOPE of this entry: for a continuation that is the
        # delta window (watermark -> last record), never the session's own span.
        "started_at": _iso(gate.watermark) if delta else _iso(row["created_at"]),
        "ended_at": _iso(gate.end_ts or row["modified_at"]) if delta
                    else _iso(row["modified_at"]),
        "tools_used": out["tools_used"],
        "errors_encountered": out["errors_encountered"],
        "follow_up": out["follow_up"],
    }
    if metrics:
        content["metrics"] = metrics
    tags = [AUTO_TAG]
    if delta:
        tags.append(DELTA_TAG)
        if gate.prev_ref:
            content["linked_entries"] = [gate.prev_ref]

    created = kmcp_call("create_entry", {
        "application": application,
        "path": path,
        "entity_type": "session",
        "title": (f"Session (cont. {gate.pass_no}): {out['title']}" if delta
                  else f"Session: {out['title']}"),
        "description": out["description"][:300],
        "content": content,
        "tags": tags,
    }, kmcp_dsn)
    if "error" in created:
        raise KmcpError(f"create_entry failed: {json.dumps(created)[:300]}")

    # Truth from the ledger: verify the row actually exists and carries OUR id.
    back = kmcp_call("get_entry", {"application": application, "path": path,
                                   "sections": ["session_id"]}, kmcp_dsn)
    got_sid = (back.get("content") or {}).get("session_id")
    if got_sid != sid:
        raise KmcpError(f"read-back verify failed: session_id={got_sid!r} != {sid!r}")

    # Graph link back to the pass this continues. Best-effort BY DESIGN: the
    # entry is the payload and it is already verified — a failed relationship
    # must not turn a written summary into a failed pass (it would be re-run and
    # write a duplicate). linked_entries above already carries the reference.
    if delta and gate.prev_ref:
        prev_app, _, prev_path = gate.prev_ref.partition(":")
        try:
            rel = kmcp_call("create_relationship", {
                "source_application": application, "source_path": path,
                "target_application": prev_app, "target_path": prev_path,
                "relationship_type": "see_also",
                "description": f"delta capture: pass {gate.pass_no} continues this summary",
            }, kmcp_dsn)
            if "error" in rel:
                log(f"  ! see_also link skipped: {json.dumps(rel)[:200]}")
        except KmcpError as exc:
            log(f"  ! see_also link skipped: {exc}")

    return application, path


def run_summarize(archive_conn: psycopg.Connection, csd_dsn: str,
                  limit: int = DEFAULT_LIMIT, min_idle_s: int = DEFAULT_MIN_IDLE_S,
                  model: str = DEFAULT_MODEL, ollama_url: str = DEFAULT_OLLAMA_URL,
                  num_ctx: int = DEFAULT_NUM_CTX, only_session: Optional[str] = None,
                  dry_run: bool = False, kmcp_dsn: Optional[str] = None,
                  delta: bool = True, min_delta_records: int = MIN_DELTA_RECORDS,
                  log: Optional[Callable[[str], None]] = None) -> SummarizeStats:
    emit = log if callable(log) else (lambda _m: None)
    kmcp_dsn = resolve_kmcp_dsn(csd_dsn, kmcp_dsn)
    ensure_attempts_table(archive_conn)
    ensure_passes_table(archive_conn)

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

        gate = _delta_gate(row, csd_dsn, kmcp_dsn, min_delta_records, enabled=delta)
        if gate.is_delta:
            emit(f"  scope: DELTA pass {gate.pass_no} after {_iso(gate.watermark)} "
                 f"(watermark: {gate.source}"
                 + (f", tail: {gate.report.klass}/{gate.report.records} records"
                    if gate.report else "") + ")")
        else:
            emit(f"  scope: FULL transcript (pass {gate.pass_no})")
        if gate.skip:
            stats.skipped.append(f"{sid}: {gate.skip}")
            emit(f"  – skipped: {gate.skip}")
            continue
        if dry_run:
            continue

        # One dispatcher per session: the console and the launchd timer both
        # reach this path, and a double dispatch writes two entries for one tail.
        if not claim_session(archive_conn, sid):
            stats.skipped.append(f"{sid}: already being summarized elsewhere")
            emit("  – skipped: another summarize holds this session")
            continue
        started = time.monotonic()
        try:
            open_pass(archive_conn, sid, gate.pass_no)
            app, path = summarize_one(row, kmcp_dsn, model, ollama_url,
                                      num_ctx, app_cache, emit, gate)
            stamped = mark_summarized(archive_conn, sid, app, path)
            record_pass(archive_conn, sid, gate.pass_no, app, path,
                        stamped.get("message_count_at_summary"),
                        stamped.get("leaf_uuid_at_summary"), status="written",
                        detail=(f"delta from {_iso(gate.watermark)} "
                                f"({gate.source})" if gate.is_delta else "full"))
            _clear_attempts(archive_conn, sid)
            stats.written.append(f"{app}:{path}"
                                 + (f"  (pass {gate.pass_no})" if gate.pass_no > 1 else ""))
            if gate.pass_no > 1:
                stats.deltas += 1
            emit(f"  ✓ {app}:{path}  ({time.monotonic() - started:.0f}s)")
        except Exception as exc:  # noqa: BLE001 — per-session isolation
            archive_conn.rollback()
            err = f"{type(exc).__name__}: {exc}"
            _record_failure(archive_conn, sid, err)
            try:
                record_pass(archive_conn, sid, gate.pass_no, status="failed",
                            detail=err)
            except psycopg.Error:
                archive_conn.rollback()
            stats.failed.append(f"{sid}: {err}")
            emit(f"  ✗ {err}")
        finally:
            release_session(archive_conn, sid)
    return stats
