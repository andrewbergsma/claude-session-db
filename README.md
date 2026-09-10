<div align="center">

# 🗄️ claude-session-db

### A **lossless Postgres archive** for your Claude Code session transcripts.

*Every message, every tool call, every token — parsed out of JSONL and into a database you can actually query.*

<br>

![Python](https://img.shields.io/badge/python-3.10+-3776AB?logo=python&logoColor=white)
![Postgres](https://img.shields.io/badge/postgres-16-4169E1?logo=postgresql&logoColor=white)
![psycopg](https://img.shields.io/badge/psycopg-3-336791)
![status](https://img.shields.io/badge/status-active-success)

</div>

---

Claude Code writes a firehose of `~/.claude/projects/**/*.jsonl` — main threads, subagent sidechains, tool-result overflow, token usage, the works. It's all *there*, but it's append-only JSONL scattered across directories. **`csd`** parses it losslessly into Postgres so you can ask real questions:

> *Which skill burned the most tokens last week? How much did cache reads save me? Where do my sessions error out and recover? What did that subagent actually do?*

```bash
csd query "SELECT skill, sum(output_tokens) FROM v_token_by_attribution GROUP BY 1 ORDER BY 2 DESC LIMIT 10"
```

## ✨ Why it's different

- **🔒 Lossless by design — with three named exceptions.** Content blocks and tool results are stored **verbatim**, never truncated; the largest results are pulled from `tool-results/*.txt` and `*.json` overflow files, and `tldr` is a *nullable derived sibling*, never a replacement. Where a `raw` escape hatch exists (`messages`, `system_events`, `session_records`) the whole record is kept. **Three families had no hatch through schema v9** and were genuinely lossy: `attachments` (keeps only the `attachment` object), the promoted-columns-only tables (`queue_operations`, `pr_links`, `file_history`, `agent_tasks`), and the seven latest-wins session-metadata types, which collapse into one `sessions` column each. All three are documented in [`DATA_MODEL.md`](DATA_MODEL.md). **Schema v10 (3.24.0) closes the first and the third** — `attachments.raw`, plus the seven metadata types and `queue-operation` archived verbatim in `session_records` — leaving only `pr_links`, `file_history` and `agent_tasks` promoted-columns-only.
- **💰 Full token economics.** Every assistant message captures input + output + cache_read + cache_creation + ephemeral, plus the raw `usage` JSONB. Per-skill / per-MCP / per-agent absorption falls out of `v_token_by_attribution`.
- **🧩 JSONB escape-hatch on every table that has one.** `raw`, `usage`, `tool_input`, `tool_use_result`, `attachment`, `stop_details`, `diagnostics`, `payload`, `block_payload`, `cost_state`, `worktree_session` absorb JSONL field drift **without a migration** — which is exactly why the three tables *without* a hatch are worth knowing by name (above).
- **⚡ Incremental & idempotent.** Sync keys off `*.jsonl` mtime (`st_mtime_ns`), not the stale sessions-index. Re-ingesting is safe: per-file tables are cleared by `source_file` and re-inserted, so a re-sync corrects them. Note that `messages` / `attachments` / `system_events` are `ON CONFLICT (uuid) DO NOTHING` rather than upserts — an existing row whose source file is gone is only correctable by a backfill.
- **🚨 Nothing is dropped in silence.** A session-scoped record type `csd` doesn't model yet still lands **verbatim** in `session_records`, flagged `is_modelled = false` — and the same sweep that stored it says so, on its summary line, its heartbeat, `csd sweep-health` and `csd stats`. New Claude Code record types show up on the next sweep, not on the next audit.
- **🛡️ Hardened background sweep.** A launchd-timed `csd sweep` with a liveness guard, heartbeat/error detection, and an idle-transaction reaper — built after a real lock-convoy once starved the schedule for ~9h.

## 🚀 Quickstart

```bash
# 1. Install (editable, with uv or pip)
uv pip install -e .        # or: pip install -e .

# 2. Point it at your Postgres
cp .env.example .env       # then edit DATABASE_URL / CSD_DATABASE_URL

# 3. Pull your sessions in
csd ingest

# 4. Look around
csd stats
csd recent 10
```

`csd` auto-derives its DSN from `$DATABASE_URL` (swapping the db name to `claude_sessions`), or you can set `$CSD_DATABASE_URL` directly — in `.env` or the shell. See [`.env.example`](.env.example).

## 🧰 Commands

| Command | What it does |
|---|---|
| `csd ingest` | Incremental sync (mtime-based glob over `*.jsonl`) |
| `csd ingest --rebuild` | `DROP SCHEMA` + rebuild from scratch |
| `csd ingest --force` | Re-sync every file regardless of mtime |
| `csd stats` | Table row counts + database size, plus the `session_records` type census (⚠ flags UNMODELLED types) |
| `csd recent [N]` | Most recent sessions |
| `csd query "SQL"` | Ad-hoc SQL (`--csv` for CSV out) |
| `csd views` | List the analytic views |
| `csd sweep` | Launchd-timed ingest + live observability head (guarded) |
| `csd sweep-health` | Watcher: heartbeat age / last outcome / held lock (DB-free) |
| `csd reconcile-summaries` | Pre-LLM gate: classify sessions summarized / not_required / pending |
| `csd unsummarized` | List the pending phase-4 work queue (newest first) |
| `csd summarize` | Phase-4 roll-up: digest → local Ollama → verified kmcp entry (guarded) |
| `csd summarize-health` | Watcher for the summarize timer (DB-free) |
| `csd mark-summarized` | Stamp a session's summary watermark after a verified write |
| `csd angles` | Pull-based turn mining: ID-addressable headlines for one turn |
| `csd angles show ID` | Print the persisted detail behind a headline |
| `csd angles sessions` | Session-management lens: open-thread inventory + delta-after-summary verdicts |
| `csd angles digest REF` | Per-session digest (`--delta` for the post-summary tail, `--head/--tail/--full`) |
| `csd digest REF` | THE session digest by id — worktree-aware, works with no database, `--since TS` for the continuation tail |
| `csd digest REF --cr` | The context-reduced cut `/session-summary`'s fresh subagent reads: prose verbatim, subagent reports + tool errors head-capped, kmcp reads/writes as one-line refs, every other row a stub with its CR row id (`--since TS`, `--out PATH`; accounting on stderr) |
| `csd digest REF --row ID` | Expand one stub — the full body of one CR manifest row (`t:<tool_id>` result, `x:<tool_id>` input, `s:`/`u:`/`a:` records); exit 2 if unknown |
| `csd summary-scope REF` | Already summarized? What a NEXT pass would cover (`full` / `delta` / `none`, `--json`) |
| `csd angles-watch` | Headless miner: keep the angles state dir warm (serves nothing) |
| `csd console` | The web UI: reply-capable session console — chat, kmcp reads, angle rail, threads lens, subagent drill-down (127.0.0.1:4462; token-authed on LAN binds) |
| `csd backfill-subagents` | One-shot: materialize child session rows for already-ingested sidechains |
| `csd dsn` | Print the connection target (password redacted) |
| `csd open` | Interactive shell (`pgcli`/`psql`) |

## 📊 What you can query

**21 tables** capture the full transcript graph — `sessions`, `messages`, `content_blocks`, `tool_results`, `agent_tasks`, `attachments`, `file_history`, `pr_links`, `session_records`, and more. Three of them carry a raw JSONB escape-hatch for the whole record (`messages.raw`, `system_events.raw`, `session_records.payload`); the rest keep promoted columns and a per-field hatch where one exists.

On top sit **16 analytic views**, ready to `SELECT` from — the ones worth knowing by name:

| View | Lens |
|---|---|
| `v_session_overview` | One row per session — counts, tokens, errors, precomputed |
| `v_token_by_attribution` | Token absorption per skill / MCP / agent |
| `v_message_cost` | The reusable per-message costing base every spend view is built on |
| `v_token_cost_by_model` · `v_token_cost_daily` | Spend through the caching lens |
| `v_token_usage_by_model` | Tokens and cache-hit % per model |
| `v_error_summary` | Every failed tool result, classified |
| `v_unsummarized` | The roll-up work queue — pending, non-subagent sessions |
| `v_daily_usage` · `v_project_activity` | Activity over time and across projects |
| `v_error_by_class` · `v_error_recovery` | Where things fail, and how they recover |
| `v_tool_usage` | Tool-call frequency and cost |
| `v_compaction` | Context-compaction events and pre-token counts |
| `v_agent_children` | One row per subagent spawn — type, status, tokens, child session link |
| `v_session_cost_drift` | `csd`'s computed cost vs Claude Code's **own** reported cost, with the terms to attribute a gap |

```bash
csd views        # full list, live from the database
```

Two of these are worth knowing about by name. **`session_records`** is the
catch-all: every session-scoped record type Claude Code emits that has no
dedicated table of its own — `cost-state`, `worktree-state`, `relocated`,
`fork-context-ref`, `atis-latch` and the rest — kept **verbatim**, keyed by
source file + line. Anything `csd` has never seen lands there too with
`is_modelled = false`, which is the standing "what did Claude Code just add"
probe:

```sql
SELECT record_type, count(*), max(ts)
FROM session_records WHERE NOT is_modelled GROUP BY 1 ORDER BY 2 DESC;
```

**`v_session_cost_drift`** puts `csd`'s computed spend next to the harness's own
`cost-state` total for the same session, and carries the terms needed to explain
a gap rather than just show one — `api_message_ratio` (one API response can
appear as several message rows), sidechain roll-up, model-fallback rows and
unpriced rows. See [`DATA_MODEL.md`](DATA_MODEL.md) for the full schema.

## 🧭 Session management (`csd angles sessions`)

The open-thread inventory: one row per recent main session with its **true**
last activity — `max(messages.ts)` from the archive, never transcript mtime
(bulk file touches produce clusters of identical mtimes that make mtime lie) —
plus message count, summary classification, and an
**OPEN / OPEN-delta / LIVE / CLOSED** verdict (LIVE = last message within
~15 min).

A **FLAGS** column marks the sessions that used to read like ordinary rows:
`bg` — a background session; `mv` — the session **relocated** (a `/cd`, or a
worktree enter), so the PROJECT column reflects where it was *filed*, not where
it is now; `fk` — a **fork**, which inherited another session's context. `—`
when a session is none of these.

For summarized sessions it also runs **delta-after-summary detection**: the
transcript tail after the summary watermark (`leaf_uuid_at_summary` →
`message_count_at_summary` → the kmcp entry's `created_at`, first resolvable
wins) is classified deterministically as `none` / `confirmation_only` /
`auto_compaction_only` / **`real`** — real deltas (file mutations, kmcp
writes, git mutations, substantive prompts, or heavy tail narration) flip the
verdict to `OPEN-delta`: the summary missed work and the session needs
re-capture. Post-summary tails have carried whole findings the summaries
never saw; this is the lens that catches them.

```bash
csd angles sessions                  # last 7 days, with delta detection
csd angles sessions --window-days 0 --json   # everything, machine-readable
csd angles digest d77cf821 --delta   # only what the summary has NOT seen
csd angles digest 926684e7           # head/tail-windowed digest (7MB-safe)
```

Transcript resolution is **worktree-aware**: `sessions.file_path` from the
archive is tried first (it points into `--claude-worktrees-*` project dirs a
base-dir lookup would miss), falling back to a glob across
`~/.claude/projects/*/<id>.jsonl`. Everything is read-only over the archive,
the knowledge DB, and the transcripts; an unreachable DB or missing
transcript degrades that row to `unknown` instead of failing the lens.
The session console exposes the same lens as its **threads** overlay
(`/api/mgmt`, `/api/digest?id=<sid>&delta=1`), with row-click digests.

## 🖥️ Session console (`csd console`) — the single web UI

The one web surface. Reply-capable: it renders each session's transcript as a
chronological event stream (chat turns, inline kmcp reads/searches joined to
their tool_results, tool rows, choice cards), the latest turn's angle rail
(mined out-of-band by `csd angles-watch`), the **threads** overlay
(open-thread inventory + delta-after-summary digests), and **subagent
navigation** — agents badges in the nav, Agent rows linking to child
(`<parent>:<agentId>`) focus views, and a spawn-anchor back-link into the
parent. Answer resumes a session (`claude -p --resume`, two-writer guarded);
Fork branches it; Stop/Archive/Summarize act on it.

```bash
csd console                                  # 127.0.0.1:4462, no auth needed
csd console --host 0.0.0.0 --port 8791       # LAN bind — token auth REQUIRED
CSD_CONSOLE_TOKEN=<secret> csd console --host 0.0.0.0   # pin the token
```

On a non-loopback bind a shared token is required (auto-generated and printed
at startup if unset; append `?token=<secret>` once — a cookie keeps you in).
This is not a read-only surface: `/api/answer` and `/api/fork` spawn
`claude -p` processes, so an unauthenticated LAN bind would be remote code
execution; `--no-auth` exists but warns loudly and should never leave a
trusted network.

## 🏗️ Architecture

```
~/.claude/projects/**/*.jsonl          ← the source firehose
        │
        ▼
jsonl_records.py   ── parse records (stdlib-only dataclasses; every record keeps its raw dict)
subagent.py        ── discover subagent sidechains + tool-result overflow
        │
        ▼
sync.py            ── glob + mtime incremental sync engine
        │
        ▼
postgres.py        ── SessionArchive: schema DDL, JSONB columns, batched idempotent upserts, views
        │
        ▼
   claude_sessions  (Postgres 16)  ←──  cli.py (the `csd` CLI)
```

**Transcripts are telemetry, not knowledge** — kept in their own database, cross-linked to other systems only by `session_id`.

## 🛡️ Reliability

The `csd sweep` agent (every 300s via launchd) is hardened against the failure mode that once silently starved the schedule and convoyed the whole DB:

- **Liveness guard** — a PID+age pidfile; a stale lock (dead PID, or alive but past `CSD_SWEEP_MAX_AGE_S`) is *reclaimed*, so a wedged predecessor can never become a permanent block.
- **Heartbeat / error detection** — every sweep writes `{ts, ok, detail}`; `csd sweep-health` reports staleness and last outcome with exit codes, and it's **DB-free** so it still works when the archive itself is wedged.
- **Transaction lifetime** — `idle_in_transaction_session_timeout` reaps abandoned transactions; reads commit immediately so the sweep never sits `idle in transaction` between phases.
- **DDL off the hot path** — `CREATE OR REPLACE VIEW` runs only on a schema-version mismatch, never on every tick. Column additions sit behind an `information_schema` guard so the `ALTER` fires once, and data backfills are bounded (20s/tick), resumable from a cursor, and isolated — a broken backfill can never stop ingest.
- **The UNMODELLED tripwire** — a record type `csd` doesn't model yet is stored and *counted*: the sync summary and the one-line sweep form print the census by type, and it rides the heartbeat so `csd sweep-health` shows it as a `notice:` even though that watcher is deliberately DB-free. It never sets `ok=false` and never changes an exit code — a new record type is a signal to act on, not a sweep failure. The sweep also reports project directories whose name it **couldn't believably decode** (Claude Code maps both `/` and `.` to `-`, so the encoding isn't invertible; `projects.encoded_path` is the exact key and is never derived).

## 🔌 Bonus: statusline

[`statusline/`](statusline/) ships a Claude Code statusline command that surfaces live session stats. See its [README](statusline/README.md) for wiring.

---

<div align="center">
<sub>Built for understanding how Claude Code actually spends its tokens. 🤖</sub>
</div>
