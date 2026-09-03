# Claude Code statusline

`statusline-command.sh` is the custom two-row statusline for Claude Code. It
reads the harness JSON on stdin and shells out to `git` for branch/worktree
state. It lives here because it reports the same `usage` shapes that this
repo's archive plane ingests — it is the live, single-message complement to the
Postgres cost views.

Verified against **Claude Code v2.1.259** (2026-09).

## What it shows

```
Opus 5  ~/GitHub/knowledge  ⎇ fix/foo ⧉ ±3 ↑1
316k (314k cached · exp 08:02 · 91% hit 2 miss) / 1000k  high  (9c799c1b)
```

- **Row 1** — model, cwd, and a compact git segment: branch, `⧉` linked-worktree
  marker, `±N` dirty count, `↑/↓` ahead/behind. Omitted outside a repo.
- **Row 2** — context budget `total (cache detail) / window`, then effort or
  output style, then the session id.

Row 2 is colored by **percentage of the actual window**, not by an absolute
token count: green `<75%`, yellow `<90%`, red `≥90%`. (Before the 2026-09 fix
the thresholds were the absolute 150k/200k marks, which pinned every 1M-context
session to red.)

### Cache detail

Everything inside the parentheses comes from the payload and each piece is
independently optional:

| Piece | Source | Shown when |
|---|---|---|
| `314k cached` | `context_window.current_usage.cache_read_input_tokens` | non-zero |
| `exp 08:02` | `prompt_cache.expires_at` | `prompt_cache.warm == true` |
| `cold` | `prompt_cache.warm == false` | `prompt_cache` present |
| `91% hit` | `prompt_cache.hit_ratio` | non-null |
| `2 miss` | `prompt_cache.misses` | `> 0` |

`exp HH:MM` is the local clock time the warm prefix leaves its TTL. Claude Code
re-runs the status line when a warm cache's `expires_at` passes, so the segment
flips to `cold` on its own. It is green normally and yellow inside the last
minute. A frozen `exp` time is your hard deadline before the prefix is evicted.

When there is no context signal at all (no `context_window`, unparseable
payload) the budget renders `—k / 200k` rather than a misleading `0k`.

## Input contract

