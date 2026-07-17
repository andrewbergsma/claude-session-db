"""session_mgmt — the session-management lens over the archive (angles surface).

Open-thread inventory + delta-after-summary detection + digest addressing.
Productizes the manual "which sessions are still open, and did anything land
AFTER the summary?" workflow: one row per recent main session with its TRUE
last activity, summary classification, and an OPEN / CLOSED / LIVE verdict —
plus, for summarized sessions, a classification of the post-watermark tail
(none / confirmation-only / auto-compaction-only / REAL new work). The real
case is the payoff: post-summary tails have carried whole findings the
summaries missed, and this lens flags them as "OPEN-delta, needs re-capture".

Design constraints (same doctrine as reconcile.py):

1. TRUTH FROM THE LEDGER, NOT THE NARRATOR. Last activity is max(messages.ts)
   from the archive — NEVER transcript file mtime. Bulk file touches make
   mtime lie (observed clusters of identical mtimes across dozens of
   sessions); message timestamps cannot be touched retroactively.
   Summarized-ness comes from summary_state, whose own truth is the kmcp
   entries table (see reconcile.py).
2. SOURCE IS NEVER MUTATED. Read-only over the archive, the knowledge DB, and
   the transcripts. No new state tables; no kmcp writes.
3. DEGRADE, DON'T DIE. A missing transcript, an unreachable knowledge DB, or
   an unresolvable watermark degrades that session's delta to "unknown" —
   never a crash of the whole lens.

Watermark resolution (first hit wins):
  leaf   — summary_state.leaf_uuid_at_summary -> messages.ts of that uuid.
  count  — summary_state.message_count_at_summary -> ts of the Nth message.
  kmcp   — the kmcp session entry's created_at (entries.content->>'session_id'
           in the knowledge DB) — the summary can't have covered anything that
           happened after it was written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

from .session_digest import load as load_jsonl
from .session_digest import render as render_digest

PROJECTS_DIR = Path.home() / ".claude" / "projects"

# --- Tunable heuristics ------------------------------------------------------

# Last message within this many minutes => the session is LIVE.
LIVE_MIN_DEFAULT = 15
# Inventory window: sessions whose (superset filter) modified_at is within this
# many days. 0 = no window (every archived main session).
WINDOW_DAYS_DEFAULT = 7
# A post-summary user prompt at or under this length that matches _CONFIRM_RE
# (or is ultra-short) is "confirmation", not new work.
CONFIRM_MAX_CHARS = 80
# Post-summary assistant narration beyond this many chars is REAL new work even
# without tool use — the "$579k finding written in the tail" case.
REAL_ASSISTANT_CHARS = 2000
# This many post-summary tool calls is REAL work even if every call is read-only.
REAL_TOOL_CALLS = 8
# Slack when comparing last activity to the watermark: within this, no delta.
WATERMARK_SLACK_S = 1

_CONFIRM_RE = re.compile(
    r"^(y(es|ep|eah)?|no|ok(ay)?|k|sure|confirm(ed)?|correct|right|good|great|"
    r"perfect|nice|thanks?|thank you|ty|lgtm|looks good( to me)?|go( ahead)?|"
    r"proceed|do it|ship it|approved?|sounds good|done|got it|👍|✅)[.!\s]*$",
    re.I,
)

# Tool names whose post-summary presence is unambiguous new work.
_MUTATING_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# kmcp write tools (mirror of angles._WRITE_TOOLS; kept literal to avoid an
# import cycle with angles -> session_digest -> here).
_KMCP_WRITE_TOOLS = {
    "create_entry", "update_entry", "patch_content", "import_entries",
    "create_relationship", "delete_entry", "delete_relationship", "move_entry",
    "rename_entry", "add_entry_tag", "import_lessons",
}
_GIT_MUTATE_RE = re.compile(
    r"\bgit\b[^|;&]*?\b(commit|push|merge|rebase|reset|stash|tag|cherry-pick)\b")


# --- transcript resolution (worktree-aware) ----------------------------------

def resolve_transcript(session_id: str,
                       file_path: Optional[str] = None) -> Optional[Path]:
    """Locate a session's JSONL. The archive's sessions.file_path is
    authoritative — it already points into worktree-suffixed project dirs
    (…/-Users-…-controltech--claude-worktrees-<name>/<id>.jsonl) that a naive
    base-dir lookup would miss. Fall back to a glob across every project dir."""
    if file_path:
        p = Path(file_path)
        if p.exists():
            return p
    hits = sorted(PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None


def resolve_session_ref(ref: str, dsn: Optional[str] = None) -> tuple[str, Optional[str]]:
    """Resolve a (possibly short-prefix) session ref to (full_id, file_path).

    Archive-first (also recovers file_path for worktree resolution); falls back
    to a transcript glob so the digest path still works with the DB down.
    Raises ValueError on no match / ambiguity.
    """
    ref = ref.strip().lower()
    if dsn:
        try:
            with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as conn:
                rows = conn.execute(
                    "SELECT session_id, file_path FROM sessions "
                    "WHERE session_id LIKE %s AND NOT is_subagent "
                    "ORDER BY modified_at DESC LIMIT 5", (ref + "%",)).fetchall()
            if len(rows) == 1:
                return rows[0]["session_id"], rows[0]["file_path"]
            if len(rows) > 1:
                opts = ", ".join(r["session_id"][:12] for r in rows)
                raise ValueError(f"ambiguous session ref {ref!r}: {opts}")
        except psycopg.Error:
            pass  # DB-free fallback below
    hits = sorted(PROJECTS_DIR.glob(f"*/{ref}*.jsonl"))
    sids = sorted({h.stem for h in hits})
    if len(sids) == 1:
        return sids[0], str(hits[0])
    if len(sids) > 1:
        raise ValueError(f"ambiguous session ref {ref!r}: "
                         + ", ".join(s[:12] for s in sids[:5]))
    raise ValueError(f"no session matches {ref!r} (archive + transcript glob)")


# --- delta-after-summary ------------------------------------------------------

@dataclass
class DeltaReport:
    klass: str                      # real | confirmation_only | auto_compaction_only | none | unknown
    records: int = 0                # post-watermark main-chain records
    prompts: int = 0                # real user prompts among them
    tool_calls: int = 0
    signals: list[str] = field(default_factory=list)   # why it classified as it did
    watermark_ts: Optional[datetime] = None
    watermark_source: str = "none"  # leaf | count | kmcp | none
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"class": self.klass, "records": self.records,
                "prompts": self.prompts, "tool_calls": self.tool_calls,
                "signals": self.signals[:8],
                "watermark_ts": _iso(self.watermark_ts),
                "watermark_source": self.watermark_source, "note": self.note}


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or []
                     if isinstance(b, dict) and b.get("type") == "text")


def _is_confirmation(text: str) -> bool:
    t = text.strip()
    return len(t) <= 15 or (len(t) <= CONFIRM_MAX_CHARS and bool(_CONFIRM_RE.match(t)))


def classify_delta(recs: list[dict], watermark: datetime) -> DeltaReport:
    """Deterministic classification of the post-watermark transcript tail.
    Extraction is code; no LLM (angles doctrine)."""
    rep = DeltaReport(klass="none", watermark_ts=watermark)
    real_prompts: list[str] = []
    confirm_prompts = 0
    assistant_chars = 0
    tool_names: list[str] = []
    compaction = 0

    for rec in recs:
        if rec.get("isSidechain"):
            continue
        ts = _parse_ts(rec.get("timestamp"))
        if ts is None or ts <= watermark + timedelta(seconds=WATERMARK_SLACK_S):
            continue
        rep.records += 1
        typ = rec.get("type")
        msg = rec.get("message", {})
        content = msg.get("content")
        if typ == "user":
            text = _text_of(content).strip()
            if rec.get("isCompactSummary") or text.startswith(
                    "This session is being continued"):
                compaction += 1
                continue
            if rec.get("isMeta") or not text or text.startswith("<") \
                    or text.startswith("[Request interrupted"):
                continue  # local-command-stdout / command wrappers / meta / harness
            if _is_confirmation(text):
                confirm_prompts += 1
            else:
                real_prompts.append(text)
        elif typ == "assistant":
            assistant_chars += len(_text_of(content).strip())
            for b in content or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    name = b.get("name", "?")
                    tool_names.append(name)
                    inp = b.get("input", {}) or {}
                    short = name.rsplit("__", 1)[-1]
                    if name in _MUTATING_TOOLS:
                        rep.signals.append(f"mutation:{name} "
                                           f"{inp.get('file_path', '')}"[:90])
                    elif short in _KMCP_WRITE_TOOLS and (
                            "kmcp" in name or "knowledge" in name):
                        rep.signals.append(f"kmcp-write:{short}")
                    elif name == "Bash" and _GIT_MUTATE_RE.search(
                            inp.get("command") or ""):
                        rep.signals.append("git-mutation")
                    elif name == "Bash" and "knowledge-cli call" in (
                            inp.get("command") or ""):
                        m = re.search(r"knowledge-cli call (\w+)",
                                      inp.get("command") or "")
                        if m and m.group(1) in _KMCP_WRITE_TOOLS:
                            rep.signals.append(f"kmcp-write:{m.group(1)}")

    rep.prompts = len(real_prompts) + confirm_prompts
    rep.tool_calls = len(tool_names)
    if rep.records == 0:
        rep.klass = "none"
        return rep

    real = bool(rep.signals)
    if real_prompts:
        real = True
        rep.signals.extend("prompt:" + re.sub(r"\s+", " ", p)[:90]
                           for p in real_prompts[:3])
    if rep.tool_calls >= REAL_TOOL_CALLS:
        real = True
        rep.signals.append(f"tool-volume:{rep.tool_calls} calls")
    if assistant_chars >= REAL_ASSISTANT_CHARS:
        real = True
        rep.signals.append(f"assistant-narration:{assistant_chars}c")

    if real:
        rep.klass = "real"
    elif confirm_prompts or tool_names or assistant_chars:
        rep.klass = "confirmation_only"
    elif compaction:
        rep.klass = "auto_compaction_only"
    else:
        rep.klass = "none"
    return rep


# Per-process cache so the web lens doesn't re-parse a 7.7MB transcript on
# every poll: (session_id) -> (mtime_ns, watermark_iso, DeltaReport).
_delta_cache: dict[str, tuple[int, Optional[str], DeltaReport]] = {}


def _delta_for_row(row: dict[str, Any]) -> DeltaReport:
    """Delta detection for one summarized inventory row (transcript-based)."""
    wm: Optional[datetime] = row.get("watermark_ts")
    source = row.get("watermark_source", "none")
    if wm is None:
        return DeltaReport(klass="unknown", note="no watermark resolvable")
    last_ts: Optional[datetime] = row.get("last_ts")
    if last_ts is not None and last_ts <= wm + timedelta(seconds=WATERMARK_SLACK_S):
        return DeltaReport(klass="none", watermark_ts=wm, watermark_source=source)

    path = resolve_transcript(row["session_id"], row.get("file_path"))
    if path is None:
        return DeltaReport(klass="unknown", watermark_ts=wm,
                           watermark_source=source,
                           note="transcript not found on disk")
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    cached = _delta_cache.get(row["session_id"])
    if cached and cached[0] == mtime_ns and cached[1] == _iso(wm):
        return cached[2]
    rep = classify_delta(load_jsonl(path), wm)
    rep.watermark_source = source
    _delta_cache[row["session_id"]] = (mtime_ns, _iso(wm), rep)
    return rep


# --- open-thread inventory ----------------------------------------------------

_INVENTORY_SQL = """
    SELECT s.session_id, p.project_name, s.cwd, s.git_branch, s.file_path,
           s.message_count, s.created_at,
           la.last_ts,
           ss.state, ss.reason, ss.kmcp_application, ss.kmcp_path,
           ss.message_count_at_summary, ss.leaf_uuid_at_summary,
           wm.ts AS leaf_ts
    FROM sessions s
    LEFT JOIN projects p USING (project_id)
    LEFT JOIN summary_state ss USING (session_id)
    LEFT JOIN LATERAL (SELECT max(m.ts) AS last_ts FROM messages m
                       WHERE m.session_id = s.session_id) la ON true
    LEFT JOIN LATERAL (SELECT m.ts FROM messages m
                       WHERE m.uuid = ss.leaf_uuid_at_summary LIMIT 1) wm ON true
    WHERE NOT s.is_subagent
      AND (%(days)s = 0
           OR s.modified_at > now() - make_interval(days => %(days)s))
    ORDER BY la.last_ts DESC NULLS LAST
