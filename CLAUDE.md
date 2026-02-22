# CLAUDE.md — claude-session-db

## Overview

Standalone CLI tool that parses Claude Code session JSONL transcripts into a SQLite database for analysis with VisiData.

**Database**: `~/.local/share/claude-session-db/sessions.db`
**CLI**: `csd` (claude-session-db)

## Commands

```bash
csd ingest              # Incremental sync (mtime-based)
csd ingest --rebuild    # Drop and rebuild from scratch
csd ingest --force      # Force re-sync all files
csd open                # Launch VisiData on the database
csd stats               # Table counts, db size
csd recent [N]          # Most recent sessions
csd query "SQL"         # Ad-hoc SQL query
csd views               # List available views
csd db-path             # Print database path
```

## Architecture

- `jsonl_records.py` — JSONL record parsing (dataclasses, zero dependencies)
- `sessions_index.py` — sessions-index.json parser
- `database.py` — SQLite schema DDL and operations
- `sync.py` — Incremental sync engine with subagent discovery
- `subagent.py` — Subagent/sidechain file discovery
- `cli.py` — Click CLI
- `visidata_plugin.py` — SQLite-backed VisiData sheets

## VisiData Plugin

Installed as symlink: `~/.config/visidata/plugins/csd.py`

Keybindings: `gc` (project sessions), `gC` (all projects)

Drill-down: Projects → Sessions → Messages → Content/Tool Results
