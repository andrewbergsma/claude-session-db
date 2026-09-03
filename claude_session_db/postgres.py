"""Postgres archive storage layer for Claude Code session data.

The LOSSLESS ARCHIVE PLANE: parses Claude Code JSONL and writes straight into a
`claude_sessions` Postgres database (a telemetry sibling of the knowledge DB on
the same Postgres host — NEVER the knowledge tables).

Design (per claudecode:design/claude-session-db-postgres-archive):
- Every row keeps a `raw` JSONB escape-hatch so JSONL field drift never forces a
  migration.
- Full per-message `usage` is captured (input + cache_read + cache_creation +
  ephemeral) — the token-economics goldmine.
- No truncation: tool results / content blocks are stored verbatim in `text`
  columns (Postgres TOAST handles multi-MB). A nullable `tldr` sibling is the
  only derived field.
- Sync signal is `*.jsonl` mtime; ingest is idempotent (messages keyed by uuid,
  child rows cleared per source_file before re-insert).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg.types.json import Jsonb

# Schema version
SCHEMA_VERSION = 10  # v9 + the 2026-09-02 code-defect batch: cross-file block/
                     #   result de-duplication (+ v_duplicate_blocks), DISTINCT
                     #   aggregates, sessions.worktree_active, attachments.raw,
                     #   content_blocks.caller, projects.decoded_from, priced/
                     #   unpriced counts on v_token_cost_daily, and the
                     #   v10_* backfills. See DATA_MODEL.md "Migration history".

DEFAULT_DB_NAME = "claude_sessions"

# Cap analytic reads (query/recent/sweep/stats) so a pathological query fails fast
# with a clear error instead of hanging indefinitely. Applied per-transaction via
# set_config(..., is_local=true), so it never touches the long-running ingest path.
ANALYTIC_TIMEOUT_MS = 15_000

# Reap an abandoned transaction on this (autocommit=False) connection. The archive
# is a long-lived daemon connection (csd sweep on a launchd timer); a sweep that
# hangs mid-transaction otherwise sits `idle in transaction` holding locks until
# killed — once jamming the DB for ~9h. Generous (5 min) so it never trips a slow
# JSONL parse that runs between a DELETE and its inserts, but bounded far below the
# multi-hour convoy. See lesson csd-sweep-idle-in-transaction-lock-convoy.
IDLE_TXN_TIMEOUT_MS = 300_000


def resolve_dsn(explicit: Optional[str] = None) -> str:
    """Resolve the connection DSN for the claude_sessions archive.

    Precedence:
      1. explicit argument
      2. $CSD_DATABASE_URL
      3. $DATABASE_URL with its database name swapped to `claude_sessions`
         (DATABASE_URL conventionally points at the sibling `knowledge` DB on
         the same Postgres host)
    """
    if explicit:
        return explicit
    if os.environ.get("CSD_DATABASE_URL"):
        return os.environ["CSD_DATABASE_URL"]
    base = os.environ.get("DATABASE_URL")
    if base:
        parts = urlsplit(base)
        # Replace the path (database name) with claude_sessions, keep everything else
        new = parts._replace(path=f"/{DEFAULT_DB_NAME}")
        return urlunsplit(new)
    raise RuntimeError(
        "No database DSN found. Set CSD_DATABASE_URL (or DATABASE_URL pointing at "
        "the Postgres host). See .env.example."
    )


SCHEMA_SQL = """
-- ============================================================================
-- claude_sessions — lossless archive of Claude Code session transcripts
-- ============================================================================

CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Per-file sync state (sync signal = *.jsonl mtime)
CREATE TABLE IF NOT EXISTS sync_state (
    file_path      TEXT PRIMARY KEY,
    file_mtime_ns  BIGINT NOT NULL,        -- st_mtime_ns for precise change detection
    record_count   INTEGER NOT NULL,
    file_size      BIGINT,
    last_synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS projects (
    project_id   BIGSERIAL PRIMARY KEY,
    encoded_path TEXT UNIQUE NOT NULL,
    decoded_path TEXT NOT NULL,
    project_name TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_projects_name ON projects(project_name);

-- Migration (idempotent, guarded): schema v10 `projects.decoded_from`.
--
-- `decoded_path` / `project_name` froze at whatever the FIRST insert guessed:
-- the conflict path updated only `last_seen_at`, so a project first seen
-- without a usable `cwd` hint kept the naive decode forever, even after a later
-- transcript supplied ground truth. The encoding maps both `/` and `.` to `-`
-- and is not invertible, so that guess is wrong for every dot-directory and
-- every worktree project.
--
-- `decoded_from` records HOW the stored path was obtained, which is what makes
-- a safe upgrade possible:
--
--   'cwd'      the transcript's own cwd re-encodes to this directory name —
--              ground truth, the only reliable inversion available
--   'encoded'  the naive decode — a guess
--   NULL       recorded before v10; provenance unknown, treated as a guess
--
-- A 'cwd' value overwrites; an 'encoded' guess never overwrites anything.
-- Never a downgrade.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'projects'
          AND column_name = 'decoded_from'
    ) THEN
        ALTER TABLE projects ADD COLUMN decoded_from TEXT;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    project_id   BIGINT REFERENCES projects(project_id),
    file_path    TEXT,
    is_subagent  BOOLEAN NOT NULL DEFAULT false,
    parent_session_id TEXT,        -- for subagent sessions
    agent_id     TEXT,             -- subagent hex id

    -- Session-scoped metadata (latest-wins from ai-title/custom-title/etc.)
    ai_title         TEXT,
    custom_title     TEXT,
    first_prompt     TEXT,
    last_prompt      TEXT,
    last_prompt_leaf_uuid TEXT,
    permission_mode  TEXT,
    mode             TEXT,
    bridge_session_id TEXT,
    agent_name       TEXT,
    git_branch       TEXT,
    cwd              TEXT,
    cc_version       TEXT,
    entrypoint       TEXT,

    created_at   TIMESTAMPTZ,
    modified_at  TIMESTAMPTZ,
    message_count INTEGER DEFAULT 0,

    -- Aggregates (recomputed after ingest)
    total_input_tokens          BIGINT DEFAULT 0,
    total_output_tokens         BIGINT DEFAULT 0,
    total_cache_read_tokens     BIGINT DEFAULT 0,
    total_cache_creation_tokens BIGINT DEFAULT 0,
    user_prompt_count INTEGER DEFAULT 0,
    tool_use_count    INTEGER DEFAULT 0,
    error_count       INTEGER DEFAULT 0,
    compact_count     INTEGER DEFAULT 0,
    duration_seconds  DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id);
CREATE INDEX IF NOT EXISTS idx_sessions_modified ON sessions(modified_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_subagent ON sessions(is_subagent);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_agent_id ON sessions(agent_id) WHERE agent_id IS NOT NULL;

-- Migration (idempotent, guarded): own_* aggregate columns. On MAIN sessions
-- the unprefixed aggregate columns keep their historical ROLL-UP meaning
-- (sidechain messages share the parent session_id, so they were always
-- included); own_* carries the main-chain-only counts. Guarded by a catalog
-- check so the ACCESS EXCLUSIVE ALTER fires exactly once (DDL off the hot
-- path — see lesson csd-sweep-idle-in-transaction-lock-convoy).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'sessions'
          AND column_name = 'own_message_count'
    ) THEN
        ALTER TABLE sessions
            ADD COLUMN own_total_input_tokens          BIGINT DEFAULT 0,
            ADD COLUMN own_total_output_tokens         BIGINT DEFAULT 0,
            ADD COLUMN own_total_cache_read_tokens     BIGINT DEFAULT 0,
            ADD COLUMN own_total_cache_creation_tokens BIGINT DEFAULT 0,
            ADD COLUMN own_message_count  INTEGER DEFAULT 0,
            ADD COLUMN own_tool_use_count INTEGER DEFAULT 0,
            ADD COLUMN own_error_count    INTEGER DEFAULT 0;
    END IF;
END $$;

-- Migration (idempotent, guarded): schema v9 session columns.
--
-- All nullable, all additive, none replacing an existing column. Every one is
-- derived from a record type that used to be DROPPED (see session_records), so
-- a row only fills in once its transcript is re-synced — `csd ingest --force`
-- for the back catalogue, the ordinary mtime sweep for live sessions.
--
-- Guarded by a single catalog check on the first column so the ACCESS EXCLUSIVE
-- ALTER fires exactly once, not on every initialize() (DDL off the hot path —
-- see lesson csd-sweep-idle-in-transaction-lock-convoy). ADD COLUMN with no
-- default is O(1) in PG11+, so this does not rewrite the heap.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'sessions'
          AND column_name = 'forked_from_session_id'
    ) THEN
        ALTER TABLE sessions
            -- Fork lineage, from `fork-context-ref` (v2.1.212+). Replaces the
            -- dead `messages.forked_from`, which Claude Code stopped emitting.
            -- Observed only on SIDECHAIN files, so in practice these land on
            -- the CHILD session row "<parent>:<agent_id>": a forked subagent
            -- inherits its parent's context and this says whose, from where,
            -- and how much.
            ADD COLUMN forked_from_session_id TEXT,   -- fork-context-ref.parentSessionId
            ADD COLUMN forked_from_uuid       TEXT,   -- fork-context-ref.parentLastUuid
            ADD COLUMN fork_context_length    INTEGER,-- fork-context-ref.contextLength (records inherited)
            ADD COLUMN fork_agent_id          TEXT,   -- fork-context-ref.agentId (the record's own field)

            -- Relocation + worktree binding. `cwd` keeps its meaning exactly —
            -- the directory the session STARTED in, from its first conversation
            -- record — because a lot of the archive keys off it (the repos
            -- lens, project attribution). `current_cwd` is the LAST known one:
            -- `/cd` (v2.1.169) and worktree moves used to leave a session filed
            -- under a directory it had long since left.
            ADD COLUMN current_cwd     TEXT,
            ADD COLUMN worktree_session JSONB,  -- worktree-state.worktreeSession, verbatim

            -- Claude Code's OWN cost ledger, from the latest `cost-state`
            -- record. Kept as JSONB (drift-proof, and modelUsage is per-model)
            -- plus the scalars a comparison view needs. This is the harness's
            -- number, NEVER csd's — v_session_cost_drift puts the two
            -- side by side.
            ADD COLUMN cost_state                JSONB,
            ADD COLUMN reported_cost_usd         NUMERIC,
            ADD COLUMN reported_total_duration_ms  BIGINT,
            ADD COLUMN reported_api_duration_ms    BIGINT,
            ADD COLUMN reported_tool_duration_ms   BIGINT,
            ADD COLUMN reported_lines_added        INTEGER,
            ADD COLUMN reported_lines_removed      INTEGER,
            ADD COLUMN has_unknown_model_cost      BOOLEAN,

            -- `sessionKind` (e.g. "bg" for a background session). Measured
            -- CONSTANT per session across every record type that carries it
            -- (0 of 2 sessions in a 30-day scan showed more than one value),
            -- so it is a session attribute, not a message one.
            ADD COLUMN session_kind TEXT;
    END IF;
END $$;
-- Migration (idempotent, guarded): schema v10 session columns.
--
-- `worktree_active` — the worktree EXIT signal, which was unrecordable.
-- `worktree-state` carries `worktreeSession: null` when a session LEAVES its
-- worktree (38% of the records in the live archive), and the session upsert
-- COALESCEs, so a null payload could never clear `worktree_session`: once a
-- session had entered a worktree the archive said it was still in one, forever.
-- Encoding the state as a boolean makes the exit expressible:
--
--   NULL   no `worktree-state` record has ever been seen for this session
--   true   the LAST such record carried a worktreeSession object (in a worktree)
--   false  the LAST such record carried `worktreeSession: null` (exited)
--
-- Written last-wins (see `_SESSION_LAST_WINS_COLS`), NOT "first non-null wins";
-- `worktree_session` deliberately keeps its old meaning — the last BINDING ever
-- seen — so the path/branch of the worktree the session was in is not lost when
-- it leaves.
--
-- Guarded by a catalog check so the ACCESS EXCLUSIVE ALTER fires once, not on
-- every sweep tick.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'sessions'
          AND column_name = 'worktree_active'
    ) THEN
        ALTER TABLE sessions ADD COLUMN worktree_active BOOLEAN;
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_sessions_worktree_active
    ON sessions(worktree_active) WHERE worktree_active IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_sessions_forked_from
    ON sessions(forked_from_session_id) WHERE forked_from_session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_current_cwd ON sessions(current_cwd);
CREATE INDEX IF NOT EXISTS idx_sessions_kind
    ON sessions(session_kind) WHERE session_kind IS NOT NULL;

CREATE TABLE IF NOT EXISTS messages (
    uuid        TEXT PRIMARY KEY,
    session_id  TEXT,
    parent_uuid TEXT,
    ts          TIMESTAMPTZ,
    role        TEXT NOT NULL,           -- user | assistant
    message_type TEXT NOT NULL,          -- prompt | tool_result | response

    -- User-side
    prompt_text TEXT,
    prompt_id   TEXT,
    permission_mode TEXT,
    is_meta     BOOLEAN DEFAULT false,
    is_compact_summary BOOLEAN DEFAULT false,
    source_tool_assistant_uuid TEXT,     -- links tool_result -> tool_use's assistant msg
    source_tool_use_id TEXT,

    -- Assistant-side
    model         TEXT,
    api_message_id TEXT,
    request_id    TEXT,
    stop_reason   TEXT,
    stop_details  JSONB,
    is_api_error  BOOLEAN DEFAULT false,
    api_error_status INTEGER,
    error_text    TEXT,
    diagnostics   JSONB,

    -- Attribution (which agent/skill/mcp/plugin produced this assistant msg)
    attribution_agent      TEXT,
    attribution_skill      TEXT,
    attribution_mcp_server TEXT,
    attribution_mcp_tool   TEXT,
    attribution_plugin     TEXT,

    -- Full usage breakdown
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    cache_read_tokens     INTEGER,
    cache_creation_tokens INTEGER,
    ephemeral_5m_tokens   INTEGER,
    ephemeral_1h_tokens   INTEGER,
    service_tier  TEXT,
    inference_geo TEXT,
    speed         TEXT,
    usage         JSONB,                 -- full raw usage object

    -- Context / threading
    is_sidechain BOOLEAN DEFAULT false,
    agent_id     TEXT,
    slug         TEXT,
    cwd          TEXT,
    git_branch   TEXT,
    cc_version   TEXT,
    entrypoint   TEXT,
    -- LEGACY. `forkedFrom` was a top-level {sessionId, messageUuid} on user /
    -- assistant records; Claude Code stopped emitting it at v2.1.212. Kept
    -- because pre-2.1.212 sessions carry real values here and the archive does
    -- not drop columns. The live replacement is the `fork-context-ref` record
    -- -> sessions.forked_from_session_id / _uuid / fork_context_length.
    forked_from  JSONB,

    source_file TEXT NOT NULL,
    source_line INTEGER,
    raw         JSONB
);