"""

_NTH_TS_SQL = """
    SELECT ts FROM messages WHERE session_id = %s AND ts IS NOT NULL
    ORDER BY ts OFFSET greatest(%s - 1, 0) LIMIT 1
"""

_KMCP_CREATED_SQL = """
    SELECT lower(content->>'session_id') AS sid, min(created_at) AS created_at
    FROM entries
    WHERE entity_type = 'session'
      AND lower(content->>'session_id') = ANY(%s)
    GROUP BY 1
"""


def _fetch_kmcp_created(kmcp_dsn: str, sids: list[str]) -> dict[str, datetime]:
    """created_at of each session's kmcp entry — the watermark of last resort
    (a summary can't cover anything after the moment it was written)."""
    if not sids:
        return {}
    with psycopg.connect(kmcp_dsn, row_factory=dict_row, connect_timeout=5) as conn:
        rows = conn.execute(_KMCP_CREATED_SQL, ([s.lower() for s in sids],)).fetchall()
    return {r["sid"]: r["created_at"] for r in rows if r["created_at"]}


def _verdict(row: dict[str, Any], delta: Optional[DeltaReport],
             live_min: int, now: datetime) -> str:
    last_ts: Optional[datetime] = row.get("last_ts")
    if last_ts is not None and (now - last_ts) <= timedelta(minutes=live_min):
        return "LIVE"
    state = row.get("state")
    if state == "summarized":
        if delta is not None and delta.klass == "real":
            return "OPEN-delta"
        if delta is not None and delta.klass == "unknown":
            return "OPEN?"
        return "CLOSED"
    if state == "not_required":
        return "CLOSED"
    return "OPEN"  # pending, or not yet reconciled


def inventory(dsn: str, kmcp_dsn: Optional[str] = None,
              window_days: int = WINDOW_DAYS_DEFAULT,
              live_min: int = LIVE_MIN_DEFAULT,
              with_delta: bool = True) -> list[dict[str, Any]]:
    """The open-thread inventory: one dict per recent main session.

    TRUE last activity = max(messages.ts). The SQL window filter uses
    sessions.modified_at only as a SUPERSET gate (a transcript's mtime is
    always >= its last message write, so mtime lies only toward "more
    recent") — rows are then re-filtered and ordered on the message-derived
    last_ts here.
    """
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10) as conn:
        conn.read_only = True
        rows = conn.execute(_INVENTORY_SQL, {"days": window_days}).fetchall()

        now = datetime.now(timezone.utc)
        cutoff = (None if window_days == 0
                  else now - timedelta(days=window_days))
        out: list[dict[str, Any]] = []
        need_kmcp: list[dict[str, Any]] = []
        for r in rows:
            if cutoff is not None and (r["last_ts"] is None or r["last_ts"] < cutoff):
                continue  # mtime superset artifact — truly older than the window
            row = dict(r)
            # Watermark resolution: leaf -> count -> kmcp created_at.
            if row["state"] == "summarized":
                if row["leaf_ts"] is not None:
                    row["watermark_ts"] = row["leaf_ts"]
                    row["watermark_source"] = "leaf"
                elif row["message_count_at_summary"]:
                    hit = conn.execute(
                        _NTH_TS_SQL,
                        (row["session_id"], row["message_count_at_summary"]),
                    ).fetchone()
                    row["watermark_ts"] = hit["ts"] if hit else None
                    row["watermark_source"] = "count" if hit else "none"
                else:
                    row["watermark_ts"] = None
                    row["watermark_source"] = "none"
                if row["watermark_ts"] is None:
                    need_kmcp.append(row)
            else:
                row["watermark_ts"] = None
                row["watermark_source"] = "none"
            out.append(row)

    if need_kmcp and kmcp_dsn:
        try:
            created = _fetch_kmcp_created(
                kmcp_dsn, [r["session_id"] for r in need_kmcp])
        except psycopg.Error:
            created = {}  # knowledge DB unreachable — degrade to unknown
        for row in need_kmcp:
            hit = created.get(row["session_id"].lower())
            if hit is not None:
                row["watermark_ts"] = hit
                row["watermark_source"] = "kmcp"

    now = datetime.now(timezone.utc)
    for row in out:
        delta: Optional[DeltaReport] = None
        if with_delta and row["state"] == "summarized":
            delta = _delta_for_row(row)
        row["delta"] = delta.as_dict() if delta else None
        row["verdict"] = _verdict(row, delta, live_min, now)
        row["idle_s"] = (int((now - row["last_ts"]).total_seconds())
                         if row["last_ts"] is not None else None)
    return out


