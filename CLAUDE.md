# claude-session-db (csd)

The lossless Postgres archive of Claude Code session transcripts, plus the web
console, the turn-angles miner and the summarization pipeline that ride on it.

## Knowledge apps

- `claude_session_db` — everything csd. Start at the switchboard: `claude_session_db:overview`
  - `claude_session_db:command/csd` — every subcommand, the column cheat-sheet, the operational gotchas
  - `claude_session_db:design/claude-session-db-postgres-archive` — the Gen3 spec
  - `claude_session_db:reference/console-surfaces` — console, angles, CR, repos lens, permission envelope, curation
  - `claude_session_db:reference/archive-operations` — summarize, sweep, session-mgmt lens, subagents, `csd usage`
  - `claude_session_db:agent/steward` — curator for this corpus
- `claudecode` — Claude Code harness behaviour + the session-management hub;
  `knowledge_mcp` / `knowledge_mcp_code` — the base the summarizer writes into

In-tree by exception: [`DATA_MODEL.md`](DATA_MODEL.md) is the schema authority
(read before touching the schema); [`CHANGELOG.md`](CHANGELOG.md) is the ship log.

## Commands

```bash
pip install -e .              # csd is a zsh function, not a PATH binary
csd ingest [--rebuild|--force] ; csd stats ; csd recent ; csd query "SQL"
csd sweep-health ; csd summarize-health          # DB-free watchers
csd digest REF ; csd summary-scope REF           # the /session-summary seam
csd digest REF --cr [--since TS] [--out F]      # the summarizer's CR cut; --row ID expands a stub
csd angles [--session ID] ; csd console          # miner + the web UI (:4462)
pytest                                           # tests
```

`csd query` goes through psycopg `execute()`: a literal `%` breaks it — use
`starts_with()`/`strpos()`, or escape as `%%`.

## Conventions

- `claude_session_db/__init__.py:__version__` is the one source of truth.
  **Bump it and add its `CHANGELOG.md` entry in the same commit** — minor for a
  feature batch, patch for fixes/docs, major for an archive generation. The
  console's version chip goes amber when the running process is older than HEAD
  on disk; restart it.
- Never write the `knowledge` tables. Transcripts are telemetry — cross-link by
  `session_id` only.