-- Migration (idempotent, guarded): schema v9 message columns.
--
-- Plain nullable columns, NOT generated columns over `usage`/`raw`. Postgres 16
-- has only STORED generated columns, and adding one rewrites the whole table —
-- a multi-GB ACCESS EXCLUSIVE rewrite of 1.3M rows inside the 5-minute sweep's
-- initialize(). ADD COLUMN without a default is O(1); the historical values are
-- filled by the resumable, self-committing cursor backfills in run_backfills().
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'messages'
          AND column_name = 'effort'
    ) THEN
        ALTER TABLE messages
            -- Top-level `effort` on assistant records ("high", ...). Present on
            -- 98.5% of them (235,687 of 239,367 in a 30-day scan) and, until
            -- v9, visible only inside `raw`. This is the effort level the turn
            -- actually ran at — the single biggest per-turn cost/quality lever,
            -- and it was unqueryable.
            ADD COLUMN effort TEXT,

            -- `sessionKind` ("bg"). Constant per session in the corpus, so
            -- `sessions.session_kind` is the primary home; this mirror exists
            -- so a message-level query does not need the join, and so a FUTURE
            -- session that does vary is not silently flattened.
            ADD COLUMN session_kind TEXT,

            -- usage sub-fields. The full `usage` object is already archived as
            -- JSONB; these three are promoted because they are the ones the
            -- cost/behaviour lenses actually group by.
            --   thinking_tokens  usage.output_tokens_details.thinking_tokens
            --                    (55% of assistant records) — how much of the
            --                    output was reasoning rather than answer.
            --   server_tool_use  usage.server_tool_use (64%) — server-side tool
            --                    invocations (web search/fetch), billed
            --                    separately from tokens. JSONB: shape varies.
            --   iterations       usage.iterations (64%) — an ARRAY, not a
            --                    count. Each element is a per-iteration usage
            --                    object carrying its OWN `model` and `type`.
            --                    This is where a model FALLBACK is recorded:
            --                        [{"type":"message","model":"claude-fable-5",…},
            --                         {"type":"fallback_message",
            --                          "model":"claude-opus-4-8",…}]
            --                    i.e. the message's top-level `model` is NOT
            --                    the only model that billed for it. v_message_cost
            --                    prices the whole message at the top-level model
            --                    and therefore mis-prices a fallback turn; the
            --                    array is archived so a future view can split it.
            --                    (Same event as the v2.1.247 `fallback` CONTENT
            --                    BLOCK — recorded twice, in two places.)
            --   iteration_count  jsonb_array_length(iterations); 1 normally,
            --                    >1 exactly when a fallback occurred.
            ADD COLUMN thinking_tokens INTEGER,
            ADD COLUMN server_tool_use JSONB,
            ADD COLUMN iterations      JSONB,
            ADD COLUMN iteration_count INTEGER;
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_messages_effort ON messages(effort)
    WHERE effort IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_session_kind ON messages(session_kind)
    WHERE session_kind IS NOT NULL;
-- Partial: the fallback lens. iteration_count > 1 iff the turn fell back to
-- another model mid-message, which is both rare and the case v_message_cost
-- gets wrong.
CREATE INDEX IF NOT EXISTS idx_messages_fallback ON messages(iteration_count)
    WHERE iteration_count > 1;
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_role ON messages(role);
CREATE INDEX IF NOT EXISTS idx_messages_model ON messages(model);
CREATE INDEX IF NOT EXISTS idx_messages_source_file ON messages(source_file);
CREATE INDEX IF NOT EXISTS idx_messages_attr_skill ON messages(attribution_skill);
CREATE INDEX IF NOT EXISTS idx_messages_attr_mcp ON messages(attribution_mcp_server);
CREATE INDEX IF NOT EXISTS idx_messages_src_tool_asst ON messages(source_tool_assistant_uuid);
-- Partial: ~40% of messages are sidechain rows carrying agent_id; per-agent
-- probes (child aggregates, EXISTS checks) seq-scanned without this.
CREATE INDEX IF NOT EXISTS idx_messages_agent ON messages(agent_id) WHERE agent_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS content_blocks (
    block_id    BIGSERIAL PRIMARY KEY,
    message_uuid TEXT NOT NULL,
    session_id  TEXT,
    block_index INTEGER NOT NULL,
    block_type  TEXT NOT NULL,           -- thinking | text | tool_use | the block's own type
                                        --   (v9: unknown types are kept verbatim, see block_payload)
    content     TEXT,                    -- full thinking/text (no truncation)
    char_count  INTEGER,
    signature   TEXT,
    tool_use_id TEXT,
    tool_name   TEXT,
    tool_input  JSONB,                   -- full tool input
    tool_type   TEXT,                    -- mcp | builtin
    mcp_server  TEXT,
    source_file TEXT NOT NULL,
    source_line INTEGER
);
CREATE INDEX IF NOT EXISTS idx_cb_message ON content_blocks(message_uuid);
CREATE INDEX IF NOT EXISTS idx_cb_type ON content_blocks(block_type);
CREATE INDEX IF NOT EXISTS idx_cb_tool ON content_blocks(tool_name);
CREATE INDEX IF NOT EXISTS idx_cb_tool_use_id ON content_blocks(tool_use_id);
CREATE INDEX IF NOT EXISTS idx_cb_source_file ON content_blocks(source_file);

-- Migration (idempotent, guarded): schema v10 `content_blocks.caller`.
--
-- `jsonl_records.ToolUseBlock` has always parsed `tool_use.caller` into a
-- `ToolUseCaller`, and `sync._content_block_row` never wrote it — so it was
-- dropped on 100% of tool_use blocks. Kept VERBATIM as JSONB (the value is
-- `{"type":"direct"}` on every block in the corpus today, which is exactly the
-- kind of field that suddenly is not).
--
-- Deliberately UNINDEXED: one value covers ~100% of rows, so an index on it
-- would be paid for on every insert and used by nothing. Add one when a query
-- needs it.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'content_blocks'
          AND column_name = 'caller'
    ) THEN
        ALTER TABLE content_blocks ADD COLUMN caller JSONB;
    END IF;
END $$;

-- Migration (idempotent, guarded): schema v9 content-block payload.
--
-- `parse_content_block` returned None for any block type it did not recognise,
-- and sync skips a None — so an unrecognised block was DROPPED, and every later
-- block in the same message shifted down one `block_index`, silently corrupting
-- the ordering of the blocks that WERE kept. Claude Code v2.1.247's `fallback`
-- block ({"type":"fallback","from":{"model":…},"to":{"model":…}} — a
-- server-side model fallback, precisely what a cost or reliability lens wants)
-- went that way. Unknown blocks are now stored under their REAL block_type with
-- the payload verbatim here.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'content_blocks'
          AND column_name = 'block_payload'
    ) THEN
        ALTER TABLE content_blocks ADD COLUMN block_payload JSONB;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS tool_results (
    result_id    BIGSERIAL PRIMARY KEY,
    message_uuid TEXT NOT NULL,
    session_id   TEXT,
    tool_use_id  TEXT NOT NULL,
    content_text TEXT,                   -- full verbatim result (no truncation)
    tldr         TEXT,                   -- nullable derived summary (archive plane: null)
    char_count   INTEGER,
    is_error     BOOLEAN DEFAULT false,
    error_class  TEXT,                   -- derived error taxonomy (null unless is_error); see transcript_analyzer.classify_error
    block_count  INTEGER DEFAULT 1,
    tool_use_result JSONB,               -- client-side structured enrichment
    from_overflow_file BOOLEAN DEFAULT false,
    source_file  TEXT NOT NULL,
    source_line  INTEGER
);
-- Migration (idempotent): add error_class to a pre-existing tool_results table
-- before any index references it.
ALTER TABLE tool_results ADD COLUMN IF NOT EXISTS error_class TEXT;
CREATE INDEX IF NOT EXISTS idx_tr_message ON tool_results(message_uuid);
CREATE INDEX IF NOT EXISTS idx_tr_tool_use ON tool_results(tool_use_id);
CREATE INDEX IF NOT EXISTS idx_tr_error ON tool_results(is_error);
CREATE INDEX IF NOT EXISTS idx_tr_error_class ON tool_results(error_class) WHERE error_class IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tr_source_file ON tool_results(source_file);

CREATE TABLE IF NOT EXISTS attachments (
    uuid        TEXT PRIMARY KEY,
    session_id  TEXT,
    parent_uuid TEXT,
    ts          TIMESTAMPTZ,
    attachment_type TEXT,
    attachment  JSONB,
    is_sidechain BOOLEAN DEFAULT false,
    source_file TEXT NOT NULL,
    source_line INTEGER
);
-- Migration (idempotent, guarded): schema v10 `attachments.raw`.
--
-- Every other conversation-flow table keeps the whole record in a `raw` JSONB
-- escape hatch; `attachments` kept only the promoted columns plus the
-- `attachment` sub-object, so an attachment record's own top-level fields
-- (cwd, gitBranch, version, userType, entrypoint, sessionKind, and whatever
-- Claude Code adds next) were parsed and then dropped. Nullable, filled on
-- ingest going forward and by a re-sync; NOT backfillable — the data was never
-- written, so there is nothing in the archive to recover it from.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'attachments'
          AND column_name = 'raw'
    ) THEN
        ALTER TABLE attachments ADD COLUMN raw JSONB;
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_att_session ON attachments(session_id);
CREATE INDEX IF NOT EXISTS idx_att_type ON attachments(attachment_type);
CREATE INDEX IF NOT EXISTS idx_att_source_file ON attachments(source_file);

CREATE TABLE IF NOT EXISTS system_events (
    uuid        TEXT PRIMARY KEY,
    session_id  TEXT,
    parent_uuid TEXT,
    ts          TIMESTAMPTZ,
    subtype     TEXT NOT NULL,
    level       TEXT,
    content     TEXT,
    duration_ms INTEGER,
    message_count INTEGER,
    url         TEXT,
    compact_trigger    TEXT,
    compact_pre_tokens INTEGER,
    logical_parent_uuid TEXT,
    error_status  INTEGER,
    error_type    TEXT,
    error_message TEXT,
    retry_in_ms   DOUBLE PRECISION,
    retry_attempt INTEGER,
    max_retries   INTEGER,
    is_sidechain  BOOLEAN DEFAULT false,
    slug          TEXT,
    source_file   TEXT NOT NULL,
    source_line   INTEGER,
    raw           JSONB
);
CREATE INDEX IF NOT EXISTS idx_sys_session ON system_events(session_id);
CREATE INDEX IF NOT EXISTS idx_sys_subtype ON system_events(subtype);
CREATE INDEX IF NOT EXISTS idx_sys_source_file ON system_events(source_file);

