# DATA_MODEL.md — the `claude_sessions` schema reference

**Schema version 9** · Postgres 16 · database `claude_sessions`.

This is the authoritative reference for **every table, column, view and index**
in the archive: its type, its nullability, the JSONL field it comes from, the
Claude Code version that introduced that field where known, and whether it is
legacy.

Two things this document is careful to state, because both have been silently
wrong before:

- **Where a column comes from.** A column with no stated source is a derivation,
  and a derivation can drift from the transcript. Every row below names either a
  JSONL field or the code that computes it.
- **What is NOT a column.** The archive is lossless — every record keeps a `raw`
  JSONB — so "not modelled" never means "not stored". [Raw-only
  fields](#8-raw-only-fields) lists what is present in the archive but reachable
  only through JSONB.

Companion documents: `CLAUDE.md` (architecture and doctrine), `CHANGELOG.md`
(release history), `claude_session_db/postgres.py` (the DDL itself — this file
documents it, it does not define it).

> **Provenance.** Sections 1-9 were written against the live catalog on
> 2026-09-02 (schema v9) and against a 30-day scan of `~/.claude/projects`
> (2,015 files, ~500K records, Claude Code v2.1.161-2.1.258). The
> [field census](#appendix-a-jsonl-field-census-2026-06-01) is the earlier
> frequency audit, kept as an appendix.

---

## Contents

1. [Source of truth: the JSONL on disk](#1-source-of-truth-the-jsonl-on-disk)
2. [Conventions used in this document](#2-conventions-used-in-this-document)
3. [Core tables](#3-core-tables) — projects, sessions, messages, content_blocks,
   tool_results
4. [Record-type tables](#4-record-type-tables) — attachments, system_events,
   file_history, file_backups, queue_operations, pr_links, agent_tasks,
   task_outputs, **session_records**
5. [`session_records` payload dictionary](#5-session_records-payload-dictionary)
6. [Reference and control tables](#6-reference-and-control-tables) — metadata,
   sync_state, model_pricing, service_tier_pricing, summary_state,
   summary_passes, summarize_attempts
7. [Views](#7-views)
8. [Raw-only fields](#8-raw-only-fields)
9. [Migration history](#9-migration-history)
- [Appendix A: JSONL field census](#appendix-a-jsonl-field-census-2026-06-01)

---

## 1. Source of truth: the JSONL on disk

```
~/.claude/
    projects/
        <project-slug>/                        # e.g. -Users-andrew-GitHub-knowledge
            sessions-index.json                 # NOT used — covers <25% of projects, stale
            <session-uuid>.jsonl                # main session transcript
            <session-uuid>/
                subagents/
                    agent-<17hex>.jsonl         # sidechain transcript
                    agent-<17hex>.meta.json     # {agentType, description, toolUseId, spawnDepth}
                    workflows/wf_*/agent-*.jsonl  # nested workflow agents
                tool-results/
                    <tool_use_id>.txt           # overflow tool result, plain text
                    <tool_use_id>.json          # overflow tool result, content-block array
                    <name>.pdf                  # WebFetch download — NOT ingested
                    pdf-<uuid>/page-N.jpg       # document renders — NOT ingested
                    extracted/, data/           # agent working dirs — NOT ingested
/private/tmp/claude-<uid>/<project-slug>/<session-uuid>/tasks/<task>.output
                                                # volatile background-task output
```

**Sync signal** is `*.jsonl` filesystem mtime (`st_mtime_ns`), never
`sessions-index.json`. Ingest is idempotent: `messages` / `attachments` /
`system_events` upsert by uuid, `session_records` by `(source_file,
source_line)`, and every other per-file table is cleared by `source_file` before
re-insert.

### The project-slug encoding is not invertible

Claude Code maps **both `/` and `.`** to `-`, so a `-` in a directory name is
three different characters. `-Users-andrew--claude` naively decodes to
`/Users/andrew//claude` → `/Users/andrew/claude`, which is a real-looking path
and the wrong one. Every worktree project
(`…-infrastructure--claude-worktrees-net-v1`) is in that family, as are
`CLAUDE_CODE_PROJECT_DIR_NAME` names and the v2.1.224 long-path scheme.

`sync.decode_project_path()` therefore prefers the transcript's own `cwd` when
it re-encodes to the same directory name (the only reliable inversion), and
`sync.project_path_is_decodable()` flags the rest. Undecodable slugs are counted
and named in the sync summary.

**`projects.encoded_path` is the key and is always exact.** `decoded_path` and
`project_name` are descriptive and may be a best-effort guess.

---

## 2. Conventions used in this document

| Convention | Meaning |
|---|---|
| **Source** | The JSONL field the column is read from, or `derived` + the code that computes it. |
| **Since** | The Claude Code version that introduced the SOURCE FIELD, where known. Blank = present since the archive began. |
| **LEGACY** | Kept and still populated for historical rows, but Claude Code no longer emits the source. Never dropped — the archive does not remove columns. |
| `→` | "feeds"; e.g. `cost-state.totalCostUSD → sessions.reported_cost_usd`. |

**Universal per-record fields.** Every conversation record (user / assistant /
system / attachment) carries `type`, `uuid`, `sessionId`, `timestamp`,
`parentUuid`, `cwd`, `gitBranch`, `version`, `userType`, `isSidechain`,
`entrypoint`. Where a table has `cwd` / `git_branch` / `cc_version` /
`entrypoint` / `is_sidechain` columns they come from these, and are not repeated
in every table below.

**Every JSONB escape hatch is deliberate.** `raw`, `usage`, `tool_input`,
`tool_use_result`, `attachment`, `stop_details`, `diagnostics`, `payload`,
`block_payload`, `server_tool_use`, `iterations`, `worktree_session`,
`cost_state` absorb Claude Code field drift without a migration. That is why the
v2.1.161-258 changes cost no data in the tables that HAD a hatch, and cost real
data in the places that did not.

---

## 3. Core tables

### `projects`

One row per project directory under `~/.claude/projects`.

| Column | Type | Null | Source | Notes |
|---|---|---|---|---|
| `project_id` | bigint | no | derived | PK, BIGSERIAL |
| `encoded_path` | text | no | directory name | **UNIQUE. The key, and always exact.** |
| `decoded_path` | text | no | derived (`decode_project_path`) | Best-effort; see §1 |
| `project_name` | text | no | derived | `Path(decoded_path).name` |
| `first_seen_at` | timestamptz | no | derived | insert time |
| `last_seen_at` | timestamptz | no | derived | touched on every conflict |

Indexes: `idx_projects_name(project_name)`.

---

### `sessions`

One row per main session, **plus** one child row per sidechain keyed
`"<parent_session_id>:<agent_id>"`. Sidechain MESSAGES stay under the parent
session_id — the source is never re-shaped — so on a main session the unprefixed
aggregate columns are a ROLL-UP that includes children, and the `own_*` columns
are main-chain only. On a child row `total_* == own_*`.

Upserts use `COALESCE(EXCLUDED.col, sessions.col)`, so a later file lacking a
field never wipes a value an earlier one set.

#### Identity

| Column | Type | Null | Source | Since | Notes |
|---|---|---|---|---|---|
| `session_id` | text | no | `sessionId`, or `"<parent>:<agent_id>"` | | PK |
| `project_id` | bigint | yes | derived | | → `projects` |
| `file_path` | text | yes | derived | | absolute path of the transcript |
| `is_subagent` | boolean | no | derived | | true on child rows |
| `parent_session_id` | text | yes | derived | | child rows only |
| `agent_id` | text | yes | filename `agent-<hex>` | | child rows only |

#### Session-scoped metadata (latest-wins)

| Column | Type | Null | Source | Since | Notes |
|---|---|---|---|---|---|
| `ai_title` | text | yes | `ai-title.aiTitle` | ~2.1.123 | replaced the `summary` record |
| `custom_title` | text | yes | `custom-title.customTitle` | | on child rows: meta.json `description` |
| `first_prompt` | text | yes | derived | | first non-meta user prompt |
| `last_prompt` | text | yes | `last-prompt.lastPrompt` | ~2.1.123 | resume marker |
| `last_prompt_leaf_uuid` | text | yes | `last-prompt.leafUuid` | ~2.1.123 | the summary watermark anchor |
| `permission_mode` | text | yes | `permission-mode.permissionMode` | ~2.1.123 | |
| `mode` | text | yes | `mode.mode` | ~2.1.123 | |
| `bridge_session_id` | text | yes | `bridge-session.bridgeSessionId` | ~2.1.123 | claude.ai web bridge |
| `agent_name` | text | yes | `agent-name.agentName` | ~2.1.123 | on child rows: meta.json `agentType` |
| `git_branch` | text | yes | `gitBranch` | | first conversation record |
| `cwd` | text | yes | `cwd` | | **where the session STARTED.** Semantics unchanged in v9 |
| `cc_version` | text | yes | `version` | | |
| `entrypoint` | text | yes | `entrypoint` | | |
| `created_at` | timestamptz | yes | derived | | `min(timestamp)` |
| `modified_at` | timestamptz | yes | derived | | file mtime (a SUPERSET of last activity — see below) |
| `message_count` | integer | yes | derived | | recomputed post-ingest |

> **`modified_at` is not last activity.** Bulk file touches create clusters of
> identical mtimes, and mtime only ever lies toward "more recent". Use
> `max(messages.ts)` for true last activity; `modified_at` is only safe as a
> superset window filter.

#### Aggregates (recomputed after every ingest)

| Column | Type | Null | Source | Notes |
|---|---|---|---|---|
| `total_input_tokens` | bigint | yes | derived | ROLL-UP on mains |
| `total_output_tokens` | bigint | yes | derived | ROLL-UP on mains |
| `total_cache_read_tokens` | bigint | yes | derived | ROLL-UP on mains |
| `total_cache_creation_tokens` | bigint | yes | derived | ROLL-UP on mains |
| `user_prompt_count` | integer | yes | derived | **main-chain only, always** |
| `tool_use_count` | integer | yes | derived | ROLL-UP on mains |
| `error_count` | integer | yes | derived | ROLL-UP on mains |
| `compact_count` | integer | yes | derived | `compact_boundary` system events |
| `duration_seconds` | double precision | yes | derived | Σ `turn_duration.durationMs`; NULL (not 0) when unknown |
| `own_total_input_tokens` | bigint | yes | derived | main-chain only |
| `own_total_output_tokens` | bigint | yes | derived | main-chain only |
| `own_total_cache_read_tokens` | bigint | yes | derived | main-chain only |
| `own_total_cache_creation_tokens` | bigint | yes | derived | main-chain only |
| `own_message_count` | integer | yes | derived | main-chain only |
| `own_tool_use_count` | integer | yes | derived | main-chain only |
| `own_error_count` | integer | yes | derived | main-chain only |

#### Fork lineage — schema v9

Every column here comes from the `fork-context-ref` record, which is observed
**only in sidechain files**, so in practice these populate CHILD rows: a forked
subagent inherits its parent session's context, and these say whose and how much.

| Column | Type | Null | Source | Since | Notes |
|---|---|---|---|---|---|
| `forked_from_session_id` | text | yes | `fork-context-ref.parentSessionId` | 2.1.212 | |
| `forked_from_uuid` | text | yes | `fork-context-ref.parentLastUuid` | 2.1.212 | last inherited record |
| `fork_context_length` | integer | yes | `fork-context-ref.contextLength` | 2.1.212 | records inherited |
| `fork_agent_id` | text | yes | `fork-context-ref.agentId` | 2.1.212 | the record's own field |

Index: `idx_sessions_forked_from` (partial, NOT NULL).

> The predecessor is `messages.forked_from` (top-level `forkedFrom`), which
> Claude Code stopped emitting at **v2.1.212**. It is LEGACY and kept.

#### Relocation and worktree binding — schema v9

| Column | Type | Null | Source | Since | Notes |
|---|---|---|---|---|---|
| `current_cwd` | text | yes | latest `relocated.relocatedCwd`, else the last conversation record's `cwd` | 2.1.169 (`/cd`) | LAST known directory |
| `worktree_session` | jsonb | yes | `worktree-state.worktreeSession` | | verbatim; keys below |

`worktree_session` keys (8, all strings): `originalCwd`, `preEnterOriginalCwd`,
`worktreePath`, `worktreeName`, `worktreeBranch`, `originalBranch`,
`originalHeadCommit`, `sessionId`.

Index: `idx_sessions_current_cwd`.

> `cwd` and `current_cwd` are different facts and both matter. `cwd` is where the
> session was FILED (the repos lens and project attribution key off it);
> `current_cwd` is where it ENDED UP. Before v9 a `/cd` or worktree enter left a
> session pointing at a directory it had long since left.

#### Claude Code's own cost ledger — schema v9

From the **latest** `cost-state` record (it is rewritten as the session runs, so
an earlier row reports a fraction of the spend). This is the HARNESS's number,
never csd's; `v_session_cost_drift` compares the two.

| Column | Type | Null | Source | Notes |
|---|---|---|---|---|
| `cost_state` | jsonb | yes | the whole `cost-state` record | includes per-model `modelUsage` |
| `reported_cost_usd` | numeric | yes | `cost-state.totalCostUSD` | |
| `reported_total_duration_ms` | bigint | yes | `cost-state.totalDuration` | wall clock |
| `reported_api_duration_ms` | bigint | yes | `cost-state.totalAPIDuration` | |
| `reported_tool_duration_ms` | bigint | yes | `cost-state.totalToolDuration` | |
| `reported_lines_added` | integer | yes | `cost-state.totalLinesAdded` | |
| `reported_lines_removed` | integer | yes | `cost-state.totalLinesRemoved` | |
| `has_unknown_model_cost` | boolean | yes | `cost-state.hasUnknownModelCost` | **NULL ≠ false**: NULL means the record never said |

Not promoted (still in `cost_state`): `totalAPIDurationWithoutRetries`,
`startTime`, `modelUsage`.

#### Session kind — schema v9

| Column | Type | Null | Source | Notes |
|---|---|---|---|---|
| `session_kind` | text | yes | `sessionKind` on any record that carries it | `"bg"` = background session |

Measured **constant per session** across user / assistant / attachment / system
records (0 of 2 carrying sessions in the scan showed more than one value), which
is why this is a session attribute. `messages.session_kind` mirrors it so a
future session that DOES vary is not silently flattened.

Index: `idx_sessions_kind` (partial, NOT NULL).

Other indexes on `sessions`: `idx_sessions_project`, `idx_sessions_modified`,
`idx_sessions_subagent`, `idx_sessions_parent`, `idx_sessions_agent_id`.

---

### `messages`

One row per `user` or `assistant` record. PK `uuid`, so re-ingest is idempotent.

> **`uuid` is not the API response id.** One API response can appear as SEVERAL
> `messages` rows sharing one `api_message_id`; measured ratios of 1.8-2.4 on
> real sessions. `v_message_cost` sums per ROW and therefore over-counts — see
> `v_session_cost_drift.api_message_ratio`.

#### Identity and threading

| Column | Type | Null | Source | Notes |
|---|---|---|---|---|
| `uuid` | text | no | `uuid` | PK |
| `session_id` | text | yes | `sessionId` | sidechain rows carry the PARENT's id |
| `parent_uuid` | text | yes | `parentUuid` | |
| `ts` | timestamptz | yes | `timestamp` | |
| `role` | text | no | derived | `user` \| `assistant` |
| `message_type` | text | no | derived | `prompt` \| `tool_result` \| `response` — see below |
| `is_sidechain` | boolean | yes | `isSidechain` | |
| `agent_id` | text | yes | `agentId` | sidechain only |
| `slug` | text | yes | `slug` | → `~/.claude/plans/<slug>.md` |
| `cwd`, `git_branch`, `cc_version`, `entrypoint` | text | yes | universal fields | |
| `source_file` | text | no | derived | absolute transcript path |
| `source_line` | integer | yes | derived | currently always NULL for messages |
| `raw` | jsonb | yes | the whole record | **the escape hatch** |

> **`message_type` classification (corrected in v9).** A user record is
> `tool_result` **iff its content contains a `tool_result` block**, else
> `prompt`. It was previously `"prompt" if content is a STRING else
> "tool_result"`, which filed every image-paste, document-attachment and
> multi-block prompt as a tool result — 7,186 rows in this archive, ~19% of all
> user prompts. Those rows were invisible to `user_prompt_count`, to
> `first_prompt`, and to the reconcile gate's empty/trivial heuristics.

#### User-side

| Column | Type | Null | Source | Since | Notes |
|---|---|---|---|---|---|
| `prompt_text` | text | yes | `message.content` | | string content verbatim; list content = ALL text blocks joined by `\n` (v9; previously only the first) |
| `prompt_id` | text | yes | `promptId` | | |
| `permission_mode` | text | yes | `permissionMode` | | |
| `is_meta` | boolean | yes | `isMeta` | | system-injected user message |
| `is_compact_summary` | boolean | yes | `isCompactSummary` | | |
| `source_tool_assistant_uuid` | text | yes | `sourceToolAssistantUUID` | | the assistant msg whose tool_use this answers |
| `source_tool_use_id` | text | yes | `sourceToolUseID` | | |

#### Assistant-side

| Column | Type | Null | Source | Since | Notes |
|---|---|---|---|---|---|
| `model` | text | yes | `message.model` | | the FIRST model; see `iterations` |
| `api_message_id` | text | yes | `message.id` | | not unique per row — see the note above |
| `request_id` | text | yes | `requestId` | | |
| `stop_reason` | text | yes | `message.stop_reason` | | |
| `stop_details` | jsonb | yes | `message.stop_details` | ~2.1.123 | populated only on `stop_reason='refusal'` |
| `is_api_error` | boolean | yes | `isApiErrorMessage` | | |
| `api_error_status` | integer | yes | `apiErrorStatus` | | |
| `error_text` | text | yes | `error` | | |
| `diagnostics` | jsonb | yes | `message.diagnostics` | ~2.1.123 | |
| `effort` | text | yes | `effort` | **2.1.161-258** | **v9.** `"high"`, … Present on 98.5% of assistant records |

#### Attribution

| Column | Type | Null | Source | Since |
|---|---|---|---|---|
| `attribution_agent` | text | yes | `attributionAgent` | ~2.1.123 |
| `attribution_skill` | text | yes | `attributionSkill` | ~2.1.123 |
| `attribution_mcp_server` | text | yes | `attributionMcpServer` | ~2.1.123 |
| `attribution_mcp_tool` | text | yes | `attributionMcpTool` | ~2.1.123 |
| `attribution_plugin` | text | yes | `attributionPlugin` | ~2.1.123 |

#### Usage

`usage` holds the complete object; the rest are promoted for queryability.

| Column | Type | Null | Source | Since | Notes |
|---|---|---|---|---|---|
| `input_tokens` | integer | yes | `usage.input_tokens` | | base (uncached) input |
| `output_tokens` | integer | yes | `usage.output_tokens` | | |
| `cache_read_tokens` | integer | yes | `usage.cache_read_input_tokens` | | |
| `cache_creation_tokens` | integer | yes | `usage.cache_creation_input_tokens` | | lump total |
| `ephemeral_5m_tokens` | integer | yes | `usage.cache_creation.ephemeral_5m_input_tokens` | | |
| `ephemeral_1h_tokens` | integer | yes | `usage.cache_creation.ephemeral_1h_input_tokens` | | |
| `service_tier` | text | yes | `usage.service_tier` | | → `service_tier_pricing` |
| `inference_geo` | text | yes | `usage.inference_geo` | | |
| `speed` | text | yes | `usage.speed` | | `"fast"` on Opus 5 fast mode; **absent on ~36% of records**, so fast mode is NOT reliably detectable |
| `usage` | jsonb | yes | `message.usage` | | the escape hatch |
| `thinking_tokens` | integer | yes | `usage.output_tokens_details.thinking_tokens` | **2.1.161-258** | **v9.** 55% of records |
| `server_tool_use` | jsonb | yes | `usage.server_tool_use` | **2.1.161-258** | **v9.** 64%. e.g. `{web_search_requests, web_fetch_requests}` |
| `iterations` | jsonb | yes | `usage.iterations` | **2.1.161-258** | **v9. An ARRAY, not a count** — see below |
| `iteration_count` | integer | yes | derived | | `jsonb_array_length(iterations)`; **>1 iff a model fallback occurred** |

> **`usage.iterations` records model fallbacks.** Each element is a per-iteration
> usage object with its OWN `model` and `type`:
> ```json
> [{"type":"message",          "model":"claude-fable-5",  "output_tokens":251},
>  {"type":"fallback_message", "model":"claude-opus-4-8", "output_tokens":1366}]
> ```
> So `messages.model` is not the only model that billed for the row.
> `v_message_cost` prices the whole message at the top-level model and is
> therefore wrong for these; `v_session_cost_drift.fallback_messages` counts
> them. This is the same event as the `fallback` CONTENT BLOCK (§`content_blocks`)
> — recorded twice, in two places.

#### Other

| Column | Type | Null | Source | Since | Notes |
|---|---|---|---|---|---|
| `session_kind` | text | yes | `sessionKind` | **2.1.161-258** | **v9.** mirror of `sessions.session_kind` |
| `forked_from` | jsonb | yes | `forkedFrom` | | **LEGACY — dead since v2.1.212.** `{sessionId, messageUuid}`. Real on older rows; replaced by `sessions.forked_from_*` |

Indexes: `idx_messages_session`, `_ts`, `_role`, `_model`, `_source_file`,
`_attr_skill`, `_attr_mcp`, `_src_tool_asst`, `_agent` (partial),
`_effort` (partial), `_session_kind` (partial), `_fallback`
(partial, `iteration_count > 1`).

---

### `content_blocks`

One row per block of an assistant `message.content`, in order.

| Column | Type | Null | Source | Since | Notes |
|---|---|---|---|---|---|
| `block_id` | bigint | no | derived | | PK, BIGSERIAL |
| `message_uuid` | text | no | derived | | → `messages.uuid` |
| `session_id` | text | yes | derived | | |
| `block_index` | integer | no | derived | | position in `message.content` |
| `block_type` | text | no | `content[].type` | | `thinking` \| `text` \| `tool_use` \| **the block's own type** |
| `content` | text | yes | `.thinking` / `.text` | | verbatim, never truncated |
| `char_count` | integer | yes | derived | | |
| `signature` | text | yes | `.signature` | | thinking blocks only |
| `tool_use_id` | text | yes | `.id` | | tool_use only; joins `tool_results` |
| `tool_name` | text | yes | `.name` | | |
| `tool_input` | jsonb | yes | `.input` | | full input, never truncated |
| `tool_type` | text | yes | derived | | `mcp` (name starts `mcp__`) \| `builtin` |
| `mcp_server` | text | yes | derived | | 2nd segment of `mcp__<server>__<tool>` |
| `source_file` | text | no | derived | | |
| `source_line` | integer | yes | derived | | currently NULL |
| `block_payload` | jsonb | yes | the whole block | **2.1.247** | **v9.** Set only for block types with no dedicated columns |

> **Unknown blocks are kept (v9).** `parse_content_block` used to return None for
> anything that was not thinking/text/tool_use, and the sync skipped it — so the
> v2.1.247 `fallback` block (`{"type":"fallback","from":{"model":…},
> "to":{"model":…}}`) was dropped, AND every later block in that message shifted
> down one `block_index`. Unknown blocks now store under their real
> `block_type` with the payload in `block_payload`. They are deliberately not
> counted as text or tool_use.

Indexes: `idx_cb_message`, `_type`, `_tool`, `_tool_use_id`, `_source_file`.

---

### `tool_results`

One row per `tool_result` block in a user record.

| Column | Type | Null | Source | Notes |
|---|---|---|---|---|
| `result_id` | bigint | no | derived | PK, BIGSERIAL |
| `message_uuid` | text | no | derived | → `messages.uuid` |
| `session_id` | text | yes | derived | |
| `tool_use_id` | text | no | `.tool_use_id` | joins `content_blocks.tool_use_id` |
| `content_text` | text | yes | `.content` | **verbatim, never truncated**; substituted from the overflow file when that is longer |
| `tldr` | text | yes | derived (`tool_tldr.tldr_result`) | heuristic one-liner; nullable sibling, never a replacement |
| `char_count` | integer | yes | derived | of `content_text` |
| `is_error` | boolean | yes | `.is_error` | |
| `error_class` | text | yes | derived (`transcript_analyzer.classify_error`) | NULL unless `is_error` |
| `block_count` | integer | yes | derived | inner content blocks |
| `tool_use_result` | jsonb | yes | `toolUseResult` | client-side structured enrichment. Polymorphic (dict/list/str) — **stored as a blob, never normalized per tool** |
| `from_overflow_file` | boolean | yes | derived | true when the body came from `tool-results/` |
| `source_file` | text | no | derived | |
| `source_line` | integer | yes | derived | currently NULL |

> **Overflow (widened in v9).** `tool-results/<tool_use_id>.txt` **and `.json`**
> are both ingested, keyed on the filename stem. The `.json` form is a
> content-block array introduced in the v2.1.161-258 window and stored as the
> JSON text it is; 230 such files existed unread while their results sat in the
> archive as inline truncations. `.pdf` downloads, `pdf-<uuid>/page-N.jpg`
> renders and the `extracted/` / `data/` agent working directories are
> deliberately NOT ingested — their filenames are not tool_use_ids. Discovery is
> non-recursive for that reason.

Indexes: `idx_tr_message`, `_tool_use`, `_error`, `_error_class` (partial),
`_source_file`.

---

## 4. Record-type tables

### `attachments`

Injected context attachments (`type: "attachment"`).

| Column | Type | Null | Source |
|---|---|---|---|
| `uuid` | text | no | `uuid` (PK) |
| `session_id`, `parent_uuid` | text | yes | `sessionId`, `parentUuid` |
| `ts` | timestamptz | yes | `timestamp` |
| `attachment_type` | text | yes | `attachment.type` — e.g. `deferred_tools_delta` |
| `attachment` | jsonb | yes | the whole `attachment` object (variable shape) |
| `is_sidechain` | boolean | yes | `isSidechain` |
| `source_file` / `source_line` | text / integer | no / yes | derived |

Indexes: `idx_att_session`, `_type`, `_source_file`.

### `system_events`

`type: "system"` records, one row each. PK `uuid`.

| Column | Type | Null | Source |
|---|---|---|---|
| `uuid` | text | no | `uuid` |
| `session_id`, `parent_uuid` | text | yes | `sessionId`, `parentUuid` |
| `ts` | timestamptz | yes | `timestamp` |
| `subtype` | text | no | `subtype` |
| `level` | text | yes | `level` |
| `content` | text | yes | `content` |
| `duration_ms` | integer | yes | `durationMs` (`turn_duration`) |
| `message_count` | integer | yes | `messageCount` (`turn_duration`) |
| `url` | text | yes | `url` (`bridge_status`) |
| `compact_trigger` | text | yes | `compactMetadata.trigger` |
| `compact_pre_tokens` | integer | yes | `compactMetadata.preTokens` |
| `logical_parent_uuid` | text | yes | `logicalParentUuid` |
| `error_status` / `error_type` / `error_message` | integer / text / text | yes | `error.*` (`api_error`) |
| `retry_in_ms` / `retry_attempt` / `max_retries` | double / integer / integer | yes | `retryInMs`, `retryAttempt`, `maxRetries` |
| `is_sidechain`, `slug` | boolean, text | yes | universal |
| `source_file` / `source_line` | text / integer | no / yes | derived |
| `raw` | jsonb | yes | the whole record |

Observed subtypes (30-day scan): `turn_duration` (4,998), `stop_hook_summary`
(4,957 — **returned** after the Jun-2026 audit recorded it as gone),
`compact_boundary` (103), `informational` (70), `local_command` (40),
`model_refusal_fallback` (4), `bridge_status` (4), `model_consent_fallback` (2).
Subtype-specific fields with no column are listed in §8.

Indexes: `idx_sys_session`, `_subtype`, `_source_file`.

### `file_history` / `file_backups`

`file-history-snapshot` records and their tracked files.

**`file_history`** — `snapshot_id` (PK, BIGSERIAL), `session_id`, `message_id`
(`messageId`), `snapshot_message_id` (`snapshot.messageId`), `ts`
(`snapshot.timestamp`), `file_count`, `has_backups`, `is_snapshot_update`
(`isSnapshotUpdate`), `source_file`, `source_line`.

**`file_backups`** — `backup_id` (PK), `snapshot_id` (FK, ON DELETE CASCADE),
`file_path` (the key of `trackedFileBackups`), `backup_file_name`,
`content_hash` (derived: the part of `backupFileName` before `@`), `version`,
`backup_time`.

Indexes: `idx_fh_session`, `_source_file`; `idx_fb_snapshot`, `_path`.

> The incremental sibling `file-history-delta` (v2.1.161-258) lands in
> `session_records`, not here — see §5.

### `queue_operations`

`queue-operation` records: `operation_id` (PK), `session_id`, `ts`, `operation`
(`operation`), `content` (`content`, present ~50% — on enqueue), `source_file`,
`source_line`. Indexes: `idx_qo_session`, `_source_file`.

### `pr_links`

`pr-link` records: `pr_link_id` (PK), `session_id`, `pr_number` (`prNumber`),
`pr_url` (`prUrl`), `pr_repository` (`prRepository`), `ts`, `source_file`,
`source_line`. Indexes: `idx_pr_session`, `_source_file`.

### `agent_tasks`

`started` / `result` agent-lifecycle records, keyed by the content hash `key`
(`v2:<sha256>`): `key` (PK), `agent_id` (`agentId`), `started` (boolean —
`type == "started"`), `result` (jsonb — the arbitrary `result` payload),
`source_file`. Indexes: `idx_at_agent`, `_source_file`.

### `task_outputs`

Background-task outputs swept from `/private/tmp/claude-<uid>/<proj>/<sid>/
tasks/*.output`. That scratchpad is wiped on reboot, so **this table is their
only durable copy.**

| Column | Type | Null | Source | Notes |
|---|---|---|---|---|
| `session_id`, `task_name` | text | no | derived | composite PK |
| `content` | text | yes | file contents | verbatim, bounded at 5 MB with a truncation note |
| `char_count` | integer | yes | derived | |
| `truncated` | boolean | yes | derived | |
| `file_size`, `file_mtime_ns` | bigint | yes | `stat()` | mtime is the idempotence check |
| `source_path` | text | yes | derived | |
| `captured_at` | timestamptz | no | derived | |

Symlinks resolving into `~/.claude/projects` are skipped: their target IS a
subagent transcript the archive already holds losslessly.

### `session_records` — schema v9

**The catch-all.** Session-scoped record types with no dedicated table, kept
verbatim. Before v9 these were parsed into `records["unknown"]` and then
dropped — nothing read that list, which is how eleven record types added between
Claude Code v2.1.161 and v2.1.258 disappeared in silence.

| Column | Type | Null | Source | Notes |
|---|---|---|---|---|
| `session_id` | text | yes | `sessionId` (or the owning session) | |
| `record_type` | text | no | `type` | |
| `ts` | timestamptz | yes | `timestamp` or `ts` | **NULL for the seven types that carry no time** — never invented |
| `agent_id` | text | yes | `agentId` | `fork-context-ref` and other agent-scoped types |
| `is_modelled` | boolean | no | derived | `true` = a known type routed here; `false` = a type csd has NEVER seen |
| `payload` | jsonb | no | the whole record | **verbatim** |
| `source_file` | text | no | derived | |
| `source_line` | integer | no | derived | 1-based line number |

**PK `(source_file, source_line)`.** These records carry no uuid, and a
transcript is append-only, so file+line is the natural key. The table is also in
`PER_FILE_TABLES`, so a re-sync clears before re-inserting.

`is_modelled = false` is the standing tripwire:

```sql
SELECT record_type, count(*), max(ts)
FROM session_records WHERE NOT is_modelled GROUP BY 1 ORDER BY 2 DESC;
```

`csd stats` prints exactly this, and the sync/sweep line and the sweep heartbeat
report it live (`csd sweep-health` shows it as a `notice:`).

Indexes: `idx_sr_session`, `_type`, `_ts`, `_unmodelled` (partial).

---

## 5. `session_records` payload dictionary

The ten record types routed here, with the fields observed in a 30-day scan of
2,015 files. Counts are from that window. Fields marked *(derived)* also feed a
`sessions` column.

### `atis-latch` — 6,077

| Field | Type | Notes |
|---|---|---|
| `atis` | string | 16-hex latch token |
| `sessionId` | string | |

### `worktree-state` — 816

| Field | Type | Notes |
|---|---|---|
| `worktreeSession` | object | *(derived → `sessions.worktree_session`)* |
| `worktreeSession.originalCwd` | string | where the session was before entering |
| `worktreeSession.preEnterOriginalCwd` | string | |
| `worktreeSession.worktreePath` | string | the worktree checkout |
| `worktreeSession.worktreeName` | string | |
| `worktreeSession.worktreeBranch` | string | e.g. `worktree-net-v1` |
| `worktreeSession.originalBranch` | string | |
| `worktreeSession.originalHeadCommit` | string | 40-hex |
| `worktreeSession.sessionId` | string | |
| `sessionId` | string | authoritative over the nested copy |

### `relocated` — 733

| Field | Type | Notes |
|---|---|---|
| `sessionId` | string | |
| `relocatedCwd` | string | *(derived → `sessions.current_cwd`)*. Emitted by `/cd` (v2.1.169) and worktree moves |

### `file-history-delta` — 638

Incremental sibling of `file-history-snapshot`. Carries no `sessionId` — the
owning session is supplied at sync time.

| Field | Type | Notes |
|---|---|---|
| `messageId` | string (uuid) | |
| `snapshotMessageId` | string (uuid) | |
| `trackingPath` | string | absolute path of the tracked file |
| `backup` | object | `{backupFileName, version, backupTime, realParentDir}`; `backupFileName` may be null |
| `timestamp` | string (ISO) | → `session_records.ts` |

### `history-suppression` — 291

| Field | Type | Notes |
|---|---|---|
| `sessionId` | string | |
| `cause` | string | e.g. `restored_owner_mismatch` |
| `vetoedAgainstAccountUuid` | string (uuid) | present on ~4% |
| `ts` | string (ISO) | → `session_records.ts` (note: `ts`, not `timestamp`) |

### `frame-link` — 241

| Field | Type | Notes |
|---|---|---|
| `sessionId` | string | |
| `path` | string | local artifact file (~9%) |
| `frameUrl` | string | `https://claude.ai/code/artifact/<uuid>` (~9%) |
| `title` | string | (~9%) |
| `artifactCount` | integer | (~97%) |
| `timestamp` | string (ISO) | → `session_records.ts` |

### `cost-state` — 107

Claude Code's own running cost ledger. All fields *(derived)* except as noted.

| Field | Type | Notes |
|---|---|---|
| `sessionId` | string | |
| `totalCostUSD` | number | → `sessions.reported_cost_usd` |
| `modelUsage` | object | `{<model>: {inputTokens, outputTokens, cacheReadInputTokens, cacheCreationInputTokens, webSearchRequests, costUSD}}`. Model keys include the long-context form `claude-opus-5[1m]` |
| `hasUnknownModelCost` | boolean | → `sessions.has_unknown_model_cost` |
| `totalDuration` | integer (ms) | → `reported_total_duration_ms` |
| `totalAPIDuration` | integer (ms) | → `reported_api_duration_ms` |
| `totalAPIDurationWithoutRetries` | integer (ms) | not promoted |
| `totalToolDuration` | integer (ms) | → `reported_tool_duration_ms` |
| `totalLinesAdded` | integer | → `reported_lines_added` |
| `totalLinesRemoved` | integer | → `reported_lines_removed` |
| `startTime` | integer (epoch ms) | the SESSION's start, not the record's — deliberately not used as `ts` |

> **These `modelUsage` numbers are what the Claude 5 pricing rows were solved
> from.** See §6, `model_pricing`.

### `artifact-autoreact-ledger` — 56

| Field | Type | Notes |
|---|---|---|
| `type`, `v` | string, integer | `v` = payload schema version (1) |
| `sessionId`, `accountUuid` | string | |
| `artifacts` | object | `{<artifact-uuid>: {savedAt, stampHighWater, everBaselined, everHadThreads, turnTimestamps[], threads[]}}` |

### `artifact-comment-monitor` — 20

| Field | Type | Notes |
|---|---|---|
| `type`, `v` | string, integer | |
| `sessionId` | string | |
| `artifacts` | object | `{<artifact-uuid>: {state, writtenAtMs, title}}` — `state` e.g. `"armed"` |

### `fork-context-ref` — 8

Observed **only in sidechain files**. All fields *(derived)*.

| Field | Type | Notes |
|---|---|---|
| `agentId` | string (17-hex) | → `sessions.fork_agent_id` |
| `parentSessionId` | string (uuid) | → `sessions.forked_from_session_id` |
| `parentLastUuid` | string (uuid) | → `sessions.forked_from_uuid` |
| `contextLength` | integer | → `sessions.fork_context_length`; records inherited |

---

## 6. Reference and control tables

### `metadata`

`key` (PK) / `value`, both text. Known keys:

| Key | Meaning |
|---|---|
| `schema_version` | the DDL version this database was last initialised at |
| `views_version` | the version the views were last recreated at. Views are recreated ONLY on a mismatch — `CREATE OR REPLACE VIEW` takes ACCESS EXCLUSIVE and would convoy every reader on a 5-minute timer |
| `backfill:<key>:cursor` | resume point of a v9 data backfill (a `messages.uuid`) |
| `backfill:<key>:done` | `"1"` once that backfill has walked the whole table |

### `sync_state`

`file_path` (PK), `file_mtime_ns` (the sync signal, `st_mtime_ns`),
`record_count`, `file_size`, `last_synced_at`.

### `model_pricing`

List prices in USD per 1M tokens. **Reference data, not session facts** — the
only non-transcript table besides `service_tier_pricing`.

| Column | Type | Null | Notes |
|---|---|---|---|
| `model_pattern` | text | no | PK. Matched `messages.model LIKE model_pattern || '%'`, **longest pattern wins** |
| `input_per_mtok` | numeric | no | base (uncached) input |
| `output_per_mtok` | numeric | no | |
| `cache_write_5m_mult` | numeric | no | default 1.25 |
| `cache_write_1h_mult` | numeric | no | default 2.0 |
| `cache_read_mult` | numeric | no | default 0.10 |
| `effective_from` | date | yes | |
| `notes` | text | yes | |

Seeded with `ON CONFLICT DO NOTHING`, so re-running `initialize()` never
clobbers a manually edited rate. **Update a rate by editing the row, not the
seed.**

| Pattern | in | out | cache read | Provenance |
|---|---|---|---|---|
| `claude-opus-5` | 5 | 25 | 0.10 | solved from `cost-state` |
| `claude-sonnet-5` | 2 | 10 | 0.10 | solved, exact on 16/16 rows |
| `claude-fable-5` | 10 | 50 | 0.10 | solved |
| `claude-fable-5-1` | 10 | 50 | **0.025** | solved, exact on 7/7 rows — $0.25/MTok, a **5.1-only** change |
| `claude-mythos-5` / `-5-1` | 10 | 50 | 0.10 / 0.025 | same tier as the Fable counterpart; not observed locally |
| `claude-opus-4-6` / `-4-7` / `-4-8` | 5 | 25 | 0.10 | correction: the generic `claude-opus-4` row prices these at 15/75 |
| `claude-opus-4` | 15 | 75 | 0.10 | Opus 4.0-4.5 |
| `claude-sonnet-4` | 3 | 15 | 0.10 | |
| `claude-haiku-4` | 1 | 5 | 0.10 | verified exactly against `cost-state` |
| `claude-3-5-haiku` | 0.80 | 4 | 0.10 | |
| `claude-3-opus` | 15 | 75 | 0.10 | |

Known limits of the flat per-model model:

- **Fast mode is not distinguishable from the transcript.** Opus 5 fast bills
  10/50 rather than 5/25, the model string is identical, and `usage.speed` is
  absent on ~36% of records. One flat rate applies; a fast-heavy session
  under-reports.
- **No >200K long-context premium** is modelled.
- **A model FALLBACK is priced wholly at the top-level model** (see
  `messages.iterations`).
- `<synthetic>` and local models (`qwen3-coder-next`) are deliberately unpriced
  and surface as `v_message_cost.unpriced`.

### `service_tier_pricing`

`service_tier` (PK, matches `messages.service_tier`), `multiplier` (scales the
whole row's cost), `notes`. Seeded: `standard` 1.0, `priority` 1.0 (same
per-token list price; committed throughput is billed separately), `batch` 0.5.

### `summary_state`

The pre-LLM gate for phase-4 roll-ups. One row per session.

| Column | Type | Null | Notes |
|---|---|---|---|
| `session_id` | text | no | PK, FK → `sessions` ON DELETE CASCADE |
| `state` | text | no | CHECK `summarized` \| `not_required` \| `pending` |
| `reason` | text | yes | CHECK `empty` \| `meta_run` \| `trivial` \| `grown` \| `natkey` |
| `kmcp_application` / `kmcp_path` | text | yes | where the summary entry lives |
| `message_count_at_summary` | integer | yes | re-eval watermark |
| `leaf_uuid_at_summary` | text | yes | re-eval watermark (preferred) |
| `decided_at` / `updated_at` | timestamptz | no | |

`summarized` is derived ONLY from rows that exist in the kmcp `entries` table,
never from a summarizer's self-report — a claimed-but-unwritten summary stays
pending and self-heals. Index: `idx_summary_state_state`.

### `summary_passes`

Append-only ledger, one row per summarization pass; also the in-flight claim.

`session_id` + `pass` (composite PK, `pass >= 1`), `application`, `path`,
`message_count_at_summary`, `leaf_uuid_at_summary`, `status` (CHECK
`in_flight` \| `written` \| `failed`), `detail`, `created_at`, `updated_at`.
Index: `idx_summary_passes_status`.

### `summarize_attempts`

Failure-isolation backoff ledger: `session_id` (PK), `attempts`,
`last_attempt_at`, `last_error`.

---

## 7. Views

`csd views` lists these; `views_version` in `metadata` gates their recreation.

| View | Answers |
|---|---|
| `v_session_overview` | one row per session, titles + aggregates + v9 context columns |
| `v_agent_children` | the Agent SPAWN ledger: tool_use ⨝ tool_result ⨝ child session |
| `v_token_usage_by_model` | tokens and cache-hit % per model |
| `v_token_by_attribution` | which skill / MCP server / agent burns tokens |
| `v_tool_usage` | tool frequency and reach |
| `v_error_summary` | every `is_error` tool_result, classified |
| `v_error_by_class` | which failure modes recur, where, how widely |
| `v_error_recovery` | each error paired with the next assistant turn |
| `v_daily_usage` | tokens per day |
| `v_compaction` | `compact_boundary` events |
| `v_project_activity` | per-project rollup (child rows excluded — parents already roll them up) |
| `v_message_cost` | **the reusable per-message cost base** |
| `v_token_cost_by_model` | cost per model, split by caching lens |
| `v_token_cost_daily` | daily spend |
| `v_session_cost_drift` | **v9** — csd's computed cost vs Claude Code's reported cost |
| `v_unsummarized` | the phase-4 work queue (pending sessions only) |

### `v_session_overview`

`v_session_overview` = `sessions` ⨝ `projects` with
`title = COALESCE(custom_title, ai_title)`. v9 added `cwd`, `current_cwd`,
`session_kind`, `forked_from_session_id`, `forked_from_uuid`,
`fork_context_length`, `worktree_session`, `reported_cost_usd` — a background
session, a fork and a relocated session were all indistinguishable from an
ordinary one before.

### `v_message_cost`

The costing base. Anthropic bills the prompt as three disjoint buckets — base
input (1x), cache writes (1.25x 5m / 2.0x 1h), cache reads (0.1x) — plus output.
Columns: `uuid`, `session_id`, `ts`, `model`, `service_tier`, the five token
buckets, `unpriced`, and `input_cost`, `cache_write_5m_cost`,
`cache_write_1h_cost`, `cache_read_cost`, `output_cost`, `total_cost`.

- An unpriced row (no `model_pricing` match) yields NULL cost terms — `sum()`
  skips them — and is counted via `unpriced`, so a rollup never silently
  under-counts.
- Writes recorded only as a lump `cache_creation` (legacy rows without the
  5m/1h split) are priced at the 5m rate, the API default TTL.
- **It sums per ROW, and rows are not unique per API response.** See below.

### `v_session_cost_drift` — schema v9

Two independent numbers that ought to agree: `computed_cost_usd` (Σ
`v_message_cost`) against `reported_cost_usd` (`cost-state.totalCostUSD`). The
view carries what is needed to ATTRIBUTE a gap, not just display one.

| Column | Meaning |
|---|---|
| `computed_cost_usd`, `reported_cost_usd`, `drift_usd`, `drift_pct` | the comparison |
| `priced_messages`, `distinct_api_messages` | row count vs distinct `api_message_id` |
| `api_message_ratio` | `priced / distinct`. **>1.0 means over-counted by about that factor** |
| `sidechain_messages` | subagent rows included in `computed` |
| `fallback_messages` | rows where a second model also billed (`iteration_count > 1`) |
| `unpriced_messages`, `has_unknown_model_cost` | coverage gaps on either side |
| `reported_model_usage` | the harness's own per-model breakdown |
| `reported_*_duration_ms`, `reported_lines_*` | the rest of the harness ledger |

Known causes of a gap, in order of measured impact:

1. **`api_message_ratio` > 1** — one API response appearing as several
   `messages` rows. Measured 1.808-2.443 on four real sessions, tracking the
   drift closely. Deduplicating `v_message_cost` by `api_message_id` is the fix
   and is deliberately **not** done yet: it changes every historical cost number
   in the archive and deserves its own change with its own verification.
2. **unpriced models** — the failure the Claude 5 seed fixed.
3. **sidechain roll-up** — subagent rows share the parent's `session_id`, so
   `computed` includes child spend; whether `cost-state` does is undocumented.
4. **fast mode** — bills higher, not detectable (see §6).
5. **model fallback** — priced wholly at the top-level model.
6. **long-context premiums** — not modelled.

A session with no `cost-state` record yet has a NULL reported side and a NULL
drift, never a fake zero.

---

## 8. Raw-only fields

Present in the archive but reachable only through JSONB. Listed so "csd does not
have it" is never confused with "csd does not have a COLUMN for it".

### Top-level record fields → `messages.raw`

| Field | On | Notes |
|---|---|---|
| `origin` | user | |
| `promptSource` | user | |
| `turnCompanion` | user | |
| `queueSkipAttachments` | user | |
| `imagePasteIds` | user | |
| `toolDenialKind` | user | |
| `interruptedMessageId` | user | |
| `mcpMeta` | user | |
| `classifierMetaLines` | user | |
| `queueOrigin` | user | |
| `isVisibleInTranscriptOnly` | user | |
| `apiBlockIndex` | assistant | |
| `isAbortedMidStream` | assistant | |
| `supersedesUuids` | assistant | |
| `errorDetails` | assistant | |
| `session_id` | both | a snake_case DUPLICATE of `sessionId`, on ~7% of records |
| `thinkingMetadata` | user | parsed into a dataclass, no column |
| `todos` | user | parsed into a dataclass, no column |
| `message.context_management` | assistant | |
| `message.container` | assistant | error records only |
| `message.stop_sequence` | assistant | |

### System-message subtypes and fields → `system_events.raw`

Subtypes with no dedicated columns: `informational`,
`model_refusal_fallback`, `model_consent_fallback`, `bridge_status`,
`away_summary`, `scheduled_task_fire`, `stop_hook_summary`.

| Field | Subtype | Notes |
|---|---|---|
| `apiRefusalCategory` | `model_refusal_fallback` | |
| `apiRefusalExplanation` | `model_refusal_fallback` | |
| `refusedUserMessageUuid` | `model_refusal_fallback` | |
| `retractedMessageUuids` | `model_refusal_fallback` | |
| `fallbackModel` / `originalModel` | `model_refusal_fallback`, `model_consent_fallback` | |
| `trigger`, `scope`, `direction` | `model_refusal_fallback` | |
| `choice`, `persistedAsDefault` | `model_consent_fallback` | |
| `pendingBackgroundAgentCount` | `turn_duration` | |
| `pendingWorkflowCount` | various | |
| `hookAdditionalContext` | `stop_hook_summary` | |
| `hookCount`, `hookErrors`, `hookInfos`, `hasOutput`, `preventedContinuation`, `stopReason`, `toolUseID` | `stop_hook_summary` | |

### Elsewhere

- **`toolUseResult`** (`tool_results.tool_use_result`) is polymorphic per tool —
  dict 45%, list 17%, string 3%. Stored as a blob **by design**; do not
  normalize per tool.
- **`attachment`** (`attachments.attachment`) is variable-shape per
  `attachment.type`.
- **`usage`** keeps every field, including the ones not promoted to columns.
- **`cost_state`** keeps `totalAPIDurationWithoutRetries`, `startTime` and the
  whole `modelUsage` object.

---

## 9. Migration history

Schema versions 1-2 belong to the retired SQLite generations (Gen1/Gen2,
`database.py` — superseded 2026-06-01). The Postgres archive starts at v3.

| Version | Date | Commit | What it added |
|---|---|---|---|
| **1-2** | — | — | Gen1/Gen2 SQLite. Retired; not upgradable to this schema. |
| **3** | 2026-06-01 | `5eb585b` | Gen3 Postgres archive: `projects`, `sessions`, `messages`, `content_blocks`, `tool_results`, `attachments`, `system_events`, `file_history`, `file_backups`, `queue_operations`, `pr_links`, `agent_tasks`, `sync_state`, `metadata`; the first analytic views. |
| **4** | 2026-06-03 | `300a987` | Pricing reference data (`model_pricing`, `service_tier_pricing`) and the token-cost views (`v_message_cost`, `v_token_cost_by_model`, `v_token_cost_daily`) — the caching lens. |
| **5** | 2026-06-09 | `49cd5c5` | `summary_state`, the pre-LLM phase-4 gate, and `v_unsummarized`. |
| **6** | 2026-07-17 | `3c971d1` | Subagent visibility: child session rows keyed `<parent>:<agent>`, `idx_messages_agent`. |
| **7** | 2026-07-17 | `26d10f7` | `own_*` aggregate columns (main-chain vs roll-up) and `v_agent_children`, the Agent spawn ledger. |
| **8** | 2026-08-20 | `5a2d462` | `summary_passes` — the per-pass ledger and in-flight claim for repeatable (delta) summarization. |
| **9** | 2026-09-02 | this release | The Claude Code v2.1.161-258 impact release. See below. |

### What v9 added

New table `session_records` (the catch-all + tripwire) and view
`v_session_cost_drift`.

`sessions` — `forked_from_session_id`, `forked_from_uuid`,
`fork_context_length`, `fork_agent_id`, `current_cwd`, `worktree_session`,
`cost_state`, `reported_cost_usd`, `reported_total_duration_ms`,
`reported_api_duration_ms`, `reported_tool_duration_ms`, `reported_lines_added`,
`reported_lines_removed`, `has_unknown_model_cost`, `session_kind`.

`messages` — `effort`, `session_kind`, `thinking_tokens`, `server_tool_use`,
`iterations`, `iteration_count`.

`content_blocks` — `block_payload`.

`model_pricing` — the Claude 5 family (opus-5, sonnet-5, fable-5, fable-5-1,
mythos-5, mythos-5-1) and the Opus 4.6/4.7/4.8 correction.

Corrections to existing data: `messages.message_type` (list-content prompts
relabelled from `tool_result` to `prompt`) and `messages.prompt_text` (filled
for those rows).

### Migration discipline

Every migration in this schema is **additive, idempotent and guarded**:

- Tables and indexes use `CREATE ... IF NOT EXISTS` and run on every
  `initialize()`, so the schema self-heals.
- Column additions live in a `DO $$ … IF NOT EXISTS (SELECT 1 FROM
  information_schema.columns …) $$` block keyed on the FIRST new column, so the
  ACCESS EXCLUSIVE `ALTER` fires exactly once and not on every 5-minute sweep
  tick. `ADD COLUMN` without a default is O(1) in PG11+, so no heap rewrite.
- **No column is ever dropped or retyped, and no row is ever deleted.** A field
  Claude Code stops emitting becomes LEGACY, not absent.
- Views are recreated only when `views_version` lags `SCHEMA_VERSION`. A view
  whose column list can grow is `DROP`ped first — `CREATE OR REPLACE VIEW`
  cannot add a column and fails with *cannot change name of view column*.
- **Data backfills are bounded, resumable and self-committing**
  (`postgres.BACKFILLS`, `SessionArchive.run_backfills`). Each walks the
  messages primary key in 20K-row committed batches with a cursor in `metadata`,
  spends at most 20s per `initialize()` call, resumes on the next sweep, is
  `IS DISTINCT FROM`-guarded so a re-run writes nothing, and isolates its own
  failures so a broken backfill can never stop ingest. A single long `UPDATE`
  would be exactly the `idle in transaction` shape that once convoyed this
  database for ~9 hours.

> **Generated columns are deliberately not used.** Postgres 16 supports only
> STORED generated columns, and adding one rewrites the entire table — a
> multi-GB ACCESS EXCLUSIVE rewrite of 1.3M `messages` rows inside a sweep tick.

---

## Appendix A: JSONL field census (2026-06-01)

The earlier frequency audit, kept for provenance. **Re-audited 2026-06-01**
against **266,939 JSONL records across 1,450 files** (623 main + 827 subagent),
Claude Code `2.1.123`-`2.1.158`. Where it disagrees with sections 1-9 above, the
sections above win — they were written against Claude Code v2.1.161-2.1.258 and
the live catalog.

Known drift since this census: `stop_hook_summary` has **returned** (recorded
here as gone); `progress` and `summary` remain gone; the eleven record types in
§5 and the tool families in `tool_labels.py` all postdate it.

### Record type distribution (2026-06-01)

| Type | Count | % |
|---|---|---|
| `assistant` | 113,741 | 42.6% |
| `user` | 75,103 | 28.1% |
| `attachment` | 19,929 | 7.5% |
| `last-prompt` | 11,831 | 4.4% |
| `ai-title` | 10,580 | 4.0% |
| `permission-mode` | 8,148 | 3.1% |
| `system` | 7,718 | 2.9% |
| `file-history-snapshot` | 6,704 | 2.5% |
| `bridge-session` | 4,461 | 1.7% |
| `queue-operation` | 3,823 | 1.4% |
| `mode` | 3,525 | 1.3% |
| `pr-link` | 462 | 0.2% |
| `agent-name` | 333 | 0.1% |
| `started` | 254 | 0.1% |
| `result` | 254 | 0.1% |
| `custom-title` | 73 | 0.0% |
| `progress` | 0 | GONE (was 43.5%) |
| `summary` | 0 | GONE → `ai-title` |

### Record type distribution (2026-09-02, 30-day window, main files only)

| Type | Count | Modelled as |
|---|---|---|
| `assistant` | 62,837 | `messages` |
| `user` | 37,786 | `messages` |
| `attachment` | 35,803 | `attachments` |
| `last-prompt` | 10,889 | `sessions` |
| `mode` | 10,711 | `sessions` |
| `system` | 10,178 | `system_events` |
| `permission-mode` | 9,917 | `sessions` |
| `ai-title` | 9,637 | `sessions` |
| `queue-operation` | 8,573 | `queue_operations` |
| `atis-latch` | 6,077 | **`session_records`** |
| `bridge-session` | 4,069 | `sessions` |
| `pr-link` | 3,601 | `pr_links` |
| `file-history-snapshot` | 3,532 | `file_history` |
| `worktree-state` | 816 | **`session_records`** |
| `relocated` | 733 | **`session_records`** |
| `file-history-delta` | 638 | **`session_records`** |
| `agent-name` | 308 | `sessions` |
| `history-suppression` | 291 | **`session_records`** |
| `frame-link` | 241 | **`session_records`** |
| `cost-state` | 107 | **`session_records`** |
| `artifact-autoreact-ledger` | 56 | **`session_records`** |
| `artifact-comment-monitor` | 20 | **`session_records`** |
| `custom-title` | 4 | `sessions` |
| `fork-context-ref` | 8 | **`session_records`** (sidechain files only) |

### Models observed (30-day window, assistant records)

`claude-opus-5` 165,910 · `claude-fable-5` 54,588 · `claude-sonnet-5` 10,713 ·
`claude-fable-5-1` 3,333 · `claude-opus-4-8` 2,668 ·
`claude-haiku-4-5-20251001` 2,046 · `<synthetic>` 109.

### Content-block types (30-day window)

`tool_use` 134,494 · `thinking` 73,827 · `text` 31,076 · `fallback` 4.

### Field frequency on `user` records (2026-06-01)

`message` 100% · `promptId` 100% · `sourceToolAssistantUUID` 89.4% ·
`toolUseResult` 64.6% · `agentId` 26.6% · `permissionMode` 6.0% · `slug` 5.4% ·
`isMeta` 1.9% · `origin` 1.1% · `mcpMeta` 0.3% · `imagePasteIds` 0.2% ·
`sourceToolUseID` 0.2% · `forkedFrom` 0.1% · `interruptedMessageId` 0.1% ·
`sessionKind` 0.0%.

### Field frequency on `assistant` records (2026-06-01)

`message` 100% · `requestId` 100% · `agentId` 25.2% · `attributionAgent` 25.2% ·
`attributionSkill` 20.5% · `attributionMcpServer` 13.1% ·
`attributionMcpTool` 13.1% · `attributionPlugin` 0.2% · `slug` 5.4% ·
`forkedFrom` 0.1% · error fields ~0.1%.

### Field frequency on `assistant` records (2026-09-02, 239,367 records)

`effort` 98.5% · `agentId` 73.7% · `attributionAgent` 73.7% ·
`attributionMcpServer` 16.1% · `attributionMcpTool` 16.1% ·
`attributionSkill` 12.3% · `apiBlockIndex` 5.6% · `sessionKind` 2.0% ·
`isApiErrorMessage` 0.05%.

### `message.usage` field frequency (2026-09-02, 239,367 records)

`input_tokens` / `output_tokens` / `cache_creation_input_tokens` /
`cache_read_input_tokens` / `cache_creation` / `service_tier` /
`inference_geo` 100% · `server_tool_use` / `iterations` / `speed` 63.9% ·
`output_tokens_details` 55.3% (`thinking_tokens` on 99.9% of those).
