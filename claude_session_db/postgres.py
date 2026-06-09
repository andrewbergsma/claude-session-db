"""Postgres archive storage layer for Claude Code session data.

The LOSSLESS ARCHIVE PLANE: parses Claude Code JSONL and writes straight into a
`claude_sessions` Postgres database (a telemetry sibling of the knowledge DB on
db-host — NEVER the knowledge tables).

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
SCHEMA_VERSION = 5  # + summary_state pre-LLM gate table & v_unsummarized view

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
         the same db-host instance)
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
        "the db-host instance)."
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
    forked_from  JSONB,

    source_file TEXT NOT NULL,
    source_line INTEGER,
    raw         JSONB
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_role ON messages(role);
CREATE INDEX IF NOT EXISTS idx_messages_model ON messages(model);
CREATE INDEX IF NOT EXISTS idx_messages_source_file ON messages(source_file);
CREATE INDEX IF NOT EXISTS idx_messages_attr_skill ON messages(attribution_skill);
CREATE INDEX IF NOT EXISTS idx_messages_attr_mcp ON messages(attribution_mcp_server);
CREATE INDEX IF NOT EXISTS idx_messages_src_tool_asst ON messages(source_tool_assistant_uuid);

CREATE TABLE IF NOT EXISTS content_blocks (
    block_id    BIGSERIAL PRIMARY KEY,
    message_uuid TEXT NOT NULL,
    session_id  TEXT,
    block_index INTEGER NOT NULL,
    block_type  TEXT NOT NULL,           -- thinking | text | tool_use
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
    reason      TEXT CHECK (reason IN ('empty', 'meta_run', 'trivial', 'grown')),
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
"""


VIEWS_SQL = """
-- Session overview
CREATE OR REPLACE VIEW v_session_overview AS
SELECT s.session_id, p.project_name, p.decoded_path AS project_path,
       COALESCE(s.custom_title, s.ai_title) AS title,
       s.first_prompt, s.is_subagent, s.agent_name,
       s.created_at, s.modified_at, s.git_branch, s.message_count,
       s.total_input_tokens, s.total_output_tokens,
       s.total_cache_read_tokens, s.total_cache_creation_tokens,
       s.user_prompt_count, s.tool_use_count, s.error_count, s.compact_count,
       s.duration_seconds, s.cc_version
FROM sessions s
LEFT JOIN projects p ON s.project_id = p.project_id
ORDER BY s.modified_at DESC NULLS LAST;

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
LEFT JOIN sessions s ON p.project_id = s.project_id
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

    def initialize(self) -> None:
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

    # -- projects / sessions ------------------------------------------------

    def get_or_create_project(self, encoded_path: str, decoded_path: str) -> int:
        conn = self.connect()
        name = Path(decoded_path).name
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO projects(encoded_path, decoded_path, project_name)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (encoded_path) DO UPDATE SET last_seen_at = now()
                   RETURNING project_id""",
                (encoded_path, decoded_path, name),
            )
            pid = cur.fetchone()[0]
        conn.commit()
        return pid

    def upsert_session(self, data: dict) -> None:
        """Insert/update a session row. Only non-None values overwrite existing."""
        conn = self.connect()
        cols = [
            "session_id", "project_id", "file_path", "is_subagent", "parent_session_id",
            "agent_id", "ai_title", "custom_title", "first_prompt", "last_prompt",
            "last_prompt_leaf_uuid", "permission_mode", "mode", "bridge_session_id",
            "agent_name", "git_branch", "cwd", "cc_version", "entrypoint",
            "created_at", "modified_at", "message_count",
        ]
        vals = [scrub(data.get(c)) for c in cols]
        # COALESCE(EXCLUDED.col, sessions.col) so a later file lacking a field
        # doesn't wipe a value an earlier file set.
        updates = ", ".join(
            f"{c}=COALESCE(EXCLUDED.{c}, sessions.{c})" for c in cols if c != "session_id"
        )
        placeholders = ", ".join(["%s"] * len(cols))
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO sessions ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT (session_id) DO UPDATE SET {updates}",
                vals,
            )
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
        ]
        jsonb_cols = {"stop_details", "diagnostics", "usage", "forked_from", "raw"}
        self._batch_insert("messages", cols, rows, jsonb_cols,
                           conflict="uuid")

    def insert_content_blocks(self, rows: list[dict]) -> None:
        cols = ["message_uuid", "session_id", "block_index", "block_type", "content",
                "char_count", "signature", "tool_use_id", "tool_name", "tool_input",
                "tool_type", "mcp_server", "source_file", "source_line"]
        self._batch_insert("content_blocks", cols, rows, {"tool_input"})

    def insert_tool_results(self, rows: list[dict]) -> None:
        cols = ["message_uuid", "session_id", "tool_use_id", "content_text", "tldr",
                "char_count", "is_error", "error_class", "block_count", "tool_use_result",
                "from_overflow_file", "source_file", "source_line"]
        self._batch_insert("tool_results", cols, rows, {"tool_use_result"})

    def insert_attachments(self, rows: list[dict]) -> None:
        cols = ["uuid", "session_id", "parent_uuid", "ts", "attachment_type",
                "attachment", "is_sidechain", "source_file", "source_line"]
        self._batch_insert("attachments", cols, rows, {"attachment"}, conflict="uuid")

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
                        count(*) FILTER (WHERE role='user' AND message_type='prompt' AND NOT is_meta) AS user_prompt_count,
                        count(*) AS message_count
                    FROM messages GROUP BY session_id
                ),
                tu AS (
                    SELECT session_id, count(*) AS cnt FROM content_blocks
                    WHERE block_type='tool_use' GROUP BY session_id
                ),
                err AS (
                    SELECT session_id, count(*) AS cnt FROM tool_results
                    WHERE is_error GROUP BY session_id
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
                        coalesce(tu.cnt, 0) AS tool_use_count,
                        coalesce(err.cnt, 0) AS error_count,
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
                    duration_seconds = agg.duration_seconds
                FROM agg
                WHERE s.session_id = agg.session_id
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
        return rows

    def statistics(self, exact: bool = False) -> dict:
        """Per-table row counts + database size.

        Default uses pg_class.reltuples catalog estimates (O(1), kept current by
        autovacuum/ANALYZE — within ~0.5% here) instead of exact count(*), which
        full-scans every table and degrades badly as messages/content_blocks/
        tool_results grow into the millions. Pass exact=True for precise counts.
        """
        tables = ["projects", "sessions", "messages", "content_blocks", "tool_results",
                  "attachments", "system_events", "file_history", "file_backups",
                  "queue_operations", "pr_links", "agent_tasks", "sync_state"]
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
        return stats
