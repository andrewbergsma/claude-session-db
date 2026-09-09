# CLAUDE.md — claude-session-db

`csd` is the front-end for the **lossless Postgres archive** of Claude Code
session transcripts. It parses session JSONL (`~/.claude/projects/**/*.jsonl`,
main + subagent sidechains) into the `claude_sessions` database — a telemetry
sibling of the `knowledge` DB on the same Postgres host (NEVER the knowledge
tables). It also serves the web console, the turn-angles miner and the
summarization pipeline.

**Connection**: env (`.env` or shell). DSN auto-derived from `$DATABASE_URL`
(swap db name → `claude_sessions`), or set `$CSD_DATABASE_URL`. See
`.env.example`. **CLI**: `csd`.

## Read before acting

| Question | Authority |
|---|---|
| Schema — tables, columns, views, payloads, migrations | [`DATA_MODEL.md`](DATA_MODEL.md) |
| Console, angles, CR, repos lens, permission envelope, curation | [`docs/CONSOLE.md`](docs/CONSOLE.md) |
| Summarize, sweep, session-mgmt lens, subagents, `csd usage` | [`docs/OPERATIONS.md`](docs/OPERATIONS.md) |
| What shipped when | [`CHANGELOG.md`](CHANGELOG.md) |
| Design spec | `claude_session_db:design/claude-session-db-postgres-archive` |
| CLI reference | `claude_session_db:command/csd` |
| Corpus curation | `claude_session_db:agent/steward` |

## Commands

```bash
csd ingest [--rebuild|--force]  # mtime-based incremental sync
csd stats | recent [N] | views | dsn | open
csd query "SQL" [--csv]  # psycopg execute(): literal % breaks — use
                         #   starts_with()/strpos(), or escape as %%
csd sweep | sweep-health           # launchd ingest tick + DB-free watcher
csd summarize | summarize-health   # phase-4 roll-up + its watcher
csd reconcile-summaries | unsummarized | mark-summarized
csd digest REF [--since TS]        # THE digest, worktree-aware, DB-free
csd summary-scope REF [--json]     # scope = full | delta | none
csd angles [show ID|sessions|digest REF]  # per-turn mining
csd angles-watch                   # headless miner (console hosts one already)
csd console                        # THE web UI — 127.0.0.1:4462
csd backfill-subagents
csd usage [add-account|use LABEL|list]
```

## Versioning

Semver, one source of truth: `claude_session_db/__init__.py:__version__`.
pyproject builds from it (hatch `dynamic`), `csd --version` prints it,
`version.py` serves it. **Bump the version and add its `CHANGELOG.md` entry in
the same commit as the change** — minor for a feature batch, patch for
fixes/perf/docs, major for an archive generation or a breaking CLI/schema
change. The major tracks the archive generation (3.x = the Postgres Gen3 era).

The console shows a version chip (click → changelog overlay) and `GET
/api/version` compares the identity **captured at server start** against the
repo's HEAD on disk. An amber dot means *restart to update* — a
launchd-respawned console keeps executing the code it booted with, which has
silently shipped stale behaviour more than once.

## Architecture

- `jsonl_records.py` — JSONL record parsing (dataclasses, stdlib-only). Every
  record keeps its `raw` dict for the JSONB escape-hatch.
- `subagent.py` — subagent + tool-results overflow discovery.
- `postgres.py` — `SessionArchive`: schema DDL, JSONB escape-hatch columns,
  batched upserts (idempotent by uuid / per-source_file clear), analytic views.
- `sync.py` — `SessionSync`: glob+mtime incremental sync engine.
- `summarize.py` — phase-4 roll-up **and the one grader**
  (`resolve_summary_scope` / `prior_capture` / `_delta_gate`): the console
  button, the launchd timer and `csd summary-scope` all call it, so they can
  never disagree about what a pass covers. Never raises; an unreachable archive
  degrades to `pass 1 / full` with the reason printed, exit 0.
- `angles.py` — turn extraction + the ANGLES extractors/probes; the state dir.
- `angles_watch.py` — headless ambient miner (settle-detect + single worker).
- `console/` — the reply-capable session console (stdlib HTTP, `server.py` +
  `index.html`) and the only web UI; reads transcripts and the angles state dir.
- `cr.py` — context reduction: honest token accounting, curated forks.
- `cli.py` — Click CLI.
- `scripts/audit_jsonl.py` — Phase-0 field-frequency re-audit (regenerates
  DATA_MODEL.md).

## Schema

**Read [`DATA_MODEL.md`](DATA_MODEL.md) before touching the schema.** It is the
authority for every table, column, view, payload shape and migration.
`SCHEMA_VERSION` lives in `postgres.py`; current version **10** (3.24.0),
recorded in `metadata` and re-applied idempotently by `initialize()`.

