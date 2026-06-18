# CLAUDE.md — claude-session-db

## Overview

`csd` is the front-end for the **lossless Postgres archive** of Claude Code
session transcripts. It parses session JSONL (`~/.claude/projects/**/*.jsonl`,
main + subagent sidechains) and writes straight into a `claude_sessions`
Postgres database — a telemetry sibling of the `knowledge` DB on
db-host (NEVER the knowledge tables).

Design spec: `claudecode:knowledge:design/claude-session-db-postgres-archive`.
Data model: `DATA_MODEL.md` (re-audited 2026-06-01 against live JSONL).

**Database**: `claude_sessions` on `DB_HOST` (db-host, pg16).
**Connection**: DSN auto-derived from `$DATABASE_URL` (swap db name →
`claude_sessions`), or set `$CSD_DATABASE_URL`.
**CLI**: `csd`

## Commands

```bash
csd ingest              # Incremental sync (mtime-based, glob over *.jsonl)
csd ingest --rebuild    # DROP SCHEMA + rebuild from scratch
csd ingest --force      # Re-sync all files regardless of mtime
csd stats               # Table row counts + db size
csd recent [N]          # Most recent sessions
csd query "SQL"         # Ad-hoc SQL (--csv for CSV)
csd views               # List analytic views
csd dsn                 # Print connection target (password redacted)
csd open                # Interactive shell (pgcli/psql)
csd sweep               # Launchd-timed: ingest + live observability head (guarded)
csd sweep-health        # Watcher: heartbeat age / last outcome / held lock (DB-free)
```

## Sweep reliability & recovery

The `csd sweep` launchd agent (`com.claude-session-db.sweep`, every 300s) is
hardened against the failure mode that once silently starved the schedule for
~9h and convoyed the whole DB:

- **Liveness guard** (`sweepguard.py`): a PID+age pidfile under
  `~/.local/state/claude-session-db/`. A new sweep self-aborts only while a prior
  run is *live AND fresh*; a stale lock (dead PID, or alive but older than
  `CSD_SWEEP_MAX_AGE_S`, default 900s) is **reclaimed** so a wedged predecessor
  can never become a permanent block. launchd's per-label serialization prevents
  overlap but converts a hang into silent starvation — this restores fail-fast.
- **Heartbeat / error detection**: every sweep writes `sweep.heartbeat`
  (`{ts, ok, detail}`). `csd sweep-health` reports staleness (heartbeat older
  than `STALE_INTERVALS` × 300s) and last outcome; exit 0=ok, 1=stale/errored,
  2=never-ran. It is DB-free, so it still works when the archive is wedged.
- **Transaction lifetime**: the archive connection sets
  `idle_in_transaction_session_timeout` (`IDLE_TXN_TIMEOUT_MS`, 5 min) so Postgres
  reaps an abandoned txn; reads (`query`, `statistics`) commit immediately so the
  sweep never sits `idle in transaction` between phases.
- **DDL off the hot path**: `CREATE OR REPLACE VIEW` (ACCESS EXCLUSIVE) runs only
  on a `views_version` ≠ `SCHEMA_VERSION` mismatch (a migration), never every
  tick — see `initialize()`.

**Recovery recipe — "queries hang but the DB is reachable" (lock convoy):**

```sql
-- 1. Find the root. The row whose pg_blocking_pids is EMPTY {} and which is
--    `idle in transaction` is the holder; everything else is a waiter behind it.
SELECT pid, state, pg_blocking_pids(pid),
       now() - xact_start AS txn_age, left(query, 60) AS query
FROM pg_stat_activity
WHERE state <> 'idle'
ORDER BY xact_start;

-- 2. Terminate ONLY the root (never the innocent waiters); the convoy drains
--    automatically in dependency order.
SELECT pg_terminate_backend(<root_pid>);
```

If the launchd job looks "running" but is wedged: `csd sweep-health` flags the
stale heartbeat; clear the stuck process and the next tick's guard reclaims the
lock. See lessons `claudecode:lesson/csd-sweep-idle-in-transaction-lock-convoy`
and `claudecode:lesson/launchd-per-label-hang-silent-starvation`.

## Architecture

- `jsonl_records.py` — JSONL record parsing (dataclasses, stdlib-only). Every
  record keeps its `raw` dict for the JSONB escape-hatch.
- `subagent.py` — subagent + tool-results overflow discovery.
- `postgres.py` — `SessionArchive`: schema DDL, JSONB escape-hatch columns,
  batched upserts (idempotent by uuid / per-source_file clear), analytic views.
- `sync.py` — `SessionSync`: glob+mtime incremental sync engine.
- `cli.py` — Click CLI.
- `scripts/audit_jsonl.py` — Phase-0 field-frequency re-audit (regenerates DATA_MODEL.md).

## Key invariants

- **No truncation.** Content blocks and tool results are stored verbatim; the
  largest results are pulled from `tool-results/*.txt` overflow files. `tldr` is
  a nullable derived sibling, never a replacement.
- **Full usage** is captured per assistant message (input + output + cache_read +
  cache_creation + ephemeral), plus the raw `usage` JSONB — the token-economics
  goldmine. See `v_token_by_attribution` for per-skill/mcp/agent absorption.
- **Sync signal is `*.jsonl` mtime** (`st_mtime_ns`), NOT sessions-index.json
  (which covers <25% of projects and is stale).
- **JSONB escape-hatch** columns (`raw`, `usage`, `tool_input`,
  `tool_use_result`, `attachment`, `stop_details`, `diagnostics`) absorb JSONL
  field drift without a migration.
- Transcripts are **telemetry**, not knowledge entries — kept in a separate DB;
  cross-link only via `session_id`.

## Retired (Gen2 SQLite era)

`database.py` (SQLite) and `sessions_index.py` are superseded by `postgres.py`
and the glob sync. The SQLite/VisiData analyst surface is retired.
