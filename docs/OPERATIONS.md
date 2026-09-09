# csd operations — summarize, sweep, session management, subagents, usage

Extracted from `CLAUDE.md` on 2026-09-09 to keep the always-loaded memory file
under budget. Full doctrine for the dual-account usage report, the phase-4
roll-up, the `/session-summary` skill seam, the open-thread lens, subagent
(sidechain) visibility, and sweep reliability + lock-convoy recovery. Verbatim
from CLAUDE.md.

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

**Repeatable delta passes.** A session summarized once is summarized *again*
over only the tail its prior pass never saw. `_watermark_for` resolves where the
last pass stopped (leaf → count → kmcp entry `created_at`); `_delta_gate` grades
the tail with `classify_delta` and opens a delta window only when it is `real`
and clears `CSD_SUMMARIZE_MIN_DELTA_RECORDS` (never without a resolvable
watermark — full scope wearing a continuation label is the failure this
prevents). The digest is `render(since=)`, the entry is dated to the window's
END, titled `Session (cont. N)`, spans the window, links the prior pass and
carries `delta-capture`. The `summary_passes` ledger records every pass
(`in_flight`/`written`/`failed`) behind a per-session advisory lock, so the
console and the launchd timer can never dispatch two passes over one tail;
`CSD_SUMMARIZE_MAX_PASSES` (6) caps a session's entries.

Reliability mirrors the sweep: `summarize.lock` liveness guard +
`summarize.heartbeat` (`csd summarize-health`), per-session failure isolation
with a `summarize_attempts` backoff ledger (`MAX_ATTEMPTS`, 6h backoff), and a
quiesce gate (`--min-idle`, default 900s) so live sessions are never digested
mid-flight. Launchd timer: `launchd/com.claude-session-db.summarize.plist`
(every 30 min, default 2 sessions/tick — the ~700-session backlog drains
gradually; `csd summarize -n 20` is the manual backfill lever).

## The skill seam (`csd digest` / `csd summary-scope`)

The two commands `/session-summary` calls. They exist so the skill stops
carrying **repo-specific plumbing** it cannot get approved headless: a
`find ~/.claude/projects -name "$SID.jsonl" | head -1` command substitution and
an absolute `python3 .../session_digest.py` path.

- **`csd digest REF [--since TS] [--full] [--head N] [--tail N]`** — resolves
  the ref worktree-aware (`sessions.file_path`, then a glob over
  `~/.claude/projects/*/<id>.jsonl`; a unique prefix is enough) and calls
  `session_digest.render` — **not a second renderer**, so the header lines
  (`SESSION DIGEST · …`, `span:` / `delta span:`) are byte-for-byte the old
  script's. It runs with **no database** (the glob alone resolves a full id).
  Default scope is the WHOLE transcript — `csd angles digest` is the
  head/tail-windowed sibling, and a silently elided middle is a silently short
  summary. `--since` is the DB-free half of `--delta`: an explicit watermark,
  no lookup. Unresolvable → exit 1 with `NO TRANSCRIPT FOUND for <ref>` on
  stderr; a `<parent>:<agent_id>` child key is refused pointing at the parent
  (session_digest renders main-chain records only).
- **`csd summary-scope REF [--json] [--mode auto|force|off]`** — does a kmcp
  summary already exist, and what would the NEXT pass cover? `scope: full` (no
  prior capture, or none with a resolvable watermark), `scope: delta` (window
  opens at `since`; the exact `csd digest … --since …` command is printed),
  `scope: none` (captured, tail not substantive — nothing new to write). This
  is what lets an **in-session** `/session-summary` detect a continuation pass;
  off-session the console still hands the window down in the envelope.

**One grader.** `resolve_summary_scope` / `prior_capture` live in
`summarize.py` beside the `_delta_gate` they wrap; the console binds its own
DSNs to them and the CLI calls them directly, so the button, the launchd timer
and the skill can never disagree about what a pass covers. Doctrine unchanged
in the move: never raises, and an unreachable archive degrades to `pass 1 /
full` with the reason printed, exit 0.

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

