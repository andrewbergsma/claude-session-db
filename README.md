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

- **🔒 Lossless by design.** Content blocks and tool results are stored **verbatim** — no truncation. The largest results are pulled from `tool-results/*.txt` overflow files. `tldr` is a *nullable derived sibling*, never a replacement.
- **💰 Full token economics.** Every assistant message captures input + output + cache_read + cache_creation + ephemeral, plus the raw `usage` JSONB. Per-skill / per-MCP / per-agent absorption falls out of `v_token_by_attribution`.
- **🧩 JSONB escape-hatch everywhere.** `raw`, `usage`, `tool_input`, `tool_use_result`, `attachment`, `stop_details`, `diagnostics` columns absorb JSONL field drift **without a migration**.
- **⚡ Incremental & idempotent.** Sync keys off `*.jsonl` mtime (`st_mtime_ns`), not the stale sessions-index. Re-ingesting is safe — messages are keyed by `uuid`, child rows cleared per source file.
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
| `csd stats` | Table row counts + database size |
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
| `csd angles-serve` | Ambient LAN dashboard: watcher + one row per live session, drill into subagents |
| `csd backfill-subagents` | One-shot: materialize child session rows for already-ingested sidechains |
| `csd dsn` | Print the connection target (password redacted) |
| `csd open` | Interactive shell (`pgcli`/`psql`) |

## 📊 What you can query

**17 tables** capture the full transcript graph — `sessions`, `messages`, `content_blocks`, `tool_results`, `agent_tasks`, `attachments`, `file_history`, `pr_links`, and more — each with its raw JSONB escape-hatch.

On top sit **analytic views**, ready to `SELECT` from:

| View | Lens |
|---|---|
| `v_session_overview` | One row per session — counts, tokens, errors, precomputed |
| `v_token_by_attribution` | Token absorption per skill / MCP / agent |
| `v_token_cost_by_model` · `v_token_cost_daily` | Spend through the caching lens |
| `v_daily_usage` · `v_project_activity` | Activity over time and across projects |
| `v_error_by_class` · `v_error_recovery` | Where things fail, and how they recover |
| `v_tool_usage` | Tool-call frequency and cost |
| `v_compaction` | Context-compaction events and pre-token counts |
| `v_agent_children` | One row per subagent spawn — type, status, tokens, child session link |

```bash
csd views        # full list, live from the database
```

## 🧭 Session management (`csd angles sessions`)

The open-thread inventory: one row per recent main session with its **true**
last activity — `max(messages.ts)` from the archive, never transcript mtime
(bulk file touches produce clusters of identical mtimes that make mtime lie) —
plus message count, summary classification, and an
**OPEN / OPEN-delta / LIVE / CLOSED** verdict (LIVE = last message within
~15 min).

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
`csd angles-serve` exposes the same lens as a **sessions** tab
(`/api/mgmt`, `/api/digest/<sid>?delta=1`), with row-click digests.

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
- **DDL off the hot path** — `CREATE OR REPLACE VIEW` runs only on a schema-version mismatch, never on every tick.

## 🔌 Bonus: statusline

[`statusline/`](statusline/) ships a Claude Code statusline command that surfaces live session stats. See its [README](statusline/README.md) for wiring.

---

<div align="center">
<sub>Built for understanding how Claude Code actually spends its tokens. 🤖</sub>
</div>