CREATE TABLE IF NOT EXISTS file_history (
    snapshot_id BIGSERIAL PRIMARY KEY,
    session_id  TEXT,
    message_id  TEXT,
    snapshot_message_id TEXT,
    ts          TIMESTAMPTZ,
    file_count  INTEGER,
    has_backups BOOLEAN DEFAULT false,
    is_snapshot_update BOOLEAN DEFAULT false,
    source_file TEXT NOT NULL,
    source_line INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fh_session ON file_history(session_id);
CREATE INDEX IF NOT EXISTS idx_fh_source_file ON file_history(source_file);

CREATE TABLE IF NOT EXISTS file_backups (
    backup_id   BIGSERIAL PRIMARY KEY,
    snapshot_id BIGINT NOT NULL REFERENCES file_history(snapshot_id) ON DELETE CASCADE,
    file_path   TEXT NOT NULL,
    backup_file_name TEXT,
    content_hash TEXT,
    version     INTEGER,
    backup_time TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_fb_snapshot ON file_backups(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_fb_path ON file_backups(file_path);

CREATE TABLE IF NOT EXISTS queue_operations (
    operation_id BIGSERIAL PRIMARY KEY,
    session_id   TEXT,
    ts           TIMESTAMPTZ,
    operation    TEXT NOT NULL,
    content      TEXT,
    source_file  TEXT NOT NULL,
    source_line  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_qo_session ON queue_operations(session_id);
CREATE INDEX IF NOT EXISTS idx_qo_source_file ON queue_operations(source_file);

CREATE TABLE IF NOT EXISTS pr_links (
    pr_link_id   BIGSERIAL PRIMARY KEY,
    session_id   TEXT,
    pr_number    INTEGER,
    pr_url       TEXT,
    pr_repository TEXT,
    ts           TIMESTAMPTZ,
    source_file  TEXT NOT NULL,
    source_line  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pr_session ON pr_links(session_id);
CREATE INDEX IF NOT EXISTS idx_pr_source_file ON pr_links(source_file);

-- Background-task outputs swept from the volatile /private/tmp scratchpad
-- (wiped on reboot — this sweep is the only durable copy). Stored verbatim,
-- keyed (session_id, task filename); idempotent by file mtime; bounded (large
-- files kept to a head + truncation note). Symlinked .output files that
-- resolve into ~/.claude/projects are skipped at sweep time: their content IS
-- a subagent transcript already archived losslessly.
CREATE TABLE IF NOT EXISTS task_outputs (
    session_id    TEXT NOT NULL,
    task_name     TEXT NOT NULL,
    content       TEXT,
    char_count    INTEGER,
    truncated     BOOLEAN DEFAULT false,
    file_size     BIGINT,
    file_mtime_ns BIGINT,
    source_path   TEXT,
    captured_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, task_name)
);

-- ---------------------------------------------------------------------------
-- session_records (schema v9) — the CATCH-ALL for session-scoped record types
-- with no dedicated table.
--
-- Claude Code v2.1.161-258 added a long tail of small, sessionId-keyed records.
-- Each is real data, none warrants its own table, and every one of them used to
-- be DROPPED: the parser collected them into `records["unknown"]` and nothing
-- ever read it. This table ends that class of loss — the payload lands here
-- VERBATIM as JSONB, and a type csd has never seen lands here too rather than
-- on the floor.
--
-- Key: (source_file, source_line). These records carry no uuid, so the file +
-- line IS the natural key. Transcripts are append-only, so it is stable; and
-- `clear_file_data` already wipes a file's rows before re-insert, so the ON
-- CONFLICT below is belt-and-braces on top of that.
--
-- `is_modelled` splits the two populations sharing the table:
--   true  — one of jsonl_records.SESSION_RECORD_TYPES; csd knows the shape and
--           deliberately routes it here.
--   false — a type csd has NEVER seen. Also counted by the SyncStats tripwire,
--           so `SELECT record_type, count(*) FROM session_records
--           WHERE NOT is_modelled GROUP BY 1` is the standing "what is new in
--           Claude Code" query.
--
-- Several of these types ALSO feed derived `sessions` columns (fork lineage,
-- current_cwd, worktree_session, cost_state). The generic row is kept
-- regardless, so a derivation can be changed and recomputed from the archive
-- without re-parsing 500K JSONL records.
CREATE TABLE IF NOT EXISTS session_records (
    session_id  TEXT,
    record_type TEXT NOT NULL,
    ts          TIMESTAMPTZ,            -- from `timestamp`/`ts` where the type has one; else NULL
    agent_id    TEXT,                   -- fork-context-ref and other agent-scoped types
    is_modelled BOOLEAN NOT NULL DEFAULT true,
    payload     JSONB NOT NULL,         -- the record VERBATIM
    source_file TEXT NOT NULL,
    source_line INTEGER NOT NULL,
    PRIMARY KEY (source_file, source_line)
);
CREATE INDEX IF NOT EXISTS idx_sr_session ON session_records(session_id);
CREATE INDEX IF NOT EXISTS idx_sr_type ON session_records(record_type);
CREATE INDEX IF NOT EXISTS idx_sr_ts ON session_records(ts);
-- Partial: the standing "what did Claude Code just add" probe.
CREATE INDEX IF NOT EXISTS idx_sr_unmodelled ON session_records(record_type)
    WHERE NOT is_modelled;

-- Agent lifecycle (started / result) — keyed by content hash `key`
CREATE TABLE IF NOT EXISTS agent_tasks (
    key         TEXT PRIMARY KEY,
    agent_id    TEXT,
    started     BOOLEAN DEFAULT false,
    result      JSONB,
    source_file TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_at_agent ON agent_tasks(agent_id);
CREATE INDEX IF NOT EXISTS idx_at_source_file ON agent_tasks(source_file);

-- ---------------------------------------------------------------------------
-- Pricing reference data (the only NON-session-fact tables: list prices, not
-- transcript data). Token quantities live in `messages`; these supply the $/tok
-- rates so the cost views can turn tokens into dollars. Seeded idempotently with
-- ON CONFLICT DO NOTHING so re-running initialize() never clobbers manual rate
-- edits — update a rate by editing the row, not the seed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS model_pricing (
    model_pattern       TEXT PRIMARY KEY,   -- longest LIKE-prefix match wins vs messages.model
    input_per_mtok      NUMERIC NOT NULL,   -- USD per 1M base (uncached) input tokens
    output_per_mtok     NUMERIC NOT NULL,   -- USD per 1M output tokens
    cache_write_5m_mult NUMERIC NOT NULL DEFAULT 1.25,  -- 5m cache write = 1.25x base input
    cache_write_1h_mult NUMERIC NOT NULL DEFAULT 2.0,   -- 1h cache write = 2.0x base input
    cache_read_mult     NUMERIC NOT NULL DEFAULT 0.10,  -- cache read = 0.1x base input (any TTL)
    effective_from      DATE,
    notes               TEXT
);

CREATE TABLE IF NOT EXISTS service_tier_pricing (
    service_tier TEXT PRIMARY KEY,          -- matches messages.service_tier
    multiplier   NUMERIC NOT NULL DEFAULT 1.0,  -- scales the whole row's cost
    notes        TEXT
);

-- Seed: Anthropic list prices (USD/MTok). VERIFY against current pricing; the
-- view applies flat per-model rates and does NOT model the >200K-input
-- long-context premium (e.g. Sonnet 1M) — refine here if that matters.
INSERT INTO model_pricing (model_pattern, input_per_mtok, output_per_mtok, effective_from, notes) VALUES
    ('claude-opus-4',     15, 75, '2025-01-01', 'Opus 4.x list price'),
    ('claude-sonnet-4',    3, 15, '2025-01-01', 'Sonnet 4.x base (<=200K input)'),
    ('claude-haiku-4',     1,  5, '2025-01-01', 'Haiku 4.5 list price'),
    ('claude-3-5-haiku', 0.80, 4, '2024-11-01', 'Haiku 3.5 list price'),
    ('claude-3-opus',     15, 75, '2024-02-01', 'Opus 3 list price')
ON CONFLICT (model_pattern) DO NOTHING;

-- Claude 5 family (schema v9). Added when a 30-day scan found 234K assistant
-- messages on claude-opus-5 / -sonnet-5 / -fable-5 / -fable-5-1 priced by NO
-- pattern at all, i.e. counted as `unpriced` and silently missing from every
-- cost rollup.
--
-- These rates are NOT taken from a price list — they are SOLVED from Claude
-- Code's own `cost-state` records, which carry per-model {inputTokens,
-- outputTokens, cacheReadInputTokens, cacheCreationInputTokens, costUSD}. Least
-- squares over those observations reproduces every sampled row to the cent:
--   sonnet-5    $2/$10   read 0.10x  write 1.25x   (16/16 rows exact)
--   fable-5-1   $10/$50  read 0.025x write 2.00x   (7/7 rows exact)
--   fable-5     $10/$50  read 0.10x                (write blends 5m/1h)
--   opus-5      $5/$25   read 0.10x                (e.g. 705 in + 73 out +
--                        90,544 cache-write = $0.571250 exactly at 5/25/1.25x)
-- The 0.025 cache-read multiplier ($0.25/MTok) is a **Fable 5.1-only** change;
-- fable-5, opus-5 and sonnet-5 all measure at the standard 0.10x. Applying
-- 0.025 across the family would under-report cache reads ~4x, and cache reads
-- are the dominant term in an agentic transcript.
--
-- Cache WRITE multipliers keep the table defaults (1.25x 5m / 2.0x 1h): the
-- view already splits writes by TTL from ephemeral_5m/1h_tokens, so it prices
-- each bucket correctly where the transcript records the split.
--
-- FAST MODE IS NOT DISTINGUISHABLE FROM THE TRANSCRIPT. Opus 5 fast mode bills
-- at $10/$50 rather than $5/$25, but `usage.speed` is absent on ~36% of
-- assistant records and the model string is identical either way — so ONE FLAT
-- RATE PER MODEL applies here and a fast-mode-heavy session under-reports.
-- (`messages.speed` is archived; a future view can lens it.)
--
-- The `[1m]` long-context variant (`claude-opus-5[1m]` in cost-state) matches
-- the same LIKE prefix. As documented above, the view does not model a
-- long-context premium.
INSERT INTO model_pricing (model_pattern, input_per_mtok, output_per_mtok,
                           cache_read_mult, effective_from, notes) VALUES
    ('claude-opus-5',    5, 25, 0.10,  '2026-01-01', 'Opus 5 (solved from cost-state; fast mode bills 10/50 but is not distinguishable)'),
    ('claude-sonnet-5',  2, 10, 0.10,  '2026-01-01', 'Sonnet 5 (solved from cost-state, exact on 16/16 rows)'),
    ('claude-fable-5',  10, 50, 0.10,  '2026-01-01', 'Fable 5 (solved from cost-state; standard 0.10x cache read)'),
    ('claude-fable-5-1',10, 50, 0.025, '2026-01-01', 'Fable 5.1 — cache reads $0.25/MTok (0.025x), exact on 7/7 rows'),
    ('claude-mythos-5',  10, 50, 0.10,  '2026-01-01', 'Mythos 5 — same tier/price as Fable 5 (not yet observed locally)'),
    ('claude-mythos-5-1',10, 50, 0.025, '2026-01-01', 'Mythos 5.1 — same tier/price as Fable 5.1 (not yet observed locally)'),
    -- Correction, same measurement method: the pre-existing `claude-opus-4`
    -- row prices ALL Opus 4.x at 15/75, but Opus 4.6/4.7/4.8 are 5/25 (an
    -- opus-4-8 cost-state row fits 5/25/1.25x exactly). These longer patterns
    -- win the length ordering; the 15/75 row still covers Opus 4.0-4.5.
    ('claude-opus-4-6',  5, 25, 0.10,  '2025-11-01', 'Opus 4.6 — 5/25, not the 15/75 Opus 4.x row'),
    ('claude-opus-4-7',  5, 25, 0.10,  '2026-01-01', 'Opus 4.7 — 5/25, not the 15/75 Opus 4.x row'),
    ('claude-opus-4-8',  5, 25, 0.10,  '2026-01-01', 'Opus 4.8 — 5/25 (solved from cost-state, exact)')
ON CONFLICT (model_pattern) DO NOTHING;

INSERT INTO service_tier_pricing (service_tier, multiplier, notes) VALUES
    ('standard', 1.0, 'default interactive tier'),
    ('priority', 1.0, 'same per-token list price; committed throughput billed separately'),
    ('batch',    0.5, 'Batch API = 50% of standard')
ON CONFLICT (service_tier) DO NOTHING;

-- ---------------------------------------------------------------------------
-- summary_state — the pre-LLM gate for phase-4 session roll-ups.
--
-- Sibling table keyed by session_id: classifies every archived top-level
-- session as summarized / not_required / pending so the expensive
-- digest->summarizer step only ever runs on the genuinely-pending residue.
-- NEVER mutates archive rows — the transcript stays the lossless source.
--
-- "summarized" is derived ONLY from rows that actually exist in the kmcp
-- `entries` table (entity_type='session', content->>'session_id') — never from
-- a summarizer's self-report. A claimed-but-unwritten summary therefore stays
-- pending and self-heals on the next reconcile. See
-- claudecode:task/claude-session-db/summary-state-and-reconcile-gate.
CREATE TABLE IF NOT EXISTS summary_state (
    session_id  TEXT PRIMARY KEY REFERENCES sessions(session_id) ON DELETE CASCADE,
    state       TEXT NOT NULL CHECK (state IN ('summarized', 'not_required', 'pending')),
    reason      TEXT CHECK (reason IN ('empty', 'meta_run', 'trivial', 'grown', 'natkey')),
    kmcp_application TEXT,   -- where the summary entry lives (when summarized)
    kmcp_path        TEXT,
    -- Re-eval watermark: archive message_count/leaf at the time the session was
    -- marked summarized. kmcp session entries store neither, so csd stamps them
    -- itself (at summarize time for phase-4 writes; first-seen at reconcile for
    -- self-run / historical summaries).
    message_count_at_summary INTEGER,
    leaf_uuid_at_summary     TEXT,
    decided_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_summary_state_state ON summary_state(state);

-- Migration (idempotent, guarded): widen reason to allow 'natkey' (natural-key
-- fallback provenance) on a pre-existing table. Guarded by a catalog check so the
-- ACCESS EXCLUSIVE ALTER fires exactly once, not on every initialize().
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'summary_state_reason_check'
          AND pg_get_constraintdef(oid) LIKE '%natkey%'
    ) THEN
        ALTER TABLE summary_state DROP CONSTRAINT IF EXISTS summary_state_reason_check;
        ALTER TABLE summary_state ADD CONSTRAINT summary_state_reason_check
            CHECK (reason IN ('empty', 'meta_run', 'trivial', 'grown', 'natkey'));
    END IF;
END $$;
"""


# --- summary_passes — the per-pass ledger for repeatable (delta) summaries ----
#
# summary_state holds ONE watermark per session (where the last capture stopped).
# That is enough to open the next delta window, but it forgets the passes
# themselves — which entry captured which slice, and how many passes a session
# has already spent. This table is that ledger: append-only, one row per pass,
# PK (session_id, pass).
#
# It is also the in-flight claim: a pass is inserted 'in_flight' before the LLM
# runs and settled to 'written'/'failed' after, so a console-dispatched summary
# and the launchd timer cannot double-dispatch the same session (belt-and-braces
# with the pg_try_advisory_lock in summarize.run_summarize).
#
# Kept as its own constant so summarize.py can self-heal the table without
# paying initialize()'s full-schema DDL (same pattern as summarize_attempts).
SUMMARY_PASSES_DDL = """
CREATE TABLE IF NOT EXISTS summary_passes (
    session_id  TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    pass        INTEGER NOT NULL CHECK (pass >= 1),
    application TEXT,
    path        TEXT,
    -- Watermark this pass CLOSED at (the next pass's delta window opens here).
    message_count_at_summary INTEGER,
    leaf_uuid_at_summary     TEXT,
    status      TEXT NOT NULL DEFAULT 'in_flight'
                CHECK (status IN ('in_flight', 'written', 'failed')),
    detail      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, pass)
);
CREATE INDEX IF NOT EXISTS idx_summary_passes_status ON summary_passes(status);
"""

# Additive: appended rather than inlined so the DDL has one home (above) that
# summarize.py can execute on its own.
SCHEMA_SQL += SUMMARY_PASSES_DDL


# --- one-time, resumable data backfills (schema v9) --------------------------
#
# A new column is O(1) to ADD but its 1.3M historical rows are not. A single
# `UPDATE messages SET ... WHERE ...` would hold one transaction for minutes
# inside a sweep tick — precisely the `idle in transaction` shape that once
# convoyed the whole database for ~9h (lesson
# csd-sweep-idle-in-transaction-lock-convoy) — and would bloat the heap.
#
# So each backfill WALKS THE PRIMARY KEY in bounded batches, commits each batch,
# and stores its cursor in `metadata`. Properties that matter:
#   * resumable   — a sweep that runs out of its time budget continues on the
#                   next tick from the stored cursor;
#   * bounded     — BACKFILL_MAX_SECONDS per initialize() call, so ingest is
#                   never delayed by more than that;
#   * ordered     — `uuid > cursor ORDER BY uuid` uses the PK index, so the
#                   whole pass is O(n log n), not O(n^2 / batch) as a repeated
#                   `WHERE col IS NULL` scan would be;
#   * idempotent  — every statement is `IS DISTINCT FROM`-guarded, so re-running
#                   one writes nothing, and a completed backfill is marked done
#                   in `metadata` and never re-walked;
#   * additive    — every one of them only ever fills a NULL or corrects a value
#                   the same source data already implies. Nothing is deleted,
#                   no row is removed, no column is rewritten wholesale.
#
# Each entry supplies SQL taking %(after)s / %(limit)s and returning one row
# (next_cursor, scanned, updated).
BACKFILL_BATCH_ROWS = 20_000
BACKFILL_MAX_SECONDS = 20.0

BACKFILLS = [
    {
        "key": "v9_message_effort_usage",
        "desc": "messages.effort / session_kind / thinking_tokens / "
                "server_tool_use / iterations from raw+usage",
        "sql": """
            WITH batch AS (
                SELECT uuid, raw, usage FROM messages
                WHERE uuid > %(after)s ORDER BY uuid LIMIT %(limit)s
            ), upd AS (
                UPDATE messages m SET
                    effort          = b.raw->>'effort',
                    session_kind    = b.raw->>'sessionKind',
                    thinking_tokens = nullif(b.usage->'output_tokens_details'
                                             ->>'thinking_tokens', '')::int,
                    server_tool_use = b.usage->'server_tool_use',
                    iterations      = CASE WHEN jsonb_typeof(b.usage->'iterations') = 'array'
                                           THEN b.usage->'iterations' END,
                    iteration_count = CASE WHEN jsonb_typeof(b.usage->'iterations') = 'array'
                                           THEN jsonb_array_length(b.usage->'iterations') END
                FROM batch b
                WHERE m.uuid = b.uuid
                  AND (m.effort          IS DISTINCT FROM b.raw->>'effort'
                    OR m.session_kind    IS DISTINCT FROM b.raw->>'sessionKind'
                    OR m.thinking_tokens IS DISTINCT FROM
                       nullif(b.usage->'output_tokens_details'->>'thinking_tokens', '')::int
                    OR m.server_tool_use IS DISTINCT FROM b.usage->'server_tool_use'
                    OR m.iterations      IS DISTINCT FROM
                       CASE WHEN jsonb_typeof(b.usage->'iterations') = 'array'
                            THEN b.usage->'iterations' END)
                RETURNING 1
            )
            SELECT (SELECT max(uuid) FROM batch)   AS next_cursor,
                   (SELECT count(*) FROM batch)    AS scanned,
                   (SELECT count(*) FROM upd)      AS updated
        """,
    },
    {
        # See jsonl_records.UserMessage.is_tool_result / sync._user_row.
        # `message_type` was `"prompt" if is_direct_prompt else "tool_result"`,
        # and `is_direct_prompt` is True only for STRING content — so every user
        # prompt carrying an image, a document, or any list-shaped content was
        # filed as a tool_result. This corrects exactly those rows: role=user,
        # typed tool_result, but with NO tool_result block anywhere in the raw
        # content. Guarded twice over (`raw` must actually lack the block), so
        # it can never relabel a genuine tool result.
        "key": "v9_relabel_list_content_prompts",
        "desc": "messages.message_type: list-content user prompts mislabelled "
                "as tool_result",
        "sql": """
            WITH batch AS (
                SELECT uuid, raw, prompt_text FROM messages
                WHERE uuid > %(after)s ORDER BY uuid LIMIT %(limit)s
            ), cand AS (
                SELECT b.uuid,
                       -- text blocks out of list content, joined in order
                       (SELECT string_agg(blk->>'text', E'\\n'
                                          ORDER BY ord)
                        FROM jsonb_array_elements(b.raw->'message'->'content')
                             WITH ORDINALITY AS t(blk, ord)
                        WHERE jsonb_typeof(b.raw->'message'->'content') = 'array'
                          AND blk->>'type' = 'text') AS txt
                FROM batch b
                WHERE jsonb_typeof(b.raw->'message'->'content') = 'array'
                  AND NOT EXISTS (
                      SELECT 1 FROM jsonb_array_elements(b.raw->'message'->'content') e
                      WHERE e->>'type' = 'tool_result')
            ), upd AS (
                UPDATE messages m
                SET message_type = 'prompt',
                    prompt_text  = coalesce(m.prompt_text, c.txt)
                FROM cand c
                WHERE m.uuid = c.uuid
                  AND m.role = 'user'
                  AND m.message_type = 'tool_result'
                RETURNING 1
            )
            SELECT (SELECT max(uuid) FROM batch) AS next_cursor,
                   (SELECT count(*) FROM batch)  AS scanned,
                   (SELECT count(*) FROM upd)    AS updated
        """,
    },
    {
        # `sync._upsert_session` picked first_prompt with `u.is_direct_prompt`
        # — the STRING-ONLY predicate the v9 relabel had already rejected — so
        # a session whose first prompt carried an image, a document, or any
        # list-shaped content got the wrong one (or none): 132 of 2,684 main
        # sessions. The code now uses the v9 rule; this recomputes the history
        # from `messages` (whose message_type IS the v9 rule, and which v9's
        # own relabel backfill already corrected) rather than re-reading 2,000
        # JSONL files.
        #
        # Per-session and idempotent: the earliest non-meta, non-sidechain
        # prompt wins, and the UPDATE is IS DISTINCT FROM-guarded, so a session
        # whose stored value already agrees is not rewritten. Main sessions
        # only — a CHILD row ("<parent>:<agent>") does not match
        # messages.session_id; refresh those with `csd backfill-subagents`.
        "key": "v10_first_prompt",
        "desc": "sessions.first_prompt: recomputed with the v9 prompt rule",
        "sql": """
            WITH batch AS (
                SELECT session_id, is_subagent, first_prompt FROM sessions
                WHERE session_id > %(after)s ORDER BY session_id LIMIT %(limit)s
            ), want AS (
                SELECT b.session_id, m.prompt_text
                FROM batch b
                JOIN LATERAL (
                    SELECT prompt_text FROM messages
                    WHERE session_id = b.session_id
                      AND role = 'user' AND message_type = 'prompt'
                      AND NOT is_meta AND NOT is_sidechain
                      AND prompt_text IS NOT NULL
                    ORDER BY ts NULLS LAST, uuid
                    LIMIT 1
                ) m ON true
                WHERE NOT b.is_subagent
            ), upd AS (
                UPDATE sessions s SET first_prompt = w.prompt_text
                FROM want w
                WHERE s.session_id = w.session_id
                  AND s.first_prompt IS DISTINCT FROM w.prompt_text
                RETURNING 1
            )
            SELECT (SELECT max(session_id) FROM batch) AS next_cursor,
                   (SELECT count(*) FROM batch)        AS scanned,
                   (SELECT count(*) FROM upd)          AS updated
        """,
    },
    {
        # v9 added `session_kind` to BOTH messages and sessions and backfilled
        # only messages, so `sessions.session_kind` was NULL on every row in the
        # archive — the column existed, the index existed, and nothing ever
        # answered "which sessions are background sessions?".
        #
        # `sessionKind` is CONSTANT per session (measured across every record
        # type that carries it), which is what makes this recoverable from
        # `messages` instead of a re-parse. Fills only where the session column
        # IS NULL — it never overwrites a value ingest derived.
        # `content_blocks.caller` is new in v10, and content_blocks are only
        # ever rewritten by a re-sync of their source file — so without this,
        # the column would stay NULL on the whole back catalogue. It IS
        # recoverable: `messages.raw` holds the assistant record verbatim,
        # caller and all.
        #
        # Matched on tool_use_id, NOT on block_index. Before v9 an unrecognised
        # content block was dropped and every LATER block in the message shifted
        # down one index, so the historical `block_index` does not reliably
        # address the raw array; `tool_use_id` is stable and unique within a
        # message either way.
        #
        # The CASE around jsonb_array_elements is load-bearing: a set-returning
        # function in a LATERAL is evaluated before the WHERE clause could
        # filter non-array content, and `jsonb_array_elements` on a string
        # errors out.
        "key": "v10_content_block_caller",
        "desc": "content_blocks.caller from messages.raw (matched on tool_use_id)",
        "sql": """
            WITH batch AS (
                SELECT uuid, raw FROM messages
                WHERE uuid > %(after)s ORDER BY uuid LIMIT %(limit)s
            ), blk AS (
                SELECT b.uuid AS message_uuid,
                       e->>'id'    AS tool_use_id,
                       e->'caller' AS caller
                FROM batch b,
                     LATERAL jsonb_array_elements(
                         CASE WHEN jsonb_typeof(b.raw->'message'->'content') = 'array'
                              THEN b.raw->'message'->'content'
                              ELSE '[]'::jsonb END) e
                WHERE e->>'type' = 'tool_use'
                  AND e ? 'caller'
                  AND coalesce(e->>'id', '') <> ''
            ), upd AS (
                UPDATE content_blocks cb SET caller = blk.caller
                FROM blk
                WHERE cb.message_uuid = blk.message_uuid
                  AND cb.tool_use_id  = blk.tool_use_id
                  AND cb.block_type   = 'tool_use'
                  AND cb.caller IS NULL
                RETURNING 1
            )
            SELECT (SELECT max(uuid) FROM batch) AS next_cursor,
                   (SELECT count(*) FROM batch)  AS scanned,
                   (SELECT count(*) FROM upd)    AS updated
        """,
    },
    {
        "key": "v10_session_kind",
        "desc": "sessions.session_kind from the constant messages.session_kind",
        "sql": """
            WITH batch AS (
                SELECT session_id, session_kind FROM sessions
                WHERE session_id > %(after)s ORDER BY session_id LIMIT %(limit)s
            ), want AS (
                SELECT b.session_id, m.session_kind
                FROM batch b
                JOIN LATERAL (
                    SELECT session_kind FROM messages
                    WHERE session_id = b.session_id AND session_kind IS NOT NULL
                    LIMIT 1
                ) m ON true
                WHERE b.session_kind IS NULL
            ), upd AS (
                UPDATE sessions s SET session_kind = w.session_kind
                FROM want w
                WHERE s.session_id = w.session_id
                  AND s.session_kind IS NULL
                RETURNING 1
            )
            SELECT (SELECT max(session_id) FROM batch) AS next_cursor,
                   (SELECT count(*) FROM batch)        AS scanned,
                   (SELECT count(*) FROM upd)          AS updated
        """,
    },
]


VIEWS_SQL = """
-- Session overview
-- DROP first: the column list grew (own_* / subagent columns), which CREATE OR
-- REPLACE cannot reconcile against an older definition. CASCADE takes the
-- dependent v_unsummarized with it — recreated at the bottom of this script.
DROP VIEW IF EXISTS v_session_overview CASCADE;
CREATE VIEW v_session_overview AS
SELECT s.session_id, p.project_name, p.decoded_path AS project_path,
       COALESCE(s.custom_title, s.ai_title) AS title,
       s.first_prompt, s.is_subagent, s.agent_name,
       s.created_at, s.modified_at, s.git_branch, s.message_count,
       s.total_input_tokens, s.total_output_tokens,
       s.total_cache_read_tokens, s.total_cache_creation_tokens,
       s.user_prompt_count, s.tool_use_count, s.error_count, s.compact_count,
       s.duration_seconds, s.cc_version,
       -- own_* = main-chain only (unprefixed aggregates on mains roll children up)
       s.parent_session_id, s.agent_id,
       s.own_message_count, s.own_tool_use_count, s.own_error_count,
       s.own_total_input_tokens, s.own_total_output_tokens,
       s.own_total_cache_read_tokens, s.own_total_cache_creation_tokens,
       -- schema v9: where the session ENDED UP (a /cd or worktree move used to
       -- leave it filed under the directory it started in), whether it is a
       -- background session, its fork parent, and the harness's own cost.
       s.cwd, s.current_cwd, s.session_kind,
       s.forked_from_session_id, s.forked_from_uuid, s.fork_context_length,
       -- schema v10: worktree_session is the last BINDING ever seen;
       -- worktree_active is the current STATE (null = never in one, false =
       -- left it — the exit COALESCE could never express).
       s.worktree_session, s.worktree_active, s.reported_cost_usd
FROM sessions s
LEFT JOIN projects p ON s.project_id = p.project_id
ORDER BY s.modified_at DESC NULLS LAST;

-- One row per Agent SPAWN in the archived ledger: the Agent tool_use joined to
-- its tool_result (tool_use_result JSONB is the harness's own record of the
-- child — agentId, agentType, status, totals), joined to the child session row.
-- Status is the ledger's, never the agent's self-report. message_uuid anchors
-- the spawn in the parent conversation; child_session_key ("<parent>:<agent>")
-- addresses the child session row / focus view. The lateral prefers the child
-- whose parent matches the spawning session (covers session-forked parents
-- where the sidechain was archived under the resumed session id).
CREATE OR REPLACE VIEW v_agent_children AS
SELECT cb.session_id                   AS parent_session_id,
       cb.message_uuid,
       m.ts                            AS spawned_at,
       cb.tool_use_id,
       tr.tool_use_result->>'agentId'  AS agent_id,
       coalesce(tr.tool_use_result->>'agentType',
                cb.tool_input->>'subagent_type')     AS agent_type,
       cb.tool_input->>'description'   AS description,
       (cb.tool_input->>'run_in_background')::boolean AS run_in_background,
       cb.tool_input->>'model'         AS model,
       tr.tool_use_result->>'status'   AS status,
       (tr.tool_use_result->>'totalTokens')::bigint       AS total_tokens,
       (tr.tool_use_result->>'totalDurationMs')::bigint   AS total_duration_ms,
       (tr.tool_use_result->>'totalToolUseCount')::bigint AS total_tool_use_count,
       tr.tool_use_result->>'resolvedModel' AS resolved_model,
       child.session_id                AS child_session_key
FROM content_blocks cb
JOIN tool_results tr ON tr.tool_use_id = cb.tool_use_id
LEFT JOIN messages m ON m.uuid = cb.message_uuid
LEFT JOIN LATERAL (
    SELECT s.session_id FROM sessions s
    WHERE s.is_subagent AND s.agent_id = tr.tool_use_result->>'agentId'
    ORDER BY (s.parent_session_id = cb.session_id) DESC
    LIMIT 1
) child ON true
WHERE cb.block_type = 'tool_use' AND cb.tool_name = 'Agent'
  AND tr.tool_use_result ? 'agentId';

-- Token usage by model
CREATE OR REPLACE VIEW v_token_usage_by_model AS
SELECT model,
       count(*) AS message_count,
       sum(input_tokens) AS total_input,
       sum(output_tokens) AS total_output,
       sum(cache_read_tokens) AS total_cache_read,
       sum(cache_creation_tokens) AS total_cache_creation,
       round(avg(output_tokens)::numeric, 1) AS avg_output,
       round(100.0 * sum(cache_read_tokens) /
             nullif(sum(input_tokens + coalesce(cache_read_tokens,0) +
                        coalesce(cache_creation_tokens,0)), 0), 2) AS cache_hit_pct
FROM messages
WHERE role = 'assistant' AND model IS NOT NULL
GROUP BY model
ORDER BY total_input DESC;

-- Token absorption by attribution (which skill/mcp/agent burns tokens)
CREATE OR REPLACE VIEW v_token_by_attribution AS
SELECT
    coalesce(attribution_skill, '(none)') AS skill,
    coalesce(attribution_mcp_server, '(none)') AS mcp_server,
    coalesce(attribution_agent, '(none)') AS agent,
    count(*) AS messages,
    sum(output_tokens) AS output_tokens,
    sum(input_tokens) AS input_tokens,
    sum(cache_read_tokens) AS cache_read_tokens
FROM messages
WHERE role = 'assistant'
GROUP BY 1, 2, 3
ORDER BY output_tokens DESC NULLS LAST;

-- Tool usage frequency
CREATE OR REPLACE VIEW v_tool_usage AS
SELECT tool_name, tool_type, mcp_server,
       count(*) AS use_count,
       count(DISTINCT session_id) AS session_count
FROM content_blocks
WHERE block_type = 'tool_use'
GROUP BY tool_name, tool_type, mcp_server
ORDER BY use_count DESC;

-- Errors: every is_error tool_result, classified (error_class) + tool + preview.
-- parallel_cancelled is cascade noise (sibling calls killed when one is rejected),
-- flagged is_noise so mining queries can exclude it without re-deriving the taxonomy.
-- DROP first: column list changed shape, which CREATE OR REPLACE cannot do.
DROP VIEW IF EXISTS v_error_summary CASCADE;
CREATE OR REPLACE VIEW v_error_summary AS
SELECT tr.session_id, cb.tool_name,
       coalesce(tr.error_class, 'unknown') AS error_class,
       (tr.error_class = 'parallel_cancelled') AS is_noise,
       left(tr.content_text, 200) AS error_preview,
       m.ts
FROM tool_results tr
JOIN messages m ON tr.message_uuid = m.uuid
LEFT JOIN content_blocks cb ON tr.tool_use_id = cb.tool_use_id
WHERE tr.is_error
ORDER BY m.ts DESC;

-- Error taxonomy rollup: which failure modes recur, on which tools, how widely.
CREATE OR REPLACE VIEW v_error_by_class AS
SELECT coalesce(tr.error_class, 'unknown') AS error_class,
       cb.tool_name,
       count(*) AS hits,
       count(DISTINCT tr.session_id) AS sessions,
       max(m.ts) AS last_seen
FROM tool_results tr
JOIN messages m ON tr.message_uuid = m.uuid
LEFT JOIN content_blocks cb ON tr.tool_use_id = cb.tool_use_id
WHERE tr.is_error
GROUP BY 1, 2
ORDER BY hits DESC;

-- Error recovery narrative: each real error paired with the agent's next assistant
-- turn (what it said/did to recover) — the "what broke -> how fixed" signal a
-- session summary needs. Excludes parallel_cancelled cascade noise.
CREATE OR REPLACE VIEW v_error_recovery AS
SELECT e.session_id, e.ts AS error_ts, e.tool_name, e.error_class,
       left(e.error_preview, 160) AS error_preview,
       a.next_ts AS recovery_ts,
       left(a.recovery_text, 240) AS recovery_narration
FROM (
    SELECT tr.session_id, m.ts, cb.tool_name,
           coalesce(tr.error_class, 'unknown') AS error_class,
           tr.content_text AS error_preview
    FROM tool_results tr
    JOIN messages m ON tr.message_uuid = m.uuid
    LEFT JOIN content_blocks cb ON tr.tool_use_id = cb.tool_use_id
    WHERE tr.is_error AND tr.error_class IS DISTINCT FROM 'parallel_cancelled'
) e
LEFT JOIN LATERAL (
    SELECT m2.ts AS next_ts,
           string_agg(cb2.content, ' ' ORDER BY cb2.block_index) AS recovery_text
    FROM messages m2
    JOIN content_blocks cb2 ON cb2.message_uuid = m2.uuid AND cb2.block_type = 'text'
    WHERE m2.session_id = e.session_id AND m2.role = 'assistant' AND m2.ts > e.ts
    GROUP BY m2.ts
    ORDER BY m2.ts
    LIMIT 1
) a ON true
ORDER BY e.ts DESC;

-- Daily token spend
CREATE OR REPLACE VIEW v_daily_usage AS
SELECT date_trunc('day', ts)::date AS day,
       count(DISTINCT session_id) AS sessions,
       sum(input_tokens) AS input_tokens,
       sum(output_tokens) AS output_tokens,
       sum(cache_read_tokens) AS cache_read_tokens,
       sum(cache_creation_tokens) AS cache_creation_tokens
FROM messages
WHERE role = 'assistant'
GROUP BY 1
ORDER BY day DESC;

-- Compaction events (the compaction-paradox signal)
CREATE OR REPLACE VIEW v_compaction AS
SELECT session_id, ts, compact_trigger, compact_pre_tokens
FROM system_events
WHERE subtype = 'compact_boundary'
ORDER BY ts DESC;

-- Project activity
CREATE OR REPLACE VIEW v_project_activity AS
SELECT p.project_name, p.decoded_path,
       count(DISTINCT s.session_id) AS session_count,
       max(s.modified_at) AS last_activity,
       sum(s.total_input_tokens) AS total_input_tokens,
       sum(s.total_output_tokens) AS total_output_tokens,
       sum(s.tool_use_count) AS total_tool_uses
FROM projects p
-- child rows excluded: parents already roll their children up, so counting
-- both would double the per-project token totals.
LEFT JOIN sessions s ON p.project_id = s.project_id AND NOT s.is_subagent
GROUP BY p.project_id, p.project_name, p.decoded_path
ORDER BY last_activity DESC NULLS LAST;

-- ---------------------------------------------------------------------------
-- Token cost (the caching lens). Anthropic bills the prompt as three disjoint
-- buckets — base input (1x), cache writes (1.25x for 5m / 2.0x for 1h), cache
-- reads (0.1x) — plus output. v_message_cost is the reusable per-message base
-- that joins each assistant message to its model + tier rates; the rollups just
-- sum it. Rates per 1M tokens, so every term is divided by 1e6.
--   * unpriced rows (no model_pricing match, e.g. non-Anthropic models) yield
--     NULL cost terms (sum() skips them) and are counted via `unpriced` so the
--     rollups never silently undercount.
--   * writes recorded only as a lump cache_creation (legacy rows lacking the
--     ephemeral 5m/1h split) are priced at the 5m rate (the API default TTL).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_message_cost AS
WITH base AS (
    SELECT
        m.uuid, m.session_id, m.ts, m.model, m.service_tier,
        coalesce(m.input_tokens, 0)        AS input_tokens,
        coalesce(m.output_tokens, 0)       AS output_tokens,
        coalesce(m.cache_read_tokens, 0)   AS cache_read_tokens,
        coalesce(m.ephemeral_5m_tokens, 0) AS write_5m_tokens,
        coalesce(m.ephemeral_1h_tokens, 0) AS write_1h_tokens,
        greatest(coalesce(m.cache_creation_tokens, 0)
                 - coalesce(m.ephemeral_5m_tokens, 0)
                 - coalesce(m.ephemeral_1h_tokens, 0), 0) AS write_untiered_tokens,
        pr.input_per_mtok, pr.output_per_mtok,
        pr.cache_write_5m_mult, pr.cache_write_1h_mult, pr.cache_read_mult,
        coalesce(st.multiplier, 1.0) AS tier_mult
    FROM messages m
    LEFT JOIN LATERAL (
        SELECT mp.input_per_mtok, mp.output_per_mtok,
               mp.cache_write_5m_mult, mp.cache_write_1h_mult, mp.cache_read_mult
        FROM model_pricing mp
        WHERE m.model LIKE mp.model_pattern || '%'
        ORDER BY length(mp.model_pattern) DESC   -- most specific pattern wins
        LIMIT 1
    ) pr ON true
    LEFT JOIN service_tier_pricing st ON st.service_tier = m.service_tier
    WHERE m.role = 'assistant' AND m.model IS NOT NULL
)
SELECT
    uuid, session_id, ts, model, service_tier,
    input_tokens, output_tokens, cache_read_tokens,
    write_5m_tokens, write_1h_tokens, write_untiered_tokens,
    (input_per_mtok IS NULL) AS unpriced,
    round(tier_mult * input_per_mtok  * input_tokens / 1e6, 6) AS input_cost,
    round(tier_mult * input_per_mtok  * cache_write_5m_mult
          * (write_5m_tokens + write_untiered_tokens) / 1e6, 6) AS cache_write_5m_cost,
    round(tier_mult * input_per_mtok  * cache_write_1h_mult * write_1h_tokens / 1e6, 6) AS cache_write_1h_cost,
    round(tier_mult * input_per_mtok  * cache_read_mult * cache_read_tokens / 1e6, 6) AS cache_read_cost,
    round(tier_mult * output_per_mtok * output_tokens / 1e6, 6) AS output_cost,
    round(tier_mult * (
          input_per_mtok  * input_tokens
        + input_per_mtok  * cache_write_5m_mult * (write_5m_tokens + write_untiered_tokens)
        + input_per_mtok  * cache_write_1h_mult * write_1h_tokens
        + input_per_mtok  * cache_read_mult * cache_read_tokens
        + output_per_mtok * output_tokens
    ) / 1e6, 6) AS total_cost
FROM base;

-- Cost by model, split by caching lens (the headline view).
CREATE OR REPLACE VIEW v_token_cost_by_model AS
SELECT model,
       count(*) AS messages,
       count(*) FILTER (WHERE unpriced) AS unpriced_messages,
       sum(input_tokens) AS input_tokens,
       sum(write_5m_tokens + write_untiered_tokens) AS cache_write_5m_tokens,
       sum(write_1h_tokens) AS cache_write_1h_tokens,
       sum(cache_read_tokens) AS cache_read_tokens,
       sum(output_tokens) AS output_tokens,
       round(sum(input_cost), 4)           AS input_cost,
       round(sum(cache_write_5m_cost), 4)  AS cache_write_5m_cost,
       round(sum(cache_write_1h_cost), 4)  AS cache_write_1h_cost,
       round(sum(cache_read_cost), 4)      AS cache_read_cost,
       round(sum(output_cost), 4)          AS output_cost,
       round(sum(total_cost), 4)           AS total_cost
FROM v_message_cost
GROUP BY model
ORDER BY total_cost DESC NULLS LAST;

-- Daily spend (caching lens), USD.
CREATE OR REPLACE VIEW v_token_cost_daily AS
SELECT date_trunc('day', ts)::date AS day,
       count(DISTINCT session_id) AS sessions,
       round(sum(input_cost), 4)                              AS input_cost,
       round(sum(cache_write_5m_cost + cache_write_1h_cost), 4) AS cache_write_cost,
       round(sum(cache_read_cost), 4)                         AS cache_read_cost,
       round(sum(output_cost), 4)                             AS output_cost,
       round(sum(total_cost), 4)                              AS total_cost
FROM v_message_cost
GROUP BY 1
ORDER BY day DESC;

-- ---------------------------------------------------------------------------
-- v_session_cost_drift (schema v9) — csd's COMPUTED cost vs Claude Code's own
-- REPORTED cost, per session.
--
-- Two independent numbers that ought to agree, and until v9 only one of them
-- existed. `computed_cost_usd` sums v_message_cost (tokens from the transcript
-- x the model_pricing rates); `reported_cost_usd` is `cost-state.totalCostUSD`,
-- the harness's own running total. The view carries what is needed to ATTRIBUTE
-- a gap rather than merely display one:
--
--   * `api_message_ratio` > 1 — THE BIG ONE, and this view found it on its
--     first run. One API response can appear as SEVERAL `messages` rows with
--     distinct uuids but the same `message.id`, so v_message_cost sums the same
--     usage object more than once. A real session measured 1,756 assistant rows
--     against 905 distinct api_message_ids — a ratio of 1.94, and a computed
--     cost almost exactly double the harness's. Treat any session with a ratio
--     meaningfully above 1.0 as over-counted by roughly that factor.
--     (Deduplicating v_message_cost by api_message_id is the fix, and it is
--     deliberately NOT done here: it changes every historical cost number in
--     the archive and deserves its own change with its own verification.)
--   * `unpriced_messages` > 0 — a model with no model_pricing pattern. This is
--     the failure the Claude 5 seed fixed, and the original reason for the view.
--   * sidechain roll-up — assistant rows for SUBAGENTS share the parent's
--     session_id (source is never re-shaped), so `computed` includes child
--     spend. Whether `cost-state` does is not documented by Claude Code.
--   * fast mode — Opus 5 fast bills 10/50 instead of 5/25 and is NOT
--     distinguishable from the transcript, so a fast-heavy session reads LOW.
--   * model FALLBACK — `usage.iterations` shows a turn can bill partly to a
--     second model (see messages.iteration_count > 1); v_message_cost prices
--     the whole message at the top-level `model`.
--   * long-context (>200K) premiums, which the flat per-model rates ignore.
--
-- Sessions with no cost-state record yet (never re-synced since v9, or an
-- older Claude Code) have a NULL reported side and a NULL drift — never a
-- fake zero.
-- DROP first, per this file's convention for a view whose column list can
-- grow: CREATE OR REPLACE cannot reconcile a new column against an older
-- definition and fails with "cannot change name of view column".
DROP VIEW IF EXISTS v_session_cost_drift;
CREATE VIEW v_session_cost_drift AS
WITH computed AS (
    SELECT c.session_id,
           round(sum(c.total_cost), 6) AS computed_cost_usd,
           count(*) AS priced_messages,
           count(*) FILTER (WHERE c.unpriced) AS unpriced_messages,
           count(DISTINCT m.api_message_id) AS distinct_api_messages,
           count(*) FILTER (WHERE m.is_sidechain) AS sidechain_messages,
           count(*) FILTER (WHERE m.iteration_count > 1) AS fallback_messages
    FROM v_message_cost c
    JOIN messages m ON m.uuid = c.uuid
    GROUP BY c.session_id
)
SELECT s.session_id,
       p.project_name,
       coalesce(s.custom_title, s.ai_title) AS title,
       s.modified_at,
       c.computed_cost_usd,
       s.reported_cost_usd,
       round(s.reported_cost_usd - c.computed_cost_usd, 6) AS drift_usd,
       round(100.0 * (s.reported_cost_usd - c.computed_cost_usd)
             / nullif(s.reported_cost_usd, 0), 2)           AS drift_pct,
       c.priced_messages,
       c.unpriced_messages,
       c.distinct_api_messages,
       -- >1.0 means v_message_cost summed the same API response more than once
       round(c.priced_messages::numeric
             / nullif(c.distinct_api_messages, 0), 3) AS api_message_ratio,
       c.sidechain_messages,
       c.fallback_messages,
       s.has_unknown_model_cost,
       -- the harness's own per-model breakdown, for attributing a drift
       s.cost_state -> 'modelUsage' AS reported_model_usage,
       s.reported_total_duration_ms,
       s.reported_api_duration_ms,
       s.reported_lines_added,
       s.reported_lines_removed
FROM sessions s
LEFT JOIN computed c ON c.session_id = s.session_id
LEFT JOIN projects p ON p.project_id = s.project_id
WHERE NOT s.is_subagent
  AND (s.reported_cost_usd IS NOT NULL OR c.computed_cost_usd IS NOT NULL)
ORDER BY abs(coalesce(s.reported_cost_usd, 0) - coalesce(c.computed_cost_usd, 0)) DESC;

-- ---------------------------------------------------------------------------
-- v_duplicate_blocks (schema v10) — the HISTORICAL cross-file duplication of a
-- message's content_blocks / tool_results, made visible.
--
-- `messages` inserts ON CONFLICT (uuid) DO NOTHING, so a record present in two
-- transcripts (a resumed/forked session re-writing the same uuids, a sidechain
-- copied into a second file) yields ONE message row. `content_blocks` and
-- `tool_results` have no uniqueness at all and `clear_file_data` deletes only by
-- `source_file`, so the SECOND file's blocks/results were appended beside the
-- first file's — 2.7% of recent assistant messages, 3,339 duplicated
-- (message_uuid, tool_use_id) pairs across 300 recent sessions. That inflated
-- sessions.tool_use_count / error_count.
--
-- Since v10 the ingest path no longer creates these (sync skips block/result
-- rows for a message whose row is owned by a DIFFERENT source_file), and
-- recompute_session_aggregates counts DISTINCT tool_use_id / distinct error
-- rows so the aggregates are right despite the history. Nothing is deleted
-- automatically: existing duplicates are data, and a bulk DELETE is exactly the
-- long-transaction shape that once convoyed this database for ~9h.
--
-- OPERATOR CLEANUP RECIPE (deliberate, off the sweep's hot path, in batches —
-- run it yourself when you want the heap back; keep the row owned by the file
-- that owns the message row):
--
--   -- 1. Look before you delete.
--   SELECT kind, count(*) AS messages, sum(row_count) AS rows
--   FROM v_duplicate_blocks GROUP BY 1;
--
--   -- 2. Delete, in bounded batches, only the rows whose source_file is NOT
--   --    the one that owns the message. Repeat until it reports 0.
--   WITH doomed AS (
--       SELECT cb.block_id
--       FROM content_blocks cb
--       JOIN messages m ON m.uuid = cb.message_uuid
--       WHERE cb.source_file <> m.source_file
--       LIMIT 20000
--   )
--   DELETE FROM content_blocks c USING doomed d WHERE c.block_id = d.block_id;
--   -- (same shape for tool_results, keyed result_id)
--
--   -- 3. Then re-run aggregates:  csd query "SELECT 1"  is not enough —
--   --    use `csd ingest` (which ends in recompute_session_aggregates), or
--   --    call SessionArchive.recompute_session_aggregates() directly.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_duplicate_blocks AS
SELECT message_uuid, session_id, kind, source_files, row_count
FROM (
    SELECT cb.message_uuid,
           min(cb.session_id)                 AS session_id,
           'content_blocks'::text             AS kind,
           count(DISTINCT cb.source_file)     AS source_files,
           count(*)                           AS row_count
    FROM content_blocks cb
    GROUP BY cb.message_uuid
    HAVING count(DISTINCT cb.source_file) > 1
    UNION ALL
    SELECT tr.message_uuid,
           min(tr.session_id),
           'tool_results'::text,
           count(DISTINCT tr.source_file),
           count(*)
    FROM tool_results tr
    GROUP BY tr.message_uuid
    HAVING count(DISTINCT tr.source_file) > 1
) d
ORDER BY row_count DESC, message_uuid;

-- Phase-4 work queue: pending-only sessions the sweep should summarize next.
-- This replaces the recent-by-mtime walk (which is ~80% already-summarized —
-- see claudecode:lesson/recent-by-mtime-backlog-is-mostly-already-summarized).
CREATE OR REPLACE VIEW v_unsummarized AS
SELECT o.session_id, o.project_name, o.project_path, o.title, o.first_prompt,
       o.created_at, o.modified_at, o.message_count, o.user_prompt_count,
       o.tool_use_count, o.error_count, o.total_output_tokens,
       ss.reason, ss.decided_at
FROM v_session_overview o
JOIN summary_state ss ON ss.session_id = o.session_id
WHERE ss.state = 'pending'
  AND NOT o.is_subagent
ORDER BY o.modified_at DESC NULLS LAST;
"""

# Tables cleared per source_file before re-inserting that file's rows
PER_FILE_TABLES = [
    "messages", "content_blocks", "tool_results", "attachments",
    "system_events", "queue_operations", "pr_links", "agent_tasks",
    "session_records",
]


def scrub(value: Any) -> Any:
    """Recursively strip NUL bytes (\\u0000) from strings.

    Postgres `text` and `jsonb` cannot store U+0000; a handful of tool results
    embed raw NULs (gzip headers, binary layout dumps, stack traces). We strip
    only the NUL — every other byte is preserved verbatim, and the on-disk JSONL
    (recorded via source_file) remains the ultimate source of truth.
    """
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def _j(value: Any) -> Optional[Jsonb]:
    """Wrap a Python value as JSONB (NUL-scrubbed), or None."""
    return Jsonb(scrub(value)) if value is not None else None


@dataclass
class SessionArchive:
    """Postgres archive for Claude Code session data."""

    dsn: str
    conn: Optional[psycopg.Connection] = field(default=None, repr=False)

    def connect(self) -> psycopg.Connection:
        if self.conn is None or self.conn.closed:
            # idle_in_transaction_session_timeout reaps an abandoned txn if a sweep
            # hangs mid-transaction, so it can never hold locks indefinitely.
            self.conn = psycopg.connect(
                self.dsn, autocommit=False,
                options=f"-c idle_in_transaction_session_timeout={IDLE_TXN_TIMEOUT_MS}",
            )
        return self.conn

    def close(self) -> None:
        if self.conn and not self.conn.closed:
            self.conn.close()
        self.conn = None

    def __enter__(self) -> "SessionArchive":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- schema -------------------------------------------------------------

    def initialize(self, backfill_log=None) -> None:
        conn = self.connect()
        with conn.cursor() as cur:
            # Tables/indexes: IF NOT EXISTS, cheap and low-conflict — run every time
            # so the schema self-heals.
            cur.execute(SCHEMA_SQL)
            cur.execute(
                "INSERT INTO metadata(key, value) VALUES ('schema_version', %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (str(SCHEMA_VERSION),),
            )
            # Views: CREATE OR REPLACE VIEW takes ACCESS EXCLUSIVE on each view, which
            # on a per-sweep timer can convoy every reader behind it. Recreate views
            # only when their version marker lags the code's SCHEMA_VERSION (i.e. a
            # migration), not on every initialize(). See lesson
            # csd-sweep-idle-in-transaction-lock-convoy.
            cur.execute("SELECT value FROM metadata WHERE key = 'views_version'")
            row = cur.fetchone()
            if (row[0] if row else None) != str(SCHEMA_VERSION):
                cur.execute(VIEWS_SQL)
                cur.execute(
                    "INSERT INTO metadata(key, value) VALUES ('views_version', %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    (str(SCHEMA_VERSION),),
                )
        conn.commit()
        # Historical data for the new v9 columns. Bounded + resumable, and it
        # commits its own batches — so it deliberately runs AFTER the DDL commit
        # above rather than inside that transaction.
        self.run_backfills(log=backfill_log)

    # -- backfills ----------------------------------------------------------

    def _meta_get(self, key: str) -> Optional[str]:
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM metadata WHERE key = %s", (key,))
            row = cur.fetchone()
        return row[0] if row else None

    def _meta_set(self, key: str, value: str) -> None:
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO metadata(key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, value),
            )

    def run_backfills(self, max_seconds: float = BACKFILL_MAX_SECONDS,
                      batch_rows: int = BACKFILL_BATCH_ROWS,
                      log=None) -> dict:
        """Advance the one-time schema data backfills. Bounded and resumable.

        Walks a table's PK in committed batches (`messages.uuid` or
        `sessions.session_id` — whichever the backfill's own SQL orders by; see
        the BACKFILLS comment for why it is not one big UPDATE), spending at
        most `max_seconds` per call. A backfill that reaches the end of the table is marked `done` in
        `metadata` and never walked again; one that runs out of budget resumes
        from its stored cursor on the next sweep tick.

        Returns {key: {updated, scanned, done}}. Never raises: a failure rolls
        back that backfill's batch, is reported, and does not stop ingest —
        this runs inside `initialize()`, on the sweep's hot path.
        """
        import time as _t
        emit = log if callable(log) else (lambda _m: None)
        conn = self.connect()
        deadline = _t.monotonic() + max_seconds
        report: dict[str, dict] = {}

        for spec in BACKFILLS:
            key, done_key = spec["key"], f"backfill:{spec['key']}:done"
            cursor_key = f"backfill:{spec['key']}:cursor"
            state = {"updated": 0, "scanned": 0, "done": False}
            report[key] = state
            try:
                if self._meta_get(done_key) == "1":
                    state["done"] = True
                    conn.commit()
                    continue
                cursor = self._meta_get(cursor_key) or ""
                while _t.monotonic() < deadline:
                    with conn.cursor() as cur:
                        cur.execute("SET LOCAL statement_timeout = '120s'")
                        cur.execute(spec["sql"],
                                    {"after": cursor, "limit": batch_rows})
                        next_cursor, scanned, updated = cur.fetchone()
                    state["scanned"] += int(scanned or 0)
                    state["updated"] += int(updated or 0)
                    if not scanned:
                        self._meta_set(done_key, "1")
                        state["done"] = True
                        conn.commit()
                        emit(f"  backfill {key}: complete "
                             f"({state['updated']:,} rows updated)")
                        break
                    cursor = next_cursor or cursor
                    self._meta_set(cursor_key, cursor)
                    conn.commit()          # bound the transaction to one batch
                else:
                    conn.commit()
                    emit(f"  backfill {key}: paused at {cursor[:8]}… "
                         f"({state['updated']:,} updated so far; resumes next run)")
            except psycopg.Error as exc:
                conn.rollback()
                state["error"] = f"{type(exc).__name__}: {exc}"
                emit(f"  backfill {key}: ERROR {state['error']} (will retry)")
        return report

    def ensure_gate_objects(self, lock_timeout_ms: int = 15_000) -> bool:
        """Cheap self-heal for the reconcile path: ensure summary_state +
        v_unsummarized exist WITHOUT paying initialize()'s full-schema DDL.

        initialize() re-runs ALTER TABLE / CREATE INDEX every call; those take
        ACCESS EXCLUSIVE locks that convoy behind concurrent ingests — the 200s
        silent hang reconcile used to take. Here we check the catalog first
        (to_regclass: no locks); only if an object is genuinely missing do we run
        the DDL, and then under a bounded lock_timeout so it fails fast instead of
        blocking forever. Returns True iff DDL ran.
        """
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.summary_state') IS NOT NULL, "
                        "to_regclass('public.v_unsummarized') IS NOT NULL")
            has_table, has_view = cur.fetchone()
        if has_table and has_view:
            return False
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = %s", (f"{lock_timeout_ms}ms",))
        self.initialize()
        return True

    def drop_all(self) -> None:
        """Drop every object (for --rebuild). Schema is recreated by initialize()."""
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        conn.commit()

    # -- sync state ---------------------------------------------------------

    def get_sync_mtime_ns(self, file_path: str) -> Optional[int]:
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute("SELECT file_mtime_ns FROM sync_state WHERE file_path=%s", (file_path,))
            row = cur.fetchone()
        return row[0] if row else None

    def needs_sync(self, file_path: str, mtime_ns: int) -> bool:
        prev = self.get_sync_mtime_ns(file_path)
        return prev != mtime_ns

    def update_sync_state(self, file_path: str, mtime_ns: int, record_count: int, file_size: int) -> None:
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO sync_state(file_path, file_mtime_ns, record_count, file_size, last_synced_at)
                   VALUES (%s, %s, %s, %s, now())
                   ON CONFLICT (file_path) DO UPDATE SET
                     file_mtime_ns=EXCLUDED.file_mtime_ns,
                     record_count=EXCLUDED.record_count,
                     file_size=EXCLUDED.file_size,
                     last_synced_at=now()""",
                (file_path, mtime_ns, record_count, file_size),
            )
        conn.commit()

    def clear_file_data(self, source_file: str) -> None:
        """Delete all rows originating from a source file (idempotent re-sync)."""
        conn = self.connect()
        with conn.cursor() as cur:
            for table in PER_FILE_TABLES:
                cur.execute(f"DELETE FROM {table} WHERE source_file = %s", (source_file,))
            # file_backups cascade from file_history
            cur.execute(
                "DELETE FROM file_history WHERE source_file = %s", (source_file,)
            )
        conn.commit()

    def message_uuids_owned_elsewhere(self, uuids: list[str],
                                      source_file: str) -> set[str]:
        """Of `uuids`, those whose `messages` row belongs to a DIFFERENT file.

        The de-duplication seam (schema v10). `messages` inserts ON CONFLICT
        (uuid) DO NOTHING, so a record present in two transcripts keeps the row
        the FIRST file wrote — but `content_blocks` / `tool_results` had no
        uniqueness, and `clear_file_data` deletes only by `source_file`, so the
        second file appended a second set of blocks/results beside the first.

        Chosen fix: SKIP, not delete-then-insert-by-message_uuid. Deleting by
        message_uuid inside this file's transaction would remove rows another
        file OWNS (rows that file's own `clear_file_data` is responsible for),
        breaking the per-file clear invariant the whole ingest path rests on —
        and those rows would then never come back until that other file's mtime
        changed. Skipping keeps exactly one rule: a message's child rows belong
        to the file that owns the message row, and are rewritten only when THAT
        file is re-synced.
        """
        if not uuids:
            return set()
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT uuid FROM messages "
                "WHERE uuid = ANY(%s) AND source_file IS DISTINCT FROM %s",
                (list({u for u in uuids if u}), source_file),
            )
            return {r[0] for r in cur.fetchall()}

    # -- projects / sessions ------------------------------------------------

    def get_or_create_project(self, encoded_path: str, decoded_path: str,
                              decoded_from: str = "encoded") -> int:
        """Upsert a project row, UPGRADING a guessed path when ground truth arrives.

        `decoded_from` is 'cwd' when `decoded_path` came from a transcript's own
        `cwd` (which re-encodes to this exact directory name — the only reliable
        inversion of an encoding that maps both `/` and `.` to `-`), else
        'encoded' for the naive decode.

        The conflict path used to update only `last_seen_at`, so the FIRST
        insert's guess was permanent: a project first seen without a usable cwd
        hint kept `/Users/andrew//claude` forever even after a later transcript
        said otherwise. Now a 'cwd' resolution overwrites the stored path and
        name, and an 'encoded' guess never overwrites anything — the upgrade is
        one-way, so this can never downgrade ground truth back to a guess.
        `encoded_path` remains the unique key and is never derived.
        """
        conn = self.connect()
        name = Path(decoded_path).name
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO projects(encoded_path, decoded_path, project_name,
                                        decoded_from)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (encoded_path) DO UPDATE SET
                     last_seen_at = now(),
                     decoded_path = CASE WHEN EXCLUDED.decoded_from = 'cwd'
                                         THEN EXCLUDED.decoded_path
                                         ELSE projects.decoded_path END,
                     project_name = CASE WHEN EXCLUDED.decoded_from = 'cwd'
                                         THEN EXCLUDED.project_name
                                         ELSE projects.project_name END,
                     decoded_from = CASE WHEN EXCLUDED.decoded_from = 'cwd'
                                         THEN 'cwd'
                                         ELSE projects.decoded_from END
                   RETURNING project_id""",
                (encoded_path, decoded_path, name, decoded_from),
            )
            pid = cur.fetchone()[0]
        conn.commit()
        return pid

    _SESSION_COLS = [
        "session_id", "project_id", "file_path", "is_subagent", "parent_session_id",
        "agent_id", "ai_title", "custom_title", "first_prompt", "last_prompt",
        "last_prompt_leaf_uuid", "permission_mode", "mode", "bridge_session_id",
        "agent_name", "git_branch", "cwd", "cc_version", "entrypoint",
        "created_at", "modified_at", "message_count",
        # --- schema v9 (all nullable; COALESCE semantics keep a later file
        # from wiping a value an earlier one set) ---
        "forked_from_session_id", "forked_from_uuid", "fork_context_length",
        "fork_agent_id",
        "current_cwd", "worktree_session",
        "cost_state", "reported_cost_usd", "reported_total_duration_ms",
        "reported_api_duration_ms", "reported_tool_duration_ms",
        "reported_lines_added", "reported_lines_removed", "has_unknown_model_cost",
        "session_kind",
        # --- schema v10 (last-wins, see _SESSION_LAST_WINS_COLS) ---
        "worktree_active",
    ]

    # Session columns that are JSONB and must be wrapped before being bound.
    _SESSION_JSONB_COLS = {"worktree_session", "cost_state"}

    # Columns whose STATE can legitimately go back to a "negative" value, so
    # first-non-null-wins is wrong for them. `worktree_active` is the case that
    # forced this: leaving a worktree is signalled by `worktreeSession: null`,
    # and under COALESCE-only semantics an exit could never be recorded (see
    # the v10 migration comment). The derivation emits a value ONLY for a file
    # that actually observed a `worktree-state` record, so the write is
    # last-observation-wins rather than blind last-writer-wins: a re-upsert
    # from a source with no such record (the subagent backfill, a second file)
    # leaves the stored state alone instead of erasing it.
    _SESSION_LAST_WINS_COLS = {"worktree_active"}

    def _session_upsert_sql(self) -> str:
        cols = self._SESSION_COLS
        # COALESCE(EXCLUDED.col, sessions.col) so a later file lacking a field
        # doesn't wipe a value an earlier file set. The last-wins columns are
        # written by explicit rule instead: the newly OBSERVED value replaces
        # the stored one, including a `false` that a COALESCE-shaped rule would
        # be indistinguishable from but a future non-boolean column would not.
        updates = ", ".join(
            (f"{c}=CASE WHEN EXCLUDED.{c} IS NOT NULL THEN EXCLUDED.{c} "
             f"ELSE sessions.{c} END"
             if c in self._SESSION_LAST_WINS_COLS
             else f"{c}=COALESCE(EXCLUDED.{c}, sessions.{c})")
            for c in cols if c != "session_id"
        )
        placeholders = ", ".join(["%s"] * len(cols))
        return (f"INSERT INTO sessions ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT (session_id) DO UPDATE SET {updates}")

    def _session_values(self, data: dict) -> list:
        """Bind values for one session row, JSONB columns wrapped."""
        return [_j(data.get(c)) if c in self._SESSION_JSONB_COLS
                else scrub(data.get(c))
                for c in self._SESSION_COLS]

    def upsert_session(self, data: dict) -> None:
        """Insert/update a session row. Only non-None values overwrite existing."""
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(self._session_upsert_sql(), self._session_values(data))
        conn.commit()

    def upsert_sessions(self, rows: list[dict]) -> None:
        """Batched session upsert (same COALESCE semantics as upsert_session).
        One executemany (psycopg3 pipelines it) — used by the subagent backfill
        so ~8K child rows don't cost 8K round-trips."""
        if not rows:
            return
        conn = self.connect()
        sql = self._session_upsert_sql()
        with conn.cursor() as cur:
            cur.executemany(sql, [self._session_values(r) for r in rows])
        conn.commit()

    # -- batched inserts ----------------------------------------------------

    def insert_messages(self, rows: list[dict]) -> None:
        if not rows:
            return
        cols = [
            "uuid", "session_id", "parent_uuid", "ts", "role", "message_type",
            "prompt_text", "prompt_id", "permission_mode", "is_meta", "is_compact_summary",
            "source_tool_assistant_uuid", "source_tool_use_id",
            "model", "api_message_id", "request_id", "stop_reason", "stop_details",
            "is_api_error", "api_error_status", "error_text", "diagnostics",
            "attribution_agent", "attribution_skill", "attribution_mcp_server",
            "attribution_mcp_tool", "attribution_plugin",
            "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
            "ephemeral_5m_tokens", "ephemeral_1h_tokens", "service_tier", "inference_geo",
            "speed", "usage", "is_sidechain", "agent_id", "slug", "cwd", "git_branch",
            "cc_version", "entrypoint", "forked_from", "source_file", "source_line", "raw",
            # schema v9
            "effort", "session_kind", "thinking_tokens", "server_tool_use",
            "iterations", "iteration_count",
        ]
        jsonb_cols = {"stop_details", "diagnostics", "usage", "forked_from", "raw",
                      "server_tool_use", "iterations"}
        self._batch_insert("messages", cols, rows, jsonb_cols,
                           conflict="uuid")

    def insert_content_blocks(self, rows: list[dict]) -> None:
        cols = ["message_uuid", "session_id", "block_index", "block_type", "content",
                "char_count", "signature", "tool_use_id", "tool_name", "tool_input",
                "tool_type", "mcp_server", "source_file", "source_line",
                "block_payload",   # schema v9: verbatim payload of an unknown block
                "caller"]          # schema v10: tool_use.caller, verbatim
        self._batch_insert("content_blocks", cols, rows,
                           {"tool_input", "block_payload", "caller"})

    def insert_tool_results(self, rows: list[dict]) -> None:
        cols = ["message_uuid", "session_id", "tool_use_id", "content_text", "tldr",
                "char_count", "is_error", "error_class", "block_count", "tool_use_result",
                "from_overflow_file", "source_file", "source_line"]
        self._batch_insert("tool_results", cols, rows, {"tool_use_result"})

    def insert_attachments(self, rows: list[dict]) -> None:
        # schema v10: `raw` — the whole record, like every other flow table.
        cols = ["uuid", "session_id", "parent_uuid", "ts", "attachment_type",
                "attachment", "is_sidechain", "source_file", "source_line", "raw"]
        self._batch_insert("attachments", cols, rows, {"attachment", "raw"},
                           conflict="uuid")

    def insert_system_events(self, rows: list[dict]) -> None:
        cols = ["uuid", "session_id", "parent_uuid", "ts", "subtype", "level", "content",
                "duration_ms", "message_count", "url", "compact_trigger",
                "compact_pre_tokens", "logical_parent_uuid", "error_status", "error_type",
                "error_message", "retry_in_ms", "retry_attempt", "max_retries",
                "is_sidechain", "slug", "source_file", "source_line", "raw"]
        self._batch_insert("system_events", cols, rows, {"raw"}, conflict="uuid")

    def insert_queue_operations(self, rows: list[dict]) -> None:
        cols = ["session_id", "ts", "operation", "content", "source_file", "source_line"]
        self._batch_insert("queue_operations", cols, rows, set())

    def insert_pr_links(self, rows: list[dict]) -> None:
        cols = ["session_id", "pr_number", "pr_url", "pr_repository", "ts",
                "source_file", "source_line"]
        self._batch_insert("pr_links", cols, rows, set())

    def insert_agent_tasks(self, rows: list[dict]) -> None:
        cols = ["key", "agent_id", "started", "result", "source_file"]
        self._batch_insert("agent_tasks", cols, rows, {"result"}, conflict="key",
                           conflict_update=["agent_id", "started", "result", "source_file"])

    def insert_session_records(self, rows: list[dict]) -> None:
        """Generic session-scoped records (schema v9). Keyed (source_file,
        source_line) — these records carry no uuid, and the transcript is
        append-only so the line number is stable. DO UPDATE rather than DO
        NOTHING so a --force re-sync refreshes a payload in place."""
        cols = ["session_id", "record_type", "ts", "agent_id", "is_modelled",
                "payload", "source_file", "source_line"]
        self._batch_insert("session_records", cols, rows, {"payload"},
                           conflict="source_file, source_line",
                           conflict_update=["session_id", "record_type", "ts",
                                            "agent_id", "is_modelled", "payload"])

    def get_task_output_mtimes(self, session_id: str) -> dict[str, int]:
        """task_name -> file_mtime_ns already captured for a session (the
        idempotence check for the /private/tmp task-output sweep)."""
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute("SELECT task_name, file_mtime_ns FROM task_outputs "
                        "WHERE session_id = %s", (session_id,))
            rows = cur.fetchall()
        conn.commit()  # release the read snapshot promptly (see query())
        return {r[0]: r[1] for r in rows}

    def upsert_task_output(self, row: dict) -> None:
        """Capture one background-task .output file (verbatim, keyed
        session_id + task filename; latest mtime wins)."""
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO task_outputs
                   (session_id, task_name, content, char_count, truncated,
                    file_size, file_mtime_ns, source_path, captured_at)
                   VALUES (%(session_id)s, %(task_name)s, %(content)s,
                           %(char_count)s, %(truncated)s, %(file_size)s,
                           %(file_mtime_ns)s, %(source_path)s, now())
                   ON CONFLICT (session_id, task_name) DO UPDATE SET
                     content = EXCLUDED.content,
                     char_count = EXCLUDED.char_count,
                     truncated = EXCLUDED.truncated,
                     file_size = EXCLUDED.file_size,
                     file_mtime_ns = EXCLUDED.file_mtime_ns,
                     source_path = EXCLUDED.source_path,
                     captured_at = now()""",
                {k: scrub(v) for k, v in row.items()},
            )
        conn.commit()

    def insert_file_history(self, snapshot_row: dict, backups: list[dict]) -> None:
        """Insert one snapshot + its backups (needs the generated snapshot_id)."""
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO file_history
                   (session_id, message_id, snapshot_message_id, ts, file_count,
                    has_backups, is_snapshot_update, source_file, source_line)
                   VALUES (%(session_id)s, %(message_id)s, %(snapshot_message_id)s, %(ts)s,
                           %(file_count)s, %(has_backups)s, %(is_snapshot_update)s,
                           %(source_file)s, %(source_line)s)
                   RETURNING snapshot_id""",
                snapshot_row,
            )
            sid = cur.fetchone()[0]
            for b in backups:
                b["snapshot_id"] = sid
            if backups:
                cur.executemany(
                    """INSERT INTO file_backups
                       (snapshot_id, file_path, backup_file_name, content_hash, version, backup_time)
                       VALUES (%(snapshot_id)s, %(file_path)s, %(backup_file_name)s,
                               %(content_hash)s, %(version)s, %(backup_time)s)""",
                    backups,
                )
        # caller commits

    def _batch_insert(self, table: str, cols: list[str], rows: list[dict],
                      jsonb_cols: set[str], conflict: Optional[str] = None,
                      conflict_update: Optional[list[str]] = None) -> None:
        if not rows:
            return
        conn = self.connect()
        placeholders = ", ".join(f"%({c})s" for c in cols)
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
        if conflict:
            if conflict_update:
                sets = ", ".join(f"{c}=EXCLUDED.{c}" for c in conflict_update)
                sql += f" ON CONFLICT ({conflict}) DO UPDATE SET {sets}"
            else:
                sql += f" ON CONFLICT ({conflict}) DO NOTHING"
        # Normalize rows: ensure all cols present, wrap JSONB
        norm = []
        for r in rows:
            d = {}
            for c in cols:
                v = r.get(c)
                if c in jsonb_cols:
                    d[c] = _j(v)
                elif isinstance(v, str):
                    d[c] = scrub(v)
                else:
                    d[c] = v
            norm.append(d)
        with conn.cursor() as cur:
            cur.executemany(sql, norm)
        # caller commits

    def commit(self) -> None:
        if self.conn:
            self.conn.commit()

    # -- aggregates ---------------------------------------------------------

    def recompute_session_aggregates(self) -> None:
        """Recompute per-session token/tool/error aggregates from messages.

        All aggregates are gathered in CTEs and applied in a SINGLE UPDATE so each
        session row is rewritten once per sync, not five times. The earlier
        five-pass version rewrote every row repeatedly, bloating the sessions heap
        (~35x) until a VACUUM FULL. Driven off the messages CTE (a session is
        always defined by its messages); siblings LEFT JOIN in, so a session whose
        source rows have gone away is authoritatively reset to 0 rather than left
        stale.

        Subagent semantics (two statements):
        - MAIN sessions: the unprefixed columns keep their historical ROLL-UP
          meaning (children included — sidechain rows share the parent
          session_id); own_* carries main-chain-only counts. Exception:
          user_prompt_count is main-chain-only — counting the ~1 sidechain seed
          prompt per agent as a "user prompt" was a defect that distorted the
          reconcile gate's empty/trivial heuristics.
        - CHILD sessions (session_id "<parent>:<agent>"): never match
          messages.session_id, so the first UPDATE can't touch them; the second
          statement fills them from messages keyed (session_id, agent_id) —
          for a child, total_* == own_*.

        Schema v10: tool_use_count counts DISTINCT tool_use_id and error_count
        counts DISTINCT (message_uuid, tool_use_id), not rows. Blocks and
        results were duplicated across source files before v10 (see
        v_duplicate_blocks); the identity counts are correct either way, and
        the historical rows are left in place rather than deleted.
        """
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH msg AS (
                    SELECT session_id,
                        coalesce(sum(input_tokens),0) AS input_tokens,
                        coalesce(sum(output_tokens),0) AS output_tokens,
                        coalesce(sum(cache_read_tokens),0) AS cache_read_tokens,
                        coalesce(sum(cache_creation_tokens),0) AS cache_creation_tokens,
                        count(*) FILTER (WHERE role='user' AND message_type='prompt'
                                         AND NOT is_meta AND NOT is_sidechain) AS user_prompt_count,
                        count(*) AS message_count,
                        coalesce(sum(input_tokens) FILTER (WHERE NOT is_sidechain),0) AS own_input_tokens,
                        coalesce(sum(output_tokens) FILTER (WHERE NOT is_sidechain),0) AS own_output_tokens,
                        coalesce(sum(cache_read_tokens) FILTER (WHERE NOT is_sidechain),0) AS own_cache_read_tokens,
                        coalesce(sum(cache_creation_tokens) FILTER (WHERE NOT is_sidechain),0) AS own_cache_creation_tokens,
                        count(*) FILTER (WHERE NOT is_sidechain) AS own_message_count
                    FROM messages GROUP BY session_id
                ),
                -- DISTINCT (schema v10): a message present in two transcripts
                -- has ONE messages row but historically got TWO sets of blocks
                -- / results (see v_duplicate_blocks). Counting rows therefore
                -- inflated tool_use_count / error_count. Counting the tool_use
                -- IDENTITY instead is correct with or without the history.
                -- The coalesce keeps a tool_use block with no id countable
                -- (block_id is unique), instead of silently vanishing from
                -- count(DISTINCT).
                tu AS (
                    SELECT cb.session_id,
                           count(DISTINCT coalesce(nullif(cb.tool_use_id, ''),
                                                   'blk:' || cb.block_id)) AS cnt,
                           count(DISTINCT coalesce(nullif(cb.tool_use_id, ''),
                                                   'blk:' || cb.block_id))
                               FILTER (WHERE NOT coalesce(m.is_sidechain, false)) AS own_cnt
                    FROM content_blocks cb
                    LEFT JOIN messages m ON m.uuid = cb.message_uuid
                    WHERE cb.block_type='tool_use' GROUP BY cb.session_id
                ),
                err AS (
                    SELECT tr.session_id,
                           count(DISTINCT (tr.message_uuid, tr.tool_use_id)) AS cnt,
                           count(DISTINCT (tr.message_uuid, tr.tool_use_id))
                               FILTER (WHERE NOT coalesce(m.is_sidechain, false)) AS own_cnt
                    FROM tool_results tr
                    LEFT JOIN messages m ON m.uuid = tr.message_uuid
                    WHERE tr.is_error GROUP BY tr.session_id
                ),
                sysev AS (
                    SELECT session_id,
                        count(*) FILTER (WHERE subtype='compact_boundary') AS compacts,
                        coalesce(sum(duration_ms) FILTER (WHERE subtype='turn_duration'),0)/1000.0 AS duration_s
                    FROM system_events GROUP BY session_id
                ),
                agg AS (
                    SELECT msg.session_id,
                        msg.input_tokens, msg.output_tokens,
                        msg.cache_read_tokens, msg.cache_creation_tokens,
                        msg.user_prompt_count, msg.message_count,
                        msg.own_input_tokens, msg.own_output_tokens,
                        msg.own_cache_read_tokens, msg.own_cache_creation_tokens,
                        msg.own_message_count,
                        coalesce(tu.cnt, 0) AS tool_use_count,
                        coalesce(tu.own_cnt, 0) AS own_tool_use_count,
                        coalesce(err.cnt, 0) AS error_count,
                        coalesce(err.own_cnt, 0) AS own_error_count,
                        coalesce(sysev.compacts, 0) AS compact_count,
                        sysev.duration_s AS duration_seconds  -- NULL when no turn_duration events (unknown != 0)
                    FROM msg
                    LEFT JOIN tu ON tu.session_id = msg.session_id
                    LEFT JOIN err ON err.session_id = msg.session_id
                    LEFT JOIN sysev ON sysev.session_id = msg.session_id
                )
                UPDATE sessions s SET
                    total_input_tokens = agg.input_tokens,
                    total_output_tokens = agg.output_tokens,
                    total_cache_read_tokens = agg.cache_read_tokens,
                    total_cache_creation_tokens = agg.cache_creation_tokens,
                    user_prompt_count = agg.user_prompt_count,
                    message_count = agg.message_count,
                    tool_use_count = agg.tool_use_count,
                    error_count = agg.error_count,
                    compact_count = agg.compact_count,
                    duration_seconds = agg.duration_seconds,
                    own_total_input_tokens = agg.own_input_tokens,
                    own_total_output_tokens = agg.own_output_tokens,
                    own_total_cache_read_tokens = agg.own_cache_read_tokens,
                    own_total_cache_creation_tokens = agg.own_cache_creation_tokens,
                    own_message_count = agg.own_message_count,
                    own_tool_use_count = agg.own_tool_use_count,
                    own_error_count = agg.own_error_count
                FROM agg
                WHERE s.session_id = agg.session_id
                """
            )
            cur.execute(
                """
                WITH cm AS (
                    SELECT session_id AS parent, agent_id,
                        coalesce(sum(input_tokens),0) AS input_tokens,
                        coalesce(sum(output_tokens),0) AS output_tokens,
                        coalesce(sum(cache_read_tokens),0) AS cache_read_tokens,
                        coalesce(sum(cache_creation_tokens),0) AS cache_creation_tokens,
                        count(*) FILTER (WHERE role='user' AND message_type='prompt'
                                         AND NOT is_meta) AS user_prompt_count,
                        count(*) AS message_count
                    FROM messages
                    WHERE agent_id IS NOT NULL
                    GROUP BY 1, 2
                ),
                ct AS (
                    -- DISTINCT for the same reason as `tu` above.
                    SELECT m.session_id AS parent, m.agent_id,
                           count(DISTINCT coalesce(nullif(cb.tool_use_id, ''),
                                                   'blk:' || cb.block_id)) AS cnt
                    FROM content_blocks cb
                    JOIN messages m ON m.uuid = cb.message_uuid
                    WHERE cb.block_type='tool_use' AND m.agent_id IS NOT NULL
                    GROUP BY 1, 2
                ),
                ce AS (
                    SELECT m.session_id AS parent, m.agent_id,
                           count(DISTINCT (tr.message_uuid, tr.tool_use_id)) AS cnt
                    FROM tool_results tr
                    JOIN messages m ON m.uuid = tr.message_uuid
                    WHERE tr.is_error AND m.agent_id IS NOT NULL
                    GROUP BY 1, 2
                )
                UPDATE sessions s SET
                    total_input_tokens = cm.input_tokens,
                    total_output_tokens = cm.output_tokens,
                    total_cache_read_tokens = cm.cache_read_tokens,
                    total_cache_creation_tokens = cm.cache_creation_tokens,
                    user_prompt_count = cm.user_prompt_count,
                    message_count = cm.message_count,
                    tool_use_count = coalesce(ct.cnt, 0),
                    error_count = coalesce(ce.cnt, 0),
                    own_total_input_tokens = cm.input_tokens,
                    own_total_output_tokens = cm.output_tokens,
                    own_total_cache_read_tokens = cm.cache_read_tokens,
                    own_total_cache_creation_tokens = cm.cache_creation_tokens,
                    own_message_count = cm.message_count,
                    own_tool_use_count = coalesce(ct.cnt, 0),
                    own_error_count = coalesce(ce.cnt, 0)
                FROM cm
                LEFT JOIN ct ON ct.parent = cm.parent AND ct.agent_id = cm.agent_id
                LEFT JOIN ce ON ce.parent = cm.parent AND ce.agent_id = cm.agent_id
                WHERE s.is_subagent
                  AND s.parent_session_id = cm.parent AND s.agent_id = cm.agent_id
                """
            )
        conn.commit()

    # -- queries ------------------------------------------------------------

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        conn = self.connect()
        with conn.cursor() as cur:
            # Bound this read so a pathological query errors fast instead of hanging.
            cur.execute("SELECT set_config('statement_timeout', %s, true)",
                        (str(ANALYTIC_TIMEOUT_MS),))
            cur.execute(sql, params)
            if cur.description is None:
                conn.commit()
                return []
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        # autocommit=False: a bare SELECT still opens a transaction that holds its
        # snapshot (and any locks) until commit/rollback. Close it immediately so
        # the sweep never sits `idle in transaction` between phases — the exact
        # state that convoyed the DB. See lesson
        # csd-sweep-idle-in-transaction-lock-convoy.
        conn.commit()
        return rows

    def session_record_census(self) -> list[dict]:
        """`session_records` broken down by type — the standing "what record
        types is Claude Code emitting that csd does not model" query.

        Returns [{record_type, is_modelled, n, last_seen}] newest-heaviest
        first. Never raises: on a pre-v9 archive (no table yet) it returns [],
        so callers can print it unconditionally.
        """
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('statement_timeout', %s, true)",
                            (str(ANALYTIC_TIMEOUT_MS),))
                cur.execute("SELECT to_regclass('public.session_records')")
                if cur.fetchone()[0] is None:
                    conn.commit()
                    return []
                cur.execute(
                    """SELECT record_type, is_modelled, count(*) AS n, max(ts) AS last_seen
                       FROM session_records
                       GROUP BY 1, 2
                       ORDER BY is_modelled, n DESC"""
                )
                cols = [d.name for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            conn.commit()
            return rows
        except psycopg.Error:
            conn.rollback()
            return []

    def statistics(self, exact: bool = False) -> dict:
        """Per-table row counts + database size.

        Default uses pg_class.reltuples catalog estimates (O(1), kept current by
        autovacuum/ANALYZE — within ~0.5% here) instead of exact count(*), which
        full-scans every table and degrades badly as messages/content_blocks/
        tool_results grow into the millions. Pass exact=True for precise counts.
        """
        tables = ["projects", "sessions", "messages", "content_blocks", "tool_results",
                  "attachments", "system_events", "file_history", "file_backups",
                  "queue_operations", "pr_links", "agent_tasks", "task_outputs",
                  "session_records", "sync_state"]
        stats: dict[str, Any] = {}
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('statement_timeout', %s, true)",
                        (str(ANALYTIC_TIMEOUT_MS),))
            if exact:
                for t in tables:
                    cur.execute(f"SELECT count(*) FROM {t}")
                    stats[t] = cur.fetchone()[0]
            else:
                cur.execute(
                    """
                    SELECT c.relname, c.reltuples::bigint
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = current_schema()
                      AND c.relname = ANY(%s)
                    """,
                    (tables,),
                )
                est = {r[0]: int(r[1]) for r in cur.fetchall()}
                # reltuples is -1 for a table that has never been analyzed; clamp to 0.
                for t in tables:
                    stats[t] = max(est.get(t, 0), 0)
            cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
            stats["db_size"] = cur.fetchone()[0]
        # Release the read snapshot promptly (see query() rationale).
        conn.commit()
        return stats
