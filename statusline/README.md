# Claude Code statusline

`statusline-command.sh` is the custom three-row statusline for Claude Code. It
reads the harness JSON on stdin and shells out to `git` for branch/worktree
state. It lives here because it reports the same `usage` shapes that this
repo's archive plane ingests — it is the live, single-message complement to the
Postgres cost views.

Verified against **Claude Code v2.1.259** (2026-09).

## What it shows

```
Opus 5  ~/GitHub/knowledge  ⎇ fix/foo ⧉ ±3 ↑1
316k / 1000k  R 41.2M · W 352k · exp 08:02 · 91% hit 2 miss  high
9c799c1b-1107-4c08-a4eb-32faf6a39b5e  start Sep 10 06:16 · last prompt Sep 10 09:41
```

- **Row 1** — model, cwd, and a compact git segment: branch, `⧉` linked-worktree
  marker, `±N` dirty count, `↑/↓` ahead/behind. Omitted outside a repo.
- **Row 2** — context budget `in-window / window`, then the cache segment,
  then effort or output style.
- **Row 3** — the full session id, then the session's start time and the time
  of the last prompt you typed, in local time. Omitted when the payload has no
  session id; the times are omitted until the transcript has been read.

The budget is colored by **percentage of the actual window**, not by an
absolute token count: green `<75%`, yellow `<90%`, red `≥90%`. (Before the
2026-09 fix the thresholds were the absolute 150k/200k marks, which pinned
every 1M-context session to red.)

### Session row times

Neither time is in the payload; both come from the same incremental transcript
pass as the cache totals (below).

- **start** — the first transcript line carrying a `timestamp`. A resumed
  session keeps its original start; `/clear` starts a new session.
- **last prompt** — the latest line that is a prompt you typed: a `user` line
  tagged `origin.kind == "human"`, or a `queued_command` attachment (a message
  typed mid-turn). Tool results, task notifications, auto-continuations,
  `isMeta` lines and sidechain (subagent) prompts don't count. Transcripts
  that predate `origin` fall back to: not `isMeta`, not a tool result, not an
  injected `<local-command-…>` / `<task-notification>` / `[CR…]` message.

### Cache segment

Each piece is independently optional:

| Piece | Source | Shown when |
|---|---|---|
| `R 41.2M` | Session-total `cache_read_input_tokens`, summed from the transcript (see below) | non-zero |
| `W 352k` | Session-total `cache_creation_input_tokens`, same sum; falls back to `prompt_cache.cache_write_tokens` when there is no transcript | non-zero |
| `exp 08:02` | `prompt_cache.expires_at` | `prompt_cache.warm == true` |
| `cold` | `prompt_cache.warm == false` | `prompt_cache` present |
| `91% hit` | `prompt_cache.hit_ratio` | non-null |
| `2 miss` | `prompt_cache.misses` | `> 0` |

`R`/`W` are cumulative for the session's main thread — every request's cache
reads and writes, not the current turn's. They are the two sides of the cache
bill: reads at ~0.1× input price, writes at 1.25× (5m) or 2× (1h). Numbers
scale `950` → `352k` → `41.2M` → `1.2B`.

#### How the totals are summed

The payload has a cumulative write count (`prompt_cache.cache_write_tokens`)
but **no cumulative read count**, and `current_usage` is only the last request.
Status-line runs are debounced and cancelled, so accumulating `current_usage`
per render would miss requests. The totals are therefore summed from
`transcript_path`, incrementally:

- A state file `$SL_STATE_DIR/<session_id>.tot` (default
  `${TMPDIR:-/tmp}/claude-statusline`) holds a layout version, the transcript
  path, the byte offset already summed, the running sums, the last message id,
  and the start / last-prompt epochs. Each render reads only the bytes appended
  since — typically a few KB. A state file in an older layout is discarded and
  the transcript rescanned once.
- Lines are de-duplicated by `message.id` (the transcript repeats a message's
  usage on every content-block line; last one wins, across chunk boundaries).
  `isSidechain` lines are skipped, matching `prompt_cache`'s main-thread scope.
  A trailing line still being written is left for the next render.
- A resumed session with a large transcript catches up in `SL_TAIL_CAP`-byte
  chunks (default 16 MB, ~120ms each), one per render. Until it has caught up
  the totals carry a trailing `+` (`R 12.1M+`), meaning "at least".
- A different transcript path, or a file that shrank, resets the sums.
- Reads use `dd skip=N count=0` to seek, not `tail -c +N`: BSD `tail` writes
  in tiny chunks (~30ms/MB), which put a 16 MB chunk at 470ms — past the
  debounce.

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
| `transcript_path` | Summed incrementally for `R`/`W` (see "How the totals are summed"); its tail is also the legacy budget fallback |
| `prompt_cache.cache_write_tokens` | `W` fallback when there is no transcript |

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
   payload, except session-total cache reads, which are summed from a
   remembered byte offset so each render reads only new bytes. The current
   version is ~130ms, dominated by one `git status`.
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

`samples/` holds twelve payloads covering the shapes the contract allows:
no `context_window`; first turn with `null`s; a normal 200k turn with a warm 5m
cache; a 1M window with a warm 1h cache; a near-full 1M window; post-`/compact`
(`current_usage` null, cache cold); `used_percentage` absent with
`current_usage` present; a resumed session with no `prompt_cache` yet;
a legacy payload resolved from `samples/transcript-fixture.jsonl`; a full
payload with `rate_limits.spend_limit`; a truncated, unparseable payload; and
session totals and session-row times from `samples/totals-fixture.jsonl`
(duplicate content-block lines, sidechain lines, a malformed line, a 2 KB tool
result, a task notification, a mid-turn `queued_command`, and a trailing line
still being written).

Assertions run against rows 2 and 3 with ANSI stripped (row 1 carries live git
state), with `TZ=UTC` so fixture times are stable. Each sample also asserts
exit 0, empty stderr, 1–3 rows,
and a 250ms wall-clock budget. A final check replays sample 12 with a 600-byte
`SL_TAIL_CAP` and asserts the chunked catch-up shows `+` and then converges on
exactly the uncapped totals. State files go to a throwaway `SL_STATE_DIR`.

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

`bash`, `jq` (1.6+ for `utf8bytelength`), `git`, `awk`, `date`, `stat`, `dd`,
`head`. `date` conversion tries BSD/macOS `date -r` first and falls back to GNU
`date -d @…`; `stat` tries GNU `-c %s` then BSD `-f %z`. Works on macOS and
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
