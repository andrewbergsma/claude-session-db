# Claude Code Session Data Model

Field-level reference for the data produced by Claude Code sessions.

**Re-audited 2026-06-01** against **266,939 JSONL records across 1,450 files**
(623 main sessions + 827 subagents) under `~/.claude/projects`, CC versions
`2.1.123`–`2.1.158`. This supersedes the Feb-2026 audit, which had drifted
badly (`progress` 43.5%→0%, `summary`→`ai-title`, a new `attribution*` system,
`stop_hook_summary` gone, and 11 new record types).

---

## File System Layout

```
~/.claude/
    projects/
        <project-slug>/                         # e.g., -Users-andrew-GitHub-knowledge
            sessions-index.json                  # Metadata index — only 30/~130 projects (DO NOT rely on)
            <session-uuid>.jsonl                 # Main session transcript (623 files)
            <session-uuid>/
                subagents/
                    agent-<hex>.jsonl             # Subagent transcripts (827 files)
                tool-results/
                    <id>.txt                      # Overflow tool results (150 dirs)
```

### Project Slug Encoding
Leading `-` is `/`, subsequent `-` are `/`:
`-Users-andrew-GitHub-knowledge` → `/Users/andrew/GitHub/knowledge`.

### Sync signal
**Glob `**/*.jsonl` + filesystem mtime.** `sessions-index.json` covers <25% of
projects and is stale — it is NOT the source of truth. Subagent files are never
indexed.

---

## Record Type Distribution (current)

| Type | Count | % | Notes vs Feb |
|---|---|---|---|
| `assistant` | 113,741 | 42.6% | — |
| `user` | 75,103 | 28.1% | — |
| `attachment` | 19,929 | 7.5% | **NEW** |
| `last-prompt` | 11,831 | 4.4% | **NEW** |
| `ai-title` | 10,580 | 4.0% | **NEW** (replaces `summary`) |
| `permission-mode` | 8,148 | 3.1% | **NEW** |
| `system` | 7,718 | 2.9% | subtypes changed |
| `file-history-snapshot` | 6,704 | 2.5% | — |
| `bridge-session` | 4,461 | 1.7% | **NEW** (claude.ai web bridge) |
| `queue-operation` | 3,823 | 1.4% | — |
| `mode` | 3,525 | 1.3% | **NEW** |
| `pr-link` | 462 | 0.2% | **NEW** |
| `agent-name` | 333 | 0.1% | **NEW** |
| `started` | 254 | 0.1% | **NEW** (agent lifecycle) |
| `result` | 254 | 0.1% | **NEW** (agent lifecycle) |
| `custom-title` | 73 | 0.0% | now more common |
| `progress` | **0** | 0% | **GONE** (was 43.5% / 142,908) |
| `summary` | **0** | 0% | **GONE** → `ai-title` |

Models: `claude-opus-4-7` (93k), `claude-opus-4-8` (15k), `claude-sonnet-4-6`
(3.9k), `claude-haiku-4-5` (1.7k), `<synthetic>` (87), `qwen3-coder-next` (4).

---

## Universal fields (on conversation records: user/assistant/system/attachment)

| Field | Freq | Notes |
|---|---|---|
| `type`, `uuid`, `sessionId`, `timestamp`, `parentUuid` | 100% | core |
| `cwd`, `gitBranch`, `version`, `userType`, `isSidechain` | 100% | context |
| `entrypoint` | 100% | **NEW** — `"cli"`, etc. |

---

## Record Type: `user` (n=75,103)

| Field | Freq | Description |
|---|---|---|
| `message` | 100% | `{role, content}`; content is string (prompt) or array (tool_result/image) |
| `promptId` | 100% | **NEW** — stable prompt identifier |
| `sourceToolAssistantUUID` | 89.4% | **HIGH** — assistant msg whose tool_use this answers |
| `toolUseResult` | 64.6% | client-side structured result (dict 45.2% / list 16.5% / str 2.9%) |
| `agentId` | 26.6% | subagent hex id (sidechain only) |
| `permissionMode` | 6.0% | `bypassPermissions`/`default`/`plan` |
| `slug` | 5.4% | session slug → `~/.claude/plans/<slug>.md` |
| `isMeta` | 1.9% | system-injected user message |
| `origin` | 1.1% | **NEW** |
| `mcpMeta` | 0.3% | **NEW** |
| `imagePasteIds` | 0.2% | |
| `sourceToolUseID` | 0.2% | tool_use id that produced this result |
| `forkedFrom` | 0.1% | `{sessionId, messageUuid}` |
| `interruptedMessageId` | 0.1% | **NEW** |
| `sessionKind` | 0.0% | **NEW** |
| `isVisibleInTranscriptOnly`, `isCompactSummary` | 0.0% | |

`message.content[]` tool_result content is **string (72%) or array (28%)** — no
top-level dict shape. (Structured per-tool data lives in `toolUseResult`.)

---

## Record Type: `assistant` (n=113,741)

| Field | Freq | Description |
|---|---|---|
| `message` | 100% | Anthropic message object (see below) |
| `requestId` | 100% | API request id |
| `agentId` | 25.2% | subagent hex id |
| `attributionAgent` | 25.2% | **NEW** — agent that produced this msg |
| `attributionSkill` | 20.5% | **NEW** — skill name (e.g. `session-summary`) |
| `attributionMcpServer` | 13.1% | **NEW** |
| `attributionMcpTool` | 13.1% | **NEW** |
| `attributionPlugin` | 0.2% | **NEW** |
| `slug` | 5.4% | |
| `forkedFrom` | 0.1% | |
| `isApiErrorMessage` / `error` / `apiErrorStatus` | ~0.1% | synthetic error records |

