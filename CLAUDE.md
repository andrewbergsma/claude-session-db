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
```

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
