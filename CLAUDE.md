# CLAUDE.md — claude-session-db

## Overview

`csd` is the front-end for the **lossless Postgres archive** of Claude Code
session transcripts. It parses session JSONL (`~/.claude/projects/**/*.jsonl`,
main + subagent sidechains) and writes straight into a `claude_sessions`
Postgres database — a telemetry sibling of the `knowledge` DB on the same
Postgres host (NEVER the knowledge tables).

Design spec: `claude_session_db:design/claude-session-db-postgres-archive`
(the csd knowledge corpus lives in its own `claude_session_db` kmcp app —
CLI reference `claude_session_db:command/csd`, curated by
`claude_session_db:agent/steward`).
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
csd angles sessions     # Session-management lens: open-thread inventory + delta verdicts
csd angles digest REF   # Per-session digest (--delta = post-summary tail; --head/--tail/--full)
csd angles-watch        # Headless miner: keep the angles state dir warm (serves nothing)
csd console             # THE web UI: reply-capable session console (127.0.0.1:4462; token auth on LAN binds)
csd backfill-subagents  # One-shot: child session rows for already-ingested sidechains
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

### One engine, one surface

Three commands, one seam — **the state dir**. Nothing serves what it mines.

- `csd angles` — the operator's synchronous pull for one turn.
- `csd angles-watch` (`angles_watch.py`) — the same miner, headless and
  ambient. Watches every live transcript; mines a session's latest turn once
  its JSONL **settles** (`(mtime_ns, size)` signature unchanged and quiet for
  `DEBOUNCE_S`), so a turn is never mined mid-write. A **single worker** drains
  the job queue, so N live sessions cannot stampede the local Ollama. It writes
  to `$CSD_STATE_DIR/angles/<sid>.json` and serves nothing.
- `csd console` (`console/`) — the reply-capable surface, and the only web UI.

The console is **Direction A**: it renders a session's own transcript (chat
turns, kmcp reads joined to their `tool_result` by `tool_use_id`) plus the angle
headlines it reads *off disk*. Answer resumes the session (`claude -p --resume`,
refused if the session wrote within 15s — the two-writer guard); Fork branches
it, and a point fork writes a **new** session file rather than mutating the
original.

### Console actions

- **Stop** — SIGINT → SIGTERM → SIGKILL to the process group of a run *the
  console spawned*. It cannot reach anything else, and the button is disabled
  with that reason. Claude Code opens a transcript, appends, and closes (no
  process holds it open), and an interactive `claude` carries no session id in
  argv — so an arbitrary live session **cannot be mapped to a pid**.
  `claude -p --resume` never attaches to a running session either; it spawns a
  new process that appends to the same file. That is what the two-writer guard
  is guarding.
- **Archive** — an index entry in `$CSD_STATE_DIR/console/archived.json`
  (atomic replace), never a mutation of `~/.claude/projects`. Archived sessions
  drop out of the sidebar, stay retrievable by id, ignore the 72h cutoff, and
  return on unarchive. Nothing is destructive.
- **Summarize + archive** — runs `/session-summary` **independently, off-session**:
  a throwaway `claude -p` process (no `--resume`) is handed the session UUID as
  the skill argument, so the skill digests the transcript from disk
  (`session_digest.py`) and writes the changelog + lessons to kmcp **without ever
  resuming or appending to the session**. Because nothing writes back, the 15s
  two-writer guard is gone and the archive is decoupled — the session is archived
  the moment the summary is dispatched (its outcome is tracked in `SUMMARIZING`
  for visibility, not as an archive gate).
- **Mine angles** — `csd angles --session <sid>` on demand, so the rail is
  usable without `csd angles-watch` running.

### Curation — the span action-vocabulary

