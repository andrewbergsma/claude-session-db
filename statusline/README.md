# Claude Code statusline

`statusline-command.sh` is the custom two-row statusline for Claude Code. It
reads the harness JSON on stdin (model, cwd, session id, transcript path) and
shells out to `git` for branch/worktree state. It lives here because it parses
the same per-message `usage` blocks that this repo's archive plane ingests — it
is the live, single-message complement to the Postgres cost views.

## What it shows

```
Opus 4.8  ~/GitHub/knowledge  ⎇ fix/foo ⧉ ±3 ↑1
316k (314k cached · exp 08:02) / 1000k  Explanatory  (9c799c1b)
```

- **Row 1** — model, cwd, and a compact git segment: branch, `⧉` linked-worktree
  marker, `±N` dirty count, `↑/↓` ahead/behind.
- **Row 2** — context budget `total (cached) / window`, usage-colored
  (green <150k, yellow <200k, red ≥200k), then effort/output-style and session id.

### Cache expiry (`exp HH:MM`)

The API never returns a cache expiry, so the script derives it: the prompt cache
lives `TTL` seconds past the **last request**, and that TTL **refreshes on every
turn**. So `expiry = last assistant-message timestamp + TTL`.

The TTL is not assumed — it is read from the transcript. Each `usage` block
splits cache writes into `cache_creation.ephemeral_5m_input_tokens` and
`ephemeral_1h_input_tokens`; the script scans recent assistant lines for the
most recent non-zero bucket to learn whether the active cache is **5m** (300s)
or **1h** (3600s), defaulting to 5m if no write signal is seen. The expiry is
then rendered as a local clock time:

- warm → `exp 08:02` (green; yellow under a minute out)
- past TTL → `cold` (red)

Because the anchor slides forward each turn, the displayed time jumps forward
when you send a message, then holds steady while you read or think — a frozen
time is your hard deadline before the warm prefix is evicted.

## Wiring

Point `~/.claude/settings.json` at this script:

```json
{
  "statusLine": {
    "type": "command",
    "command": "bash /Users/me/GitHub/claude-session-db/statusline/statusline-command.sh"
  }
}
```

The previous home was a loose `~/.claude/statusline-command.sh`; repoint
settings.json here after this branch merges so the repo copy is the source of
truth (mirrors the `transcript_analyzer.py` relocation in #71).

## Dependencies

`bash`, `jq`, `git`, and `date`. Timestamp parsing tries BSD/macOS
`date -j -f` first and falls back to GNU `date -d`, so it works on macOS and
Linux.