Claude Code pipes one JSON object to the command on stdin per refresh —
[docs](https://code.claude.com/docs/en/statusline). The fields this script
reads, and their absence rules:

| Field | Notes |
|---|---|
| `model.id`, `model.display_name` | `display_name` may carry a `(1M context)` suffix, which is stripped |
| `workspace.current_dir` (falls back to `cwd`) | |
| `session_id` | |
| `context_window.context_window_size` | **Authoritative** window size: `200000`, or `1000000` for extended-context models. Never infer this from the model id when the field is present |
| `context_window.total_input_tokens` | Tokens currently in the window (input + cache read + cache write). `0` before the first API response |
| `context_window.current_usage` | `null` before the first API call and again after `/compact` until the next response |
| `context_window.used_percentage` | Pre-computed; **may be `null` early in a session**. Input-only basis: `input + cache_creation + cache_read`, output excluded |
| `prompt_cache` | v2.1.251+. **Absent until the main conversation's first API response.** `warm`, `ttl`, `expires_at` (epoch s, `null` when cold), `hit_ratio` (`null` while all counts are zero), `misses`, `requests`, `cache_write_tokens`, … Subagent requests are not counted |
| `effort.level` | Absent when the model has no effort parameter |
| `output_style.name` | |
| `transcript_path` | Only read on the legacy fallback path (see below) |

Fields the script deliberately does **not** use: `cost.*`, `rate_limits.*`
(incl. `spend_limit`, v2.1.251+), `exceeds_200k_tokens`, `pr.*`, `vim.*`,
`worktree.*`, `workspace.repo.*`, `fast_mode`, `thinking.enabled`,
`session_name`, `prompt_id`. Samples in `samples/` carry them so a future
change can render them without re-deriving the contract.

### Deriving the budget

In order, first hit wins:

1. `context_window.total_input_tokens`
2. `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` from
   `current_usage` — the same input-only formula Claude Code uses, so the
   derived percentage matches `used_percentage`
3. `used_percentage / 100 × context_window_size`
4. **Legacy only** (payload has no `context_window` at all): the last
   `usage` block in the last 400 lines of `transcript_path`

## Why it is shaped this way

Three constraints drove the 2026-09 rewrite; breaking any of them is what made
the context/cache area render intermittently or wrongly.

1. **Speed.** Claude Code debounces status-line updates at 300ms and
   **cancels an in-flight script when the next update triggers**. A slow script
   therefore shows stale content or nothing. The old version grepped the entire
   transcript twice per render — 649ms on a 37 MB JSONL, and worse as the
   session grows. Everything it was mining from the transcript is now in the
   payload; the current version is ~130ms, dominated by one `git status`.
2. **One `jq` pass.** The payload is parsed once into a `\037`-joined row.
   Not tab-joined: tab is IFS whitespace, so bash collapses runs of tabs and a
   single absent field silently shifts every field after it.
3. **Independent degradation.** Every lookup is null-guarded inside jq, and each
   segment is appended only when its source is present. No missing field can
   blank the line, and nothing writes to stderr.

## Tests

```
./test_statusline.sh          # assert every sample
./test_statusline.sh --show   # render every sample (eyeball / diff versions)
SL_SCRIPT=/other/statusline.sh ./test_statusline.sh --show   # compare versions
```

`samples/` holds eleven payloads covering the shapes the contract allows:
no `context_window`; first turn with `null`s; a normal 200k turn with a warm 5m
cache; a 1M window with a warm 1h cache; a near-full 1M window; post-`/compact`
(`current_usage` null, cache cold); `used_percentage` absent with
`current_usage` present; a resumed session with no `prompt_cache` yet;
a legacy payload resolved from `samples/transcript-fixture.jsonl`; a full
payload with `rate_limits.spend_limit`; and a truncated, unparseable payload.

Assertions run against the last output row with ANSI stripped, because row 1
carries live git state. Each sample also asserts exit 0, empty stderr, 1–2 rows,
and a 250ms wall-clock budget.

## Wiring

```json
{
  "statusLine": {
    "type": "command",
    "command": "bash /path/to/claude-session-db/statusline/statusline-command.sh"
  }
}
```

Add `"refreshInterval": <seconds>` if you want the line to re-run on a timer as
well as on events; the event triggers go quiet while the main session is idle
(e.g. waiting on background subagents).

## Dependencies

`bash`, `jq`, `git`, `awk`, `date`. `date` conversion tries BSD/macOS
`date -r` first and falls back to GNU `date -d @…`, so it works on macOS and
Linux.

## Version notes

| Claude Code | Change | Effect here |
|---|---|---|
| v2.0.65 | `context_window` added to the payload | Basis for the budget; the pre-2.0.65 transcript path is now fallback-only |
| v2.0.70 | `current_usage` added | Cache-read split without touching the transcript |
| v2.1.6 | `used_percentage` / `remaining_percentage` added | |
| v2.1.119 | `effort.level`, `thinking.enabled` added | Effort label |
| v2.1.132 | `total_input_tokens` / `total_output_tokens` became **current-turn**, not cumulative | Makes them the right budget source |
| v2.1.141 | Multi-line statusline row corruption fixed | Two-row layout is safe |
| v2.1.145 | Repo/PR info added to the payload | Unused |
| v2.1.196 | `prompt_id` added | Unused |
| v2.1.211 | `cost.total_cost_usd` resets on `/clear` | Unused |
| v2.1.251 | **`prompt_cache` object** and `rate_limits.spend_limit` added | Replaces the transcript-derived TTL/expiry guesswork entirely |
