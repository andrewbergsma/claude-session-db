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

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

# --- Natural-key fallback (session_id is unreliable: ~28% of kmcp session
# entries carry none, and 100+ ids are reused across distinct entries — see
# claudecode:lesson/session-id-unreliable-as-kmcp-session-key). When the
# session_id lookup misses, fall back to a PRECISE natural key — normalized
# project path + start time to the minute — used ONLY when it resolves to exactly
# one kmcp entry AND one archive session (both-sides-unique). A false "summarized"
# is silent data loss, so the bar is exactness, never title similarity.
NatKey = tuple[str, str]  # (normalized_path, "YYYY-MM-DDTHH:MM")


def _norm_path(p: Optional[str]) -> Optional[str]:
    if not p:
        return None
    p = p.strip()
    if p.startswith("~"):
        p = os.path.expanduser(p)
    p = p.rstrip("/")
    return p or None


def _minute_iso(ts: Optional[str]) -> Optional[str]:
    """Minute key from a kmcp ISO timestamp string (e.g. 2026-02-05T16:55:00Z)."""
    if not ts or len(ts) < 16 or ts[10] not in "T ":
        return None
    return "T".join(ts[:16].split(" "))


def _minute_dt(dt: Optional[datetime]) -> Optional[str]:
    """Minute key from an archive datetime, normalized to UTC to match kmcp."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M")


def _natkey(path: Optional[str], minute: Optional[str]) -> Optional[NatKey]:
    return (path, minute) if path and minute else None


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
    recovered: int = 0      # subset of summarized matched via the natural-key fallback
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
            f"  summarized   {self.summarized}" + (f"  (incl. {self.recovered} via natural-key fallback)" if self.recovered else ""),
            f"  not_required {nr}  ({reasons})",
            f"  pending      {self.pending}" + (f"  (incl. {self.grown} grown past watermark)" if self.grown else ""),
        ]
        if self.duplicates:
            passes = sum(1 for c in self.duplicates if c.resolved)
            cross = sum(1 for c in self.duplicates if not c.resolved and c.apps > 1)
            collide = sum(1 for c in self.duplicates
                          if not c.resolved and c.apps == 1)
            lines.append(
                f"session_id reuse: {len(self.duplicates)} ids claimed by >1 entry "
                f"({passes} repeat passes — resolved to the latest entry, "
                f"{cross} cross-app copies, {collide} in-app id collisions) — "
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
    (application, path). They split into three kinds:
      - repeat passes: SAME app + SAME project_path — the delta-capture case.
        Re-summarizing a session that grew writes a second entry for the same
        thread; the id is not "reused", it is legitimately claimed twice. These
        are RESOLVED (latest entry wins) instead of dropped.
      - cross_app: the same entry copied into another app (migration artifact).
      - distinct project paths: genuinely different sessions/topics that were
        stamped with the same (wrong/placeholder) session_id at authoring.
    `entries` is therefore almost never a deletable-dup count."""
    session_id: str
    entries: int
    paths: int
    apps: int
    resolved: bool = False   # same app + project => canonical = latest entry

    @property
    def kind(self) -> str:
        if self.resolved:
            return "repeat passes"
        if self.apps > 1 and self.paths <= 1:
            return "cross-app copy"
        if self.apps > 1:
            return "cross-app + collision"
        return "id collision"  # distinct project paths, one app