`track → event`, `record → lesson`, `task → task` deposit kmcp writes;
`load` / `drop` are context ops that compose the operator's *next message*
(the design's "the operator's next message IS the curation") and write nothing.

Writes are **two-phase**: compose a draft, validate with `import_entries`
`dry_run`, show it, write only on explicit confirm. A small model's headline
never reaches the corpus unreviewed. Two further guards:

- The entry document is passed as **JSON**, which is valid YAML 1.2 — sidestepping
  the `import_entries` YAML footguns wholesale (unquoted `#` truncation, bare
  timestamps coerced to datetime, angle-bracket placeholder rejection).
- Application inference **proposes, never decides**. The cwd basename is a guess
  (`final-taglists` is not a kmcp app); it is validated against the live
  `list_applications`. A confirmed write is *refused* when the app was a
  fallback or when kmcp is unreachable — otherwise the entry lands silently in
  the wrong corpus, or invents a junk application out of a directory name.

The kmcp reads-rail counts the `knowledge-cli call <tool>` Bash shim as a read,
not just `mcp__*__<tool>` — a session that took the fallback loaded just as much
context and must not vanish from the rail.

Superseded and REMOVED (2026-07-17): `angles-serve` / `angles_web.py`, a
read-only LAN dashboard that duplicated the session list and reads-rail beside
the console. Its watcher lives on as `angles_watch.py`; its gems — the
session-management "sessions" tab and the subagent drill-down — were ported
into the console (threads overlay, Agent-row child links); its UI is gone.
The console is the single web surface: `csd console` binds 127.0.0.1:4462 by
default, and any non-loopback bind (e.g. `--host 0.0.0.0 --port 8791` for the
LAN) requires token auth (`CSD_CONSOLE_TOKEN`, auto-generated if unset;
`?token=` once, then a cookie).

## Session management (`csd angles sessions` / `csd angles digest`)

The open-thread inventory (`session_mgmt.py`): one row per recent main
session with TRUE last activity = `max(messages.ts)` — NEVER transcript mtime
(bulk file touches create clusters of identical mtimes; mtime only ever lies
toward "more recent", so `sessions.modified_at` is used solely as a superset
window filter). Columns: short id, project, branch, last activity, msgs,
summary_state classification, verdict LIVE (last msg ≤ ~15 min) / OPEN /
OPEN-delta / CLOSED.

**Delta-after-summary**: for summarized sessions, the transcript tail after
the summary watermark (resolution order: `leaf_uuid_at_summary` →
`message_count_at_summary` → kmcp session entry `created_at` from the
knowledge DB) is classified deterministically (code, no LLM): `none` /
`confirmation_only` (short confirm prompts, light chatter) /
`auto_compaction_only` (isCompactSummary + command wrappers) / `real`
(file/kmcp/git mutations, substantive prompts, ≥8 tool calls, or ≥2000 chars
of tail narration) → verdict `OPEN-delta`, needs re-capture. `csd angles
digest REF --delta` renders exactly that tail; plain digests default to a
head 40 / tail 120 record window (`--full` to disable) since full transcripts
reach 7.7MB. Transcript resolution is worktree-aware: `sessions.file_path`
first, then glob `~/.claude/projects/*/<id>.jsonl`.

Doctrine (same as reconcile.py): truth from the ledger not the narrator;
source never mutated (read-only over archive + knowledge DB + transcripts,
no new state tables, no kmcp writes); DB/transcript failures degrade a row to
`unknown`, never crash the lens. The session console exposes the lens as its
"threads" overlay (`/api/mgmt`, `/api/digest?id=<sid>`), polled at 30s.

## Subagent (sidechain) visibility

Every sidechain file also upserts a **child session row** keyed
`<parent_session_id>:<agent_id>` (`is_subagent=true`; agentType/description
from the adjacent `agent-<id>.meta.json` sidecar; seed prompt as
`first_prompt`). Sidechain **messages stay under the parent session_id** —
source is never re-shaped. Aggregate semantics: on MAIN sessions the unprefixed
aggregate columns are **ROLL-UP** (children included, as they always were);
`own_*` columns carry main-chain-only counts; `user_prompt_count` is
main-chain-only (sidechain seed prompts no longer inflate it). Child rows carry
their own aggregates (`total_* == own_*`). `v_agent_children` is the spawn
ledger: one row per Agent tool_use ⨝ tool_result (`tool_use_result` carries
agentId/agentType/status/totals — the harness's record, never agent
self-report) with a `child_session_key` link. Navigation: `csd angles` accepts
`<parent>:<agent_id>` or a bare 17-hex agent id as `--session`; the `agents`
angle (prefix A) headlines each Agent/SendMessage/TaskStop in a turn; the
console serves child transcripts at `/api/session?id=<parent>:<agent_id>`
(Agent tool rows link to the child view; the child header back-links to the
spawning message), its threads overlay shows an `agents n (running/failed)`
badge per session, its nav rows carry a total/live sidechain glance, and
`angles-watch` mines live sidechains under their child keys.
Volatile background-task outputs
(`/private/tmp/claude-*/<proj>/<sid>/tasks/*.output`) are swept verbatim into
`task_outputs` at sync time (idempotent by mtime, 5MB bound) — the archive is
their only durable copy. `csd backfill-subagents` is the one-shot backfill for
pre-existing archives.

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
- `angles.py` — turn extraction + the ANGLES extractors/probes; the state dir.
- `angles_watch.py` — headless ambient miner (settle-detect + single worker).
- `console/` — reply-capable session console (stdlib HTTP, `server.py` +
  `index.html`); reads transcripts and the angles state dir, nothing else.
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
