"""Reconcile summary_state — the pre-LLM gate for phase-4 session roll-ups.

Classifies every archived top-level session as summarized / not_required /
pending so the expensive digest->summarizer step only ever spends tokens on the
genuinely-pending residue. Designed per
claudecode:task/claude-session-db/summary-state-and-reconcile-gate after the
2026-06-07 dry run burned ~716K subagent tokens on 8 already-summarized /
meta / empty sessions out of a batch of 10.

Three properties the gate guarantees:

1. TRUTH FROM THE LEDGER, NOT THE NARRATOR. "summarized" is derived only from
   rows that actually exist in the kmcp `entries` table — never from a
   summarizer agent's self-report. A claimed-but-unwritten summary stays
   pending and self-heals on the next reconcile (observed live 2026-06-09:
   5 of 14 fan-out agents reported success but wrote nothing).
2. SOURCE IS NEVER MUTATED. summary_state is a sibling table; archive rows
   (sessions/messages/tool_results) stay the lossless source of record.
3. IDEMPOTENT + RE-RUNNABLE. Heuristics are recomputed from live counts each
   run, so a session that grows out of "empty" flips to pending on its own;
   watermarks on summarized rows are stamped once and preserved.

Classification precedence (first match wins):

  summarized   — a kmcp session entry exists with content->>'session_id' == id.
                 Re-eval edge: if the archive message_count has since grown past
                 the stamped watermark (+ slack), flip back to pending/grown.
  meta_run     — the session's own first_prompt is a /session-summary (or
                 session-summary skill) invocation carrying a FOREIGN session
                 UUID: its deliverable IS another session's entry, so its own
                 summary is near-zero-value noise.
  empty        — nothing happened: no user prompts, or <= EMPTY_MAX_MESSAGES.
  trivial      — too small to carry durable signal: no tool activity and only
                 a couple of prompts, or under TRIVIAL_MIN_MESSAGES total.
  pending      — the residue; what `csd unsummarized` serves to the sweep.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg.rows import dict_row

# --- Heuristic thresholds (archive-only; tune with reconcile re-runs) --------

# "empty": message_count at or below this, or zero user prompts.
EMPTY_MAX_MESSAGES = 2
# "trivial": below this many messages total...
TRIVIAL_MIN_MESSAGES = 6
# ...or no tool use and at most this many user prompts (pure micro-chat).
TRIVIAL_MAX_PROMPTS_NO_TOOLS = 2
# Re-eval slack: a self-summarized session's transcript keeps growing for the
# tail of the /session-summary run itself; don't flip on that noise.
GROW_SLACK_DEFAULT = 8

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)
_SESSION_SUMMARY_RE = re.compile(r"session.summary", re.I)


def resolve_kmcp_dsn(csd_dsn: str, explicit: Optional[str] = None) -> str:
    """The kmcp `knowledge` DB lives on the same server/role as the archive —
    swap the database name on the resolved csd DSN unless given explicitly."""
    if explicit:
        return explicit
    parts = urlsplit(csd_dsn)
    return urlunsplit(parts._replace(path="/knowledge"))


def is_meta_run(session_id: str, first_prompt: Optional[str]) -> bool:
    """A /session-summary invocation whose <command-args> carries a FOREIGN
    session UUID (validated ~30/30 in the 2026-06-07 planning run). Also
    matches workflow-dispatched summarizer prompts ("...session-summary skill
    ... <uuid>") since their deliverable is equally another session's entry."""
    if not first_prompt or not _SESSION_SUMMARY_RE.search(first_prompt):
        return False
    own = session_id.lower()
    return any(u.lower() != own for u in _UUID_RE.findall(first_prompt))


@dataclass
class ReconcileStats:
    summarized: int = 0
    not_required: dict[str, int] = field(default_factory=dict)
    pending: int = 0
    grown: int = 0          # subset of pending flipped by the re-eval edge
    changed: int = 0        # summary_state rows actually inserted/updated this run
    duplicates: list["Collision"] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.summarized + sum(self.not_required.values()) + self.pending

    def summary(self) -> str:
        nr = sum(self.not_required.values())
        reasons = ", ".join(f"{k}={v}" for k, v in sorted(self.not_required.items())) or "none"
        lines = [
            f"Reconciled {self.total} sessions ({self.changed} reclassified this run):",
            f"  summarized   {self.summarized}",
            f"  not_required {nr}  ({reasons})",
            f"  pending      {self.pending}" + (f"  (incl. {self.grown} grown past watermark)" if self.grown else ""),
        ]
        if self.duplicates:
            cross = sum(1 for c in self.duplicates if c.apps > 1)
            collide = sum(1 for c in self.duplicates if c.apps == 1)
            lines.append(
                f"session_id reuse: {len(self.duplicates)} ids claimed by >1 entry "
                f"({cross} cross-app copies, {collide} in-app id collisions) — "
                f"not duplicate documents; each entry has a unique path:"
            )
            for c in self.duplicates[:10]:
                lines.append(f"  {c.session_id}  x{c.entries}  ({c.kind}: "
                             f"{c.paths} paths, {c.apps} apps)")
        return "\n".join(lines)


@dataclass
class Collision:
    """A session_id claimed by >1 kmcp session entry. Investigation (2026-06-11)
    found these are NOT duplicate documents — every entry sits at a unique
    (application, path). They split into two kinds:
      - cross_app: the same entry copied into another app (migration artifact).
      - distinct paths within one app: genuinely different sessions/topics that
        were stamped with the same (wrong/placeholder) session_id at authoring.
    `entries` is therefore almost never a deletable-dup count."""
    session_id: str
    entries: int
    paths: int
    apps: int

    @property
    def kind(self) -> str:
        if self.apps > 1 and self.paths <= 1:
            return "cross-app copy"
        if self.apps > 1:
            return "cross-app + collision"
        return "id collision"  # distinct paths, one app


def fetch_kmcp_session_map(kmcp_dsn: str) -> tuple[dict[str, tuple[str, str]], list[Collision]]:
    """session_id -> (application, path) for every kmcp `session` entry.

    Read-only against the knowledge DB. Returns the map plus a Collision per
    session_id claimed by MORE than one entry — characterized (cross-app copy vs
    in-app id collision) rather than reported as a raw "duplicate" count, since
    no two entries actually share an (application, path)."""
    sql = """
        SELECT application, path, content->>'session_id' AS session_id
        FROM entries
        WHERE entity_type = 'session'
          AND content->>'session_id' IS NOT NULL
        ORDER BY created_at
    """
    seen: dict[str, tuple[str, str]] = {}
    rows: dict[str, int] = {}
    paths: dict[str, set[str]] = {}
    apps: dict[str, set[str]] = {}
    with psycopg.connect(kmcp_dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            for r in cur.fetchall():
                sid = r["session_id"].lower()
                seen.setdefault(sid, (r["application"], r["path"]))  # first (oldest) wins
                rows[sid] = rows.get(sid, 0) + 1
                paths.setdefault(sid, set()).add(r["path"])
                apps.setdefault(sid, set()).add(r["application"])
    collisions = [
        Collision(sid, rows[sid], len(paths[sid]), len(apps[sid]))
        for sid in rows if rows[sid] > 1
    ]
    return seen, sorted(collisions, key=lambda c: -c.entries)


_UPSERT_SQL = """
    INSERT INTO summary_state
        (session_id, state, reason, kmcp_application, kmcp_path,
         message_count_at_summary, leaf_uuid_at_summary, updated_at)
    VALUES (%(session_id)s, %(state)s, %(reason)s, %(kmcp_application)s,
            %(kmcp_path)s, %(message_count_at_summary)s,
            %(leaf_uuid_at_summary)s, now())
    ON CONFLICT (session_id) DO UPDATE SET
        state = EXCLUDED.state,
        reason = EXCLUDED.reason,
        kmcp_application = EXCLUDED.kmcp_application,
        kmcp_path = EXCLUDED.kmcp_path,
        message_count_at_summary = EXCLUDED.message_count_at_summary,
        leaf_uuid_at_summary = EXCLUDED.leaf_uuid_at_summary,
        updated_at = now()
    WHERE (summary_state.state, coalesce(summary_state.reason, ''),
           coalesce(summary_state.kmcp_path, ''),
           coalesce(summary_state.message_count_at_summary, -1))
       IS DISTINCT FROM
          (EXCLUDED.state, coalesce(EXCLUDED.reason, ''),
           coalesce(EXCLUDED.kmcp_path, ''),
           coalesce(EXCLUDED.message_count_at_summary, -1))
"""


def reconcile(archive_conn: psycopg.Connection, kmcp_dsn: str,
              grow_slack: int = GROW_SLACK_DEFAULT,
              log: Optional[Any] = None) -> ReconcileStats:
    """Classify every non-subagent archived session and upsert summary_state.

    `log`, if given, is called with one-line progress strings at each stage so a
    caller (the CLI) can surface what the gate is doing instead of blocking mute.
    """
    emit = log if callable(log) else (lambda _m: None)
    emit("reading kmcp session ledger…")
    kmcp_map, dups = fetch_kmcp_session_map(kmcp_dsn)
    emit(f"  kmcp ledger: {len(kmcp_map)} session entries"
         + (f", {len(dups)} duplicate ids" if dups else ""))
    stats = ReconcileStats(duplicates=dups)

    with archive_conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT s.session_id, s.first_prompt, s.message_count,
                   s.user_prompt_count, s.tool_use_count, s.last_prompt_leaf_uuid,
                   ss.state AS prev_state,
                   ss.message_count_at_summary AS prev_watermark,
                   ss.leaf_uuid_at_summary AS prev_leaf,
                   ss.kmcp_application AS prev_app, ss.kmcp_path AS prev_path
            FROM sessions s
            LEFT JOIN summary_state ss ON ss.session_id = s.session_id
            WHERE NOT s.is_subagent
        """)
        rows = cur.fetchall()

    emit(f"classifying {len(rows)} archived sessions…")
    upserts: list[dict[str, Any]] = []
    for r in rows:
        sid = r["session_id"]
        msgs = r["message_count"] or 0
        prompts = r["user_prompt_count"] or 0
        tools = r["tool_use_count"] or 0
        verdict: dict[str, Any] = {
            "session_id": sid, "state": None, "reason": None,
            "kmcp_application": None, "kmcp_path": None,
            "message_count_at_summary": None, "leaf_uuid_at_summary": None,
        }

        kmcp_hit = kmcp_map.get(sid.lower())
        if kmcp_hit:
            verdict["kmcp_application"], verdict["kmcp_path"] = kmcp_hit
            prev_wm = r["prev_watermark"]
            if prev_wm is not None and msgs > prev_wm + grow_slack:
                # Re-eval edge: content grew past what the summary captured.
                # Keep the old watermark so the re-summarizer can diff from it.
                verdict.update(state="pending", reason="grown",
                               message_count_at_summary=prev_wm,
                               leaf_uuid_at_summary=r["prev_leaf"])
                stats.pending += 1
                stats.grown += 1
            else:
                # Stamp the watermark once (first-seen); preserve thereafter.
                wm = prev_wm if prev_wm is not None else msgs
                leaf = r["prev_leaf"] if prev_wm is not None else r["last_prompt_leaf_uuid"]
                verdict.update(state="summarized",
                               message_count_at_summary=wm,
                               leaf_uuid_at_summary=leaf)
                stats.summarized += 1
        elif is_meta_run(sid, r["first_prompt"]):
            verdict.update(state="not_required", reason="meta_run")
            stats.not_required["meta_run"] = stats.not_required.get("meta_run", 0) + 1
        elif prompts == 0 or msgs <= EMPTY_MAX_MESSAGES:
            verdict.update(state="not_required", reason="empty")
            stats.not_required["empty"] = stats.not_required.get("empty", 0) + 1
        elif msgs < TRIVIAL_MIN_MESSAGES or (
                tools == 0 and prompts <= TRIVIAL_MAX_PROMPTS_NO_TOOLS):
            verdict.update(state="not_required", reason="trivial")
            stats.not_required["trivial"] = stats.not_required.get("trivial", 0) + 1
        else:
            verdict.update(state="pending")
            stats.pending += 1
        upserts.append(verdict)

    emit(f"writing summary_state ({len(upserts)} rows)…")
    with archive_conn.cursor() as cur:
        cur.executemany(_UPSERT_SQL, upserts)
        # rowcount sums the batch; the conditional ON CONFLICT WHERE means
        # idempotent no-ops don't count — so this is the real churn this run.
        stats.changed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    archive_conn.commit()
    return stats


def mark_summarized(archive_conn: psycopg.Connection, session_id: str,
                    application: str, path: str) -> dict[str, Any]:
    """Stamp a session summarized at its CURRENT message_count/leaf watermark.

    The phase-4 writer calls this right after a verified kmcp write (kmcp
    entries store neither message_count nor leaf uuid, so csd stamps them
    itself — see the task's considerations). Returns the stamped row.
    """
    with archive_conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            INSERT INTO summary_state
                (session_id, state, reason, kmcp_application, kmcp_path,
                 message_count_at_summary, leaf_uuid_at_summary, updated_at)
            SELECT s.session_id, 'summarized', NULL, %s, %s,
                   s.message_count, s.last_prompt_leaf_uuid, now()
            FROM sessions s WHERE s.session_id = %s
            ON CONFLICT (session_id) DO UPDATE SET
                state = 'summarized', reason = NULL,
                kmcp_application = EXCLUDED.kmcp_application,
                kmcp_path = EXCLUDED.kmcp_path,
                message_count_at_summary = EXCLUDED.message_count_at_summary,
                leaf_uuid_at_summary = EXCLUDED.leaf_uuid_at_summary,
                updated_at = now()
            RETURNING *
        """, (application, path, session_id))
        row = cur.fetchone()
    archive_conn.commit()
    if row is None:
        raise ValueError(f"session {session_id} not found in archive — ingest first")
    return row