- **`SCHEMA_VERSION` is not the table inventory.** It gates view recreation and
  `postgres.BACKFILLS` — but `CREATE TABLE IF NOT EXISTS` runs unconditionally,
  so a table can arrive without the version moving (`summarize_attempts` and
  `task_outputs` both did). Read the catalog, not the marker.
- **Arrows are not foreign keys.** Exactly **four** FKs exist:
  `sessions.project_id`, `file_backups.snapshot_id`, `summary_state.session_id`,
  `summary_passes.session_id`. Every other `→` in the docs is a logical
  reference ingest order keeps consistent, not a constraint.
- **Migrations are additive, idempotent and guarded.** No column dropped or
  retyped, no row deleted. Column additions sit behind an `information_schema`
  guard so the ACCESS EXCLUSIVE `ALTER` fires once, not every sweep tick. Data
  backfills go through `postgres.BACKFILLS` / `run_backfills`: bounded (20s per
  `initialize()`), resumable from a cursor, `IS DISTINCT FROM`-guarded,
  failure-isolated. A single long `UPDATE` is the exact `idle in transaction`
  shape that once convoyed this database for ~9h.

## Key invariants

- **No truncation.** Content blocks and tool results are stored verbatim; the
  largest results come from `tool-results/*.txt` **and `*.json`** overflow
  files. `tldr` is a nullable derived sibling, never a replacement.
- **The never-drop convention.** Claude Code adds session-scoped record types
  without warning (ten arrived between v2.1.161 and v2.1.258 and were silently
  dropped). A new record type has exactly two legitimate destinations: a
  **modelled route** (dedicated table or mapped column, `is_modelled = true`),
  or **`session_records`**, the catch-all, stored verbatim with
  `is_modelled = false`. There is no third option — dropping a record, a
  content block or a field is a defect, not a design choice. When you promote a
  type to a modelled route, keep the `session_records` row.
- **The UNMODELLED tripwire** makes the catch-all visible instead of silent:
  `SyncStats.unknown_types` prints on the sync summary, rides the sweep
  heartbeat for the DB-free `csd sweep-health`, and `csd stats` prints the
  whole-archive census. It is a **signal, never a failure** — it must not set
  `ok=false` or change an exit code.
- **JSONB escape-hatch** columns (`raw`, `usage`, `tool_input`,
  `tool_use_result`, `attachment`, `stop_details`, `diagnostics`, `payload`,
  `block_payload`, `cost_state`, `worktree_session`) absorb JSONL field drift
  without a migration — **only on the tables that have one**. Just `messages`,
  `system_events` and `session_records` keep the WHOLE record; a field added to
  `queue_operations` / `pr_links` / `file_history` / `agent_tasks` is dropped,
  not hidden.
- **Full usage** is captured per assistant message (input + output + cache_read
  + cache_creation + ephemeral) plus the raw `usage` JSONB — the token-economics
  goldmine. See `v_token_by_attribution`.
- **Sync signal is `*.jsonl` mtime** (`st_mtime_ns`), NOT sessions-index.json
  (which covers <25% of projects and is stale). Likewise the session-mgmt lens
  uses `max(messages.ts)` for true last activity — mtime only ever lies toward
  "more recent".
- Transcripts are **telemetry**, not knowledge entries — separate DB, cross-link
  only via `session_id`.
- **Nothing serves what it mines.** The angles miner, the console and the CLI
  meet at the state dir (`$CSD_STATE_DIR`), never in-process.
- **Source is never mutated.** Archive, fork and CR all write new files or index
  entries; `~/.claude/projects` is read-only to us.
- **Degrade, never block.** Every lens, grader and envelope resolver returns a
  stated reason on failure (`unknown`, zero flags, full scope) rather than
  raising — an unreachable archive costs its own half of a payload, not a 500.

## Sweep recovery — "queries hang but the DB is reachable" (lock convoy)

```sql
-- 1. Root = the row whose pg_blocking_pids is EMPTY and which is idle in transaction.
SELECT pid, state, pg_blocking_pids(pid),
       now() - xact_start AS txn_age, left(query, 60) AS query
FROM pg_stat_activity WHERE state <> 'idle' ORDER BY xact_start;
-- 2. Terminate ONLY the root; the convoy drains in dependency order.
SELECT pg_terminate_backend(<root_pid>);
```

`csd sweep-health` flags the stale heartbeat; the next tick's guard reclaims the
lock. Full hardening detail in [`docs/OPERATIONS.md`](docs/OPERATIONS.md);
lessons `claudecode:lesson/csd-sweep-idle-in-transaction-lock-convoy` and
`claudecode:lesson/launchd-per-label-hang-silent-starvation`.

## Retired

`database.py` (SQLite) and `sessions_index.py` are superseded by `postgres.py`
and the glob sync; the SQLite/VisiData analyst surface is retired. So is
`angles_web.py` / `csd angles-serve` (2026-07-17) — the console is the single
web surface.