### `message` (assistant)
| Field | Freq | Description |
|---|---|---|
| `id`, `type`, `role`, `model`, `content`, `usage` | 100% | |
| `stop_reason`, `stop_sequence` | 100% | |
| `stop_details` | 100% | **NEW** |
| `diagnostics` | 89.0% | **NEW** |
| `context_management` | 0.1% | rare |
| `container` | 0.1% | error records only |

### `message.usage` — capture FULL object (token-economics goldmine)
| Field | Freq |
|---|---|
| `input_tokens`, `output_tokens` | 100% |
| `cache_creation_input_tokens`, `cache_read_input_tokens` | 100% |
| `cache_creation` (`{ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}`) | 100% |
| `service_tier`, `inference_geo` | 100% |
| `iterations` | 83.6% |
| `server_tool_use`, `speed` | 83.6% |

### `message.content[]` blocks
- `thinking` `{thinking, signature}`
- `text` `{text}`
- `tool_use` `{id, name, input, caller?}`

Top tools: `Bash` (28k), `Read` (6.6k), `Edit` (3.9k), kmcp tools, `TaskUpdate`,
`Write`, `ToolSearch`, `TaskCreate`, `AskUserQuestion`, `WebFetch`, `WebSearch`,
`Agent`, `Skill`. MCP tools are `mcp__<server>__<tool>`.

---

## Record Type: `system` (n=7,718)

Common: `subtype`, `isMeta` (97.4%), universal fields. Subtype distribution:

| subtype | Count | % | Key fields | Notes vs Feb |
|---|---|---|---|---|
| `turn_duration` | 4,878 | 63.2% | `durationMs`, `messageCount` (**NEW**) | |
| `away_summary` | 1,932 | 25.0% | `content` | **NEW** |
| `local_command` | 242 | 3.1% | `content`, `level` | |
| `bridge_status` | 220 | 2.9% | `content`, `url` | **NEW** |
| `scheduled_task_fire` | 203 | 2.6% | `content` | **NEW** |
| `api_error` | 199 | 2.6% | `error`, `retryInMs`, `retryAttempt`, `maxRetries`, `cause` | |
| `informational` | 35 | 0.5% | `content`, `level` | **NEW** |
| `compact_boundary` | 9 | 0.1% | `compactMetadata` (`{preTokens, trigger,...}`), `logicalParentUuid` | |
| `stop_hook_summary` | **0** | — | — | **GONE** (was 35.8%) |
| `microcompact_boundary` | **0** | — | — | **GONE** |

Also seen: `pendingBackgroundAgentCount`, `pendingWorkflowCount` on some records.

---

## Record Type: `attachment` (n=19,929) — NEW

Conversation-flow record (has `uuid`/`parentUuid`/`timestamp` + universal fields)
carrying an injected context attachment.

| Field | Description |
|---|---|
| `attachment.type` | e.g. `deferred_tools_delta` (ToolSearch tool additions), etc. |
| `attachment.*` | type-specific payload (e.g. `addedNames[]`) |

Variable shape — store the `attachment` object as JSONB.

---

## Session-scoped metadata records (sessionId-keyed, latest-wins)

These are lightweight, NOT in the conversation thread. Fold into `sessions`
columns or a small key/value table.

| Type | Fields | Meaning |
|---|---|---|
| `ai-title` | `{aiTitle, sessionId}` | AI-generated session title (replaces `summary`) |
| `custom-title` | `{customTitle, sessionId}` | user-assigned title |
| `last-prompt` | `{lastPrompt, leafUuid, sessionId}` | most recent user prompt (resume marker) |
| `permission-mode` | `{permissionMode, sessionId}` | session permission mode |
| `mode` | `{mode, sessionId}` | session mode (`normal`, ...) |
| `bridge-session` | `{bridgeSessionId, lastSequenceNum, sessionId}` | claude.ai web-bridge link |
| `agent-name` | `{agentName, sessionId}` | human name for a subagent session |
| `pr-link` | `{prNumber, prUrl, prRepository, timestamp, sessionId}` | PR opened during session |

---

## Agent-lifecycle records — NEW

| Type | Fields | Meaning |
|---|---|---|
| `started` | `{key, agentId}` | agent task start marker (`key` = `v2:<sha256>`) |
| `result` | `{key, agentId, result}` | structured agent result (`result` is arbitrary JSON) |

Store `result.result` as JSONB.

---

## Record Type: `file-history-snapshot` (n=6,704)

`{messageId, isSnapshotUpdate, snapshot:{messageId, timestamp, trackedFileBackups}}`.
`trackedFileBackups` is `{<filepath>: {backupFileName, backupTime, version}}`.

## Record Type: `queue-operation` (n=3,823)
`{operation, timestamp, sessionId, content?}` — `content` present 50% (enqueue).

---

## `toolUseResult` (client-side enrichment, on `user` records)

Polymorphic per-tool. Shapes: dict (45.2%), list (16.5%), str (2.9% — error
strings). **v1: store as JSONB blob, do not normalize per-tool.** The raw
verbatim result also lives in `message.content[].tool_result`.

## Overflow files: `<session>/tool-results/<id>.txt`
Full tool output when it exceeds the inline cap. Filename stem matches
`tool_use_id`. 150 session dirs have these — wire them in so the largest results
are stored verbatim, not truncated.

## Subagent files: `<session>/subagents/agent-<hex>.jsonl`
Same record format; `isSidechain=true`; `agentId` matches filename hex; `slug`
links to `~/.claude/plans/<slug>.md`.