# --- digest surface -----------------------------------------------------------

# Default head/tail windows (transcript records) for a bare `angles digest` —
# full digests can be huge (7.7MB transcripts have been observed).
DIGEST_HEAD_DEFAULT = 40
DIGEST_TAIL_DEFAULT = 120


def digest_for(ref: str, dsn: Optional[str] = None,
               kmcp_dsn: Optional[str] = None, delta: bool = False,
               head: Optional[int] = None, tail: Optional[int] = None,
               full: bool = False, result_head: int = 200,
               full_inputs: bool = False) -> str:
    """Render a session digest addressed by (short) session id.

    Plain mode: head/tail-windowed digest (defaults DIGEST_HEAD/TAIL_DEFAULT;
    --full disables the window). Delta mode: only the post-watermark tail —
    what the existing summary has NOT seen — resolved through the same
    leaf/count/kmcp chain as the inventory (needs the archive; kmcp fallback
    needs the knowledge DB).
    """
    sid, file_path = resolve_session_ref(ref, dsn)
    path = resolve_transcript(sid, file_path)
    if path is None:
        raise ValueError(f"no transcript on disk for session {sid}")

    since: Optional[datetime] = None
    note = ""
    if delta:
        if not dsn:
            raise ValueError("--delta needs the archive DSN to resolve the watermark")
        wm, source = _watermark_for(sid, dsn, kmcp_dsn)
        if wm is None:
            raise ValueError(
                f"no summary watermark resolvable for {sid[:8]} — is it summarized? "
                "(run `csd reconcile-summaries`, or use a plain digest)")
        since = wm
        note = f"DELTA after summary watermark {_iso(wm)} (source: {source})"
        if full or (head is None and tail is None):
            head = tail = None  # delta default: the whole post-watermark tail
    elif not full and head is None and tail is None:
        head, tail = DIGEST_HEAD_DEFAULT, DIGEST_TAIL_DEFAULT

    return render_digest(path, result_head=result_head, full_inputs=full_inputs,
                         head=head, tail=tail, since=since, note=note)


def _watermark_for(sid: str, dsn: str,
                   kmcp_dsn: Optional[str]) -> tuple[Optional[datetime], str]:
    """Standalone watermark resolution for one session (digest --delta path)."""
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10) as conn:
        conn.read_only = True
        row = conn.execute("""
            SELECT ss.leaf_uuid_at_summary, ss.message_count_at_summary, ss.state,
                   (SELECT m.ts FROM messages m
                    WHERE m.uuid = ss.leaf_uuid_at_summary LIMIT 1) AS leaf_ts
            FROM summary_state ss WHERE ss.session_id = %s
        """, (sid,)).fetchone()
        if row and row["leaf_ts"] is not None:
            return row["leaf_ts"], "leaf"
        if row and row["message_count_at_summary"]:
            hit = conn.execute(_NTH_TS_SQL,
                               (sid, row["message_count_at_summary"])).fetchone()
            if hit:
                return hit["ts"], "count"
    if kmcp_dsn:
        try:
            created = _fetch_kmcp_created(kmcp_dsn, [sid])
        except psycopg.Error:
            created = {}
        if sid.lower() in created:
            return created[sid.lower()], "kmcp"
    return None, "none"

