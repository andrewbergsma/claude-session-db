# CLAUDE.md — claude-session-db

## Overview

`csd` is the front-end for the **lossless Postgres archive** of Claude Code
session transcripts. It parses session JSONL (`~/.claude/projects/**/*.jsonl`,
main + subagent sidechains) and writes straight into a `claude_sessions`
Postgres database — a telemetry sibling of the `knowledge` DB on the same
Postgres host (NEVER the knowledge tables).

Design spec: `claudecode:knowledge:design/claude-session-db-postgres-archive`.
Data model: `DATA_MODEL.md` (re-audited 2026-06-01 against live JSONL).

**Database**: `claude_sessions` on the Postgres host (pg16).
**Connection**: configured via env (`.env` or shell). DSN auto-derived from
`$DATABASE_URL` (swap db name → `claude_sessions`), or set `$CSD_DATABASE_URL`.
See `.env.example`.
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
csd reconcile-summaries # Pre-LLM gate: classify summarized/not_required/pending
csd unsummarized        # List the pending phase-4 work queue (newest first)
csd summarize           # Phase-4 roll-up: digest -> local Ollama -> kmcp entry (guarded)
csd summarize-health    # Watcher for the summarize launchd timer (DB-free)
csd mark-summarized     # Stamp a session's watermark after a verified kmcp write
csd angles              # Pull-based turn mining: ID-addressable headlines for one turn
csd angles show ID      # Print the persisted detail behind a headline
csd usage               # Dual-account Claude Max quota report (live, all vaulted accounts)
csd usage add-account   # Vault the currently logged-in account (run once per account)
csd usage use LABEL     # Switch the active account (replaces the interactive /login swap)
csd usage list          # List vaulted accounts (no network)
```

## Dual-account usage (`csd usage`)

Reports live Claude Max quota for both Max accounts from the same OAuth endpoints
Claude Code's own `/usage` uses — refresh at `platform.claude.com/v1/oauth/token`
(Claude Code public `client_id`), quota at `api.anthropic.com/api/oauth/usage`,
identity at `.../api/oauth/profile` (`anthropic-beta: oauth-2025-04-20`). The
refresh response self-labels each account (email + org), so no extra lookup names
them.

**One-account-at-a-time constraint.** Only the logged-in account is authenticated
(macOS keychain `Claude Code-credentials` is authoritative; `~/.claude/.credentials.json`
is a mirror). To poll *both*, each account's refresh token is vaulted (0600) at
`$CSD_STATE_DIR/usage-accounts.json`. Anthropic **rotates the refresh token on
every use**, so the vault is rewritten after each refresh and the active
account's rotated creds are written back to keychain+file (preserving `mcpOAuth`)
so the two stores never desync. `csd usage use LABEL` performs a rotation-safe
account swap in place of the interactive `/login`. Bootstrap: log into each
account and run `csd usage add-account` once.

Local per-account token/cost is **not attributable** (transcripts carry no
account identity), so the reported cost is a commingled all-accounts aggregate
from `v_token_cost_daily`.

## Phase-4 roll-up (`csd summarize`)

Automated off-session summarization of the reconcile gate's PENDING queue —
per session: `session_digest.render(--full-inputs)` → local Ollama
(`CSD_SUMMARIZE_MODEL`, default `gemma4:26b-mlx`, `think:false`) → kmcp
`session` entry via `knowledge-cli` in local-trusted mode
(`KNOWLEDGE_ALLOW_UNAUTH_LOCAL=1`) → read-back verify → `mark_summarized`
watermark. Never `claude --resume`, never raw-transcript replay (the two
historically failing paths). Auto entries carry the `auto-summary` tag; the
target application is inferred deterministically from the session cwd
(`APP_ALIASES` + live app check, fallback `CSD_SUMMARIZE_DEFAULT_APP`).

Reliability mirrors the sweep: `summarize.lock` liveness guard +
`summarize.heartbeat` (`csd summarize-health`), per-session failure isolation
with a `summarize_attempts` backoff ledger (`MAX_ATTEMPTS`, 6h backoff), and a
quiesce gate (`--min-idle`, default 900s) so live sessions are never digested
mid-flight. Launchd timer: `launchd/com.claude-session-db.summarize.plist`
(every 30 min, default 2 sessions/tick — the ~700-session backlog drains
gradually; `csd summarize -n 20` is the manual backfill lever).

## Turn angles (`csd angles`) — pull-based per-turn mining

P1 spike of `claudecode:design/turn-angles-context-cockpit`. The operator fires
`! csd angles` right after an agent response lands; the latest turn is read
straight from the live session JSONL (no DB round-trip) and mined by ANGLES:
deterministic extractors (files F, commands X, git G, kmcp writes W, errors R,
metrics M — pure code, instant) plus small-model probes (direction D, events E
on `CSD_ANGLES_MODEL`, default qwen2.5vl:7b) and retrieval (knowledge K via
hybrid_search). Output is one-line ID-addressable headlines (~1-2K tokens);
detail persists under the state dir (`csd angles show F1`). Curation is the
operator's next message ("track E1, load K1, task D1") — nothing is written to
kmcp by the command itself. Doctrine: pull not push; extraction is code, models
only judge. A failed probe degrades to `(unavailable)`, never blocks the pull.

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