def fetch_kmcp_session_map(
    kmcp_dsn: str,
) -> tuple[dict[str, tuple[str, str]], list[Collision], dict[NatKey, tuple[str, str]]]:
    """Indexes over every kmcp `session` entry (read-only against knowledge DB):

    1. session_id -> (application, path) — the primary key for the gate.
    2. Collisions — session_ids claimed by >1 entry, characterized (cross-app
       copy vs in-app id collision); not raw "duplicate" counts (no two entries
       share an (application, path)).
    3. natkey -> (application, path) — (normalized project_path, start-minute) for
       entries where that key is UNIQUE corpus-wide; the fallback index for the
       28% of entries that carry no/wrong session_id. Ambiguous keys are dropped.

    Collision discrimination (2026-08-20, repeatable-summarization): a blanket
    "drop every id claimed twice" also drops the DELTA-CAPTURE case, which is not
    id reuse at all — a second pass over the SAME session writes a second entry in
    the same application for the same project_path. Live corpus: 182 colliding
    ids, 97 of them same-app+same-project. Those keep the id and resolve to the
    LATEST entry (the newest pass is the canonical summary); only genuinely
    cross-project / cross-app claims still fall through to the natural-key
    disambiguator.
    """
    sql = """
        SELECT application, path, created_at,
               content->>'session_id'   AS session_id,
               content->>'project_path' AS project_path,
               content->>'started_at'   AS started_at
        FROM entries
        WHERE entity_type = 'session'
        ORDER BY created_at NULLS FIRST, application, path
    """
    seen: dict[str, tuple[str, str]] = {}
    per_sid: dict[str, list[dict[str, Any]]] = {}
    natkey_all: dict[NatKey, set[tuple[str, str]]] = {}
    with psycopg.connect(kmcp_dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            for r in cur.fetchall():
                sid = r["session_id"]
                if sid:
                    per_sid.setdefault(sid.lower(), []).append(r)
                nk = _natkey(_norm_path(r["project_path"]), _minute_iso(r["started_at"]))
                if nk:
                    natkey_all.setdefault(nk, set()).add((r["application"], r["path"]))

    collisions: list[Collision] = []
    for sid, ents in per_sid.items():
        if len(ents) == 1:
            seen[sid] = (ents[0]["application"], ents[0]["path"])
            continue
        projects = {_norm_path(e["project_path"]) for e in ents}
        apps = {e["application"] for e in ents}
        paths = {e["path"] for e in ents}
        # Same app AND same (non-null) project => repeat passes over one session.
        resolved = len(apps) == 1 and len(projects) == 1 and None not in projects
        if resolved:
            # The query is ordered by created_at, so the last row is the newest
            # pass — the canonical entry for the session as it stands today.
            seen[sid] = (ents[-1]["application"], ents[-1]["path"])
        # Otherwise the bare-id pick would be arbitrary — for cross-stamped ids it
        # can be a FOREIGN entry, yielding a false "summarized". Leave the id out of
        # the primary map so those archive sessions fall through to the natural-key
        # disambiguator, which only matches when unique on both sides. See
        # claudecode:lesson/session-id-unreliable-as-kmcp-session-key.
        collisions.append(Collision(sid, len(ents), len(paths), len(apps),
                                    resolved=resolved))
    # Keep only natural keys that resolve to exactly one kmcp entry.
    natkey_unique = {nk: next(iter(v)) for nk, v in natkey_all.items() if len(v) == 1}
    return seen, sorted(collisions, key=lambda c: -c.entries), natkey_unique


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
    kmcp_map, dups, natkey_map = fetch_kmcp_session_map(kmcp_dsn)
    emit(f"  kmcp ledger: {len(kmcp_map)} session entries"
         + (f", {len(dups)} duplicate ids" if dups else "")
         + f"; {len(natkey_map)} unique natural keys")
    stats = ReconcileStats(duplicates=dups)

    with archive_conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT s.session_id, s.first_prompt, s.message_count,
                   s.user_prompt_count, s.tool_use_count, s.last_prompt_leaf_uuid,
                   s.cwd, s.created_at,
                   ss.state AS prev_state,
                   ss.message_count_at_summary AS prev_watermark,
                   ss.leaf_uuid_at_summary AS prev_leaf,
                   ss.kmcp_application AS prev_app, ss.kmcp_path AS prev_path
            FROM sessions s
            LEFT JOIN summary_state ss ON ss.session_id = s.session_id
            WHERE NOT s.is_subagent
        """)
        rows = cur.fetchall()

    # Archive-side natural-key counts: the fallback only fires when the key is
    # unique on BOTH sides (one kmcp entry AND one archive session) — that guard
    # is what makes it false-positive-safe (the true owner of a kmcp entry shares
    # its key, so any contention drops the match).
    arch_natkey_count: dict[NatKey, int] = {}
    for r in rows:
        nk = _natkey(_norm_path(r["cwd"]), _minute_dt(r["created_at"]))
        if nk:
            arch_natkey_count[nk] = arch_natkey_count.get(nk, 0) + 1

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
        via_natkey = False
        if not kmcp_hit:
            # session_id missed — try the precise natural-key fallback, but only
            # when the key is unambiguous on both sides.
            nk = _natkey(_norm_path(r["cwd"]), _minute_dt(r["created_at"]))
            if nk and arch_natkey_count.get(nk) == 1 and nk in natkey_map:
                kmcp_hit = natkey_map[nk]
                via_natkey = True
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
                               reason="natkey" if via_natkey else None,
                               message_count_at_summary=wm,
                               leaf_uuid_at_summary=leaf)
                stats.summarized += 1
                if via_natkey:
                    stats.recovered += 1
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

        # NEVER null an existing watermark. A session that was summarized once and
        # whose kmcp entry no longer resolves (moved, renamed, id restamped) still
        # HAS a captured prefix — dropping the watermark would make the next pass
        # re-summarize the whole transcript and claim it as new work. Carry the
        # prior watermark forward; kmcp_application/path stay NULL because the
        # ledger no longer backs them (truth from the ledger, not the narrator).
        if not kmcp_hit and r["prev_watermark"] is not None:
            verdict["message_count_at_summary"] = r["prev_watermark"]
            verdict["leaf_uuid_at_summary"] = r["prev_leaf"]

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
                   s.message_count,
                   -- The TRUE message tail, not s.last_prompt_leaf_uuid: that
                   -- column is the last USER PROMPT leaf, which resolves for only
                   -- ~18% of sessions (261/1432) and points behind everything the
                   -- session did after its last prompt. The delta window opens at
                   -- the watermark, so a stale leaf re-renders work already
                   -- summarized. See session_mgmt._watermark_for (leaf -> count
                   -- -> kmcp resolution order).
                   (SELECT m.uuid FROM messages m
                    WHERE m.session_id = s.session_id AND m.ts IS NOT NULL
                    ORDER BY m.ts DESC LIMIT 1),
                   now()
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
