#!/usr/bin/env bash
# Claude Code statusline script
# Receives JSON on stdin from Claude Code

input=$(cat)

# --- ANSI color codes ---
DIM=$'\033[2m'
RESET=$'\033[0m'
GREEN=$'\033[32m'
YELLOW=$'\033[33m'
RED=$'\033[31m'
CYAN=$'\033[36m'
MAGENTA=$'\033[35m'

# --- Model (family + version, e.g. "Opus 4.7" from "Opus 4.7 (1M context)") ---
# Drop the trailing "(... context)" suffix; the window size is already shown
# on row 2 via the "/<size>k" context budget.
model_id=$(echo "$input" | jq -r '.model.id // ""')
model_name=$(echo "$input" | jq -r '.model.display_name // ""')
model_name=$(echo "$model_name" | sed -E 's/ *\(.*\)$//')

# --- Session ID ---
session_id=$(echo "$input" | jq -r '.session_id // ""')

# --- Working directory (basename, or ~/... if under $HOME) ---
current_dir=$(echo "$input" | jq -r '.workspace.current_dir // ""')
if [ -n "$current_dir" ]; then
  home_dir="$HOME"
  if [[ "$current_dir" == "$home_dir" ]]; then
    dir_display="~"
  elif [[ "$current_dir" == "$home_dir"/* ]]; then
    dir_display="~${current_dir#$home_dir}"
  else
    dir_display="$current_dir"
  fi
else
  dir_display=""
fi

# --- Git / worktree status (branch, worktree marker, dirty, ahead/behind) ---
# Computed by shelling out to git against current_dir; not present in stdin JSON.
git_branch=""
git_ahead=0
git_behind=0
git_dirty=0
is_worktree=0

if [ -n "$current_dir" ] && git -C "$current_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  # One call gets branch, upstream ahead/behind, and per-file change lines.
  status_v2=$(git -C "$current_dir" status --porcelain=v2 --branch 2>/dev/null)
  git_branch=$(printf '%s\n' "$status_v2" | awk '/^# branch.head / {print $3; exit}')
  ab=$(printf '%s\n' "$status_v2" | awk '/^# branch.ab / {print $3, $4; exit}')
  if [ -n "$ab" ]; then
    git_ahead=$(printf '%s\n' "$ab" | awk '{gsub(/[+]/,"",$1); print $1+0}')
    git_behind=$(printf '%s\n' "$ab" | awk '{gsub(/[-]/,"",$2); print $2+0}')
  fi
  # Count changed/untracked entries (lines starting with 1, 2, u, or ?).
  git_dirty=$(printf '%s\n' "$status_v2" | grep -cE '^(1|2|u|\?) ')

  # Linked worktree iff git-dir and git-common-dir diverge.
  gd=$(git -C "$current_dir" rev-parse --git-dir 2>/dev/null)
  gcd=$(git -C "$current_dir" rev-parse --git-common-dir 2>/dev/null)
  if [ -n "$gd" ] && [ -n "$gcd" ] && [ "$gd" != "$gcd" ]; then
    is_worktree=1
  fi
fi

# Build the compact git segment (omit pieces that are zero/absent).
git_segment=""
if [ -n "$git_branch" ]; then
  git_segment="${CYAN}⎇ ${git_branch}${RESET}"
  [ "$is_worktree" -eq 1 ] && git_segment="${git_segment} ${MAGENTA}⧉${RESET}"
  [ "${git_dirty:-0}" -gt 0 ] && git_segment="${git_segment} ${YELLOW}±${git_dirty}${RESET}"
  ab_disp=""
  [ "${git_ahead:-0}" -gt 0 ] && ab_disp="↑${git_ahead}"
  [ "${git_behind:-0}" -gt 0 ] && ab_disp="${ab_disp}↓${git_behind}"
  [ -n "$ab_disp" ] && git_segment="${git_segment} ${DIM}${ab_disp}${RESET}"
fi

# --- Context window size: 1M for [1m]/longcontext variants, else 200k ---
if echo "$model_id" | grep -qiE '1m|-1m|long'; then
  ctx_size=1000000
else
  ctx_size=200000
fi

# --- Parse transcript for cached/uncached token split ---
transcript_path=$(echo "$input" | jq -r '.transcript_path // ""')
cached_tokens=0
uncached_tokens=0
total_tokens=0
got_transcript=0

last_ts=""
if [ -n "$transcript_path" ] && [ -f "$transcript_path" ]; then
  # Extract usage block from the last assistant message in the JSONL.
  # Transcript lines have outer "type":"assistant" with usage nested at .message.usage
  # Also handles lines where inner message object has "type":"message","role":"assistant"
  # Grab the whole last assistant line once — it carries both the usage block and
  # the ISO-8601 timestamp we use as the cache-expiry anchor.
  last_asst_line=$(grep -F '"type":"assistant"' "$transcript_path" 2>/dev/null | tail -1)
  usage_json=$(printf '%s' "$last_asst_line" | jq -r '.message.usage // empty' 2>/dev/null)
  last_ts=$(printf '%s' "$last_asst_line" | jq -r '.timestamp // empty' 2>/dev/null)

  # Fallback: some formats use "role":"assistant" at outer level
  if [ -z "$usage_json" ]; then
    last_asst_line=$(grep -F '"role":"assistant"' "$transcript_path" 2>/dev/null | tail -1)
    usage_json=$(printf '%s' "$last_asst_line" | jq -r '.message.usage // .usage // empty' 2>/dev/null)
    last_ts=$(printf '%s' "$last_asst_line" | jq -r '.timestamp // empty' 2>/dev/null)
  fi

  if [ -n "$usage_json" ]; then
    input_tok=$(echo "$usage_json" | jq -r '.input_tokens // 0')
    cache_create=$(echo "$usage_json" | jq -r '.cache_creation_input_tokens // 0')
    cache_read=$(echo "$usage_json" | jq -r '.cache_read_input_tokens // 0')
    output_tok=$(echo "$usage_json" | jq -r '.output_tokens // 0')

    total_tokens=$(( input_tok + cache_create + cache_read + output_tok ))
    cached_tokens=$cache_read
    uncached_tokens=$(( total_tokens - cached_tokens ))
    got_transcript=1
  fi
fi

# Fall back to used_percentage if transcript parse failed
if [ "$got_transcript" -eq 0 ]; then
  used_pct=$(echo "$input" | jq -r '.context_window.used_percentage // empty')
  if [ -n "$used_pct" ]; then
    total_tokens=$(echo "$used_pct $ctx_size" | awk '{printf "%d", $1 / 100 * $2}')
  else
    total_tokens=0
  fi
  cached_tokens=0
  uncached_tokens=$total_tokens
fi

# Clamp total
if [ "$total_tokens" -gt "$ctx_size" ]; then
  total_tokens=$ctx_size
fi

# Usage-aware color for the context budget: <150k green, 150k–200k yellow, 200k+ red.
# Computed here (before token_display) so the expiry segment can revert to it.
if [ "$total_tokens" -lt 150000 ]; then
  token_color="$GREEN"
elif [ "$total_tokens" -lt 200000 ]; then
  token_color="$YELLOW"
else
  token_color="$RED"
fi

# --- Cache expiry: anchor = last request timestamp + TTL (refreshes every turn) ---
# The API never returns an expiry, but the prompt cache lives TTL seconds past the
# last request. Claude Code writes the prefix with either a 1h or 5m TTL; detect
# which by scanning recent usage for the most recent non-zero ephemeral bucket
# (cache_read refreshes the existing entry's TTL, so a pure-read turn keeps the
# same window). expiry = last_ts + ttl; we render that as a local clock time.
ttl_seconds=0
expiry_disp=""
expiry_color=""
if [ "$got_transcript" -eq 1 ] && [ "$cached_tokens" -gt 0 ] && [ -n "$last_ts" ]; then
  ttl_kind=$(grep -F '"type":"assistant"' "$transcript_path" 2>/dev/null \
    | tail -50 \
    | jq -rs 'map(.message.usage.cache_creation // empty)
              | map(select((.ephemeral_1h_input_tokens // 0) > 0 or (.ephemeral_5m_input_tokens // 0) > 0))
              | last
              | if . == null then ""
                elif (.ephemeral_1h_input_tokens // 0) > 0 then "1h"
                else "5m" end' 2>/dev/null)
  case "$ttl_kind" in
    1h) ttl_seconds=3600 ;;
    *)  ttl_seconds=300 ;;   # 5m write seen, or none → API default of 5 minutes
  esac

  # Parse the UTC timestamp to epoch (BSD/macOS date first, GNU date fallback).
  clean_ts="${last_ts%.*}"; clean_ts="${clean_ts%Z}"
  ts_epoch=$(TZ=UTC date -j -f "%Y-%m-%dT%H:%M:%S" "$clean_ts" +%s 2>/dev/null)
  [ -z "$ts_epoch" ] && ts_epoch=$(date -u -d "$last_ts" +%s 2>/dev/null)

  if [ -n "$ts_epoch" ]; then
    exp_epoch=$(( ts_epoch + ttl_seconds ))
    remain=$(( exp_epoch - $(date +%s) ))
    if [ "$remain" -le 0 ]; then
      expiry_disp="cold"
      expiry_color="$RED"
    else
      # Local clock time the cache goes cold (slides forward each turn).
      exp_clock=$(date -r "$exp_epoch" +%H:%M 2>/dev/null)
      [ -z "$exp_clock" ] && exp_clock=$(date -d "@$exp_epoch" +%H:%M 2>/dev/null)
      expiry_disp="exp ${exp_clock}"
      if [ "$remain" -le 60 ]; then expiry_color="$YELLOW"; else expiry_color="$GREEN"; fi
    fi
  fi
fi

# --- Token counts in thousands (round to nearest 1k) ---
total_k=$(echo "$total_tokens" | awk '{printf "%d", $1 / 1000 + 0.5}')
cached_k=$(echo "$cached_tokens" | awk '{printf "%d", $1 / 1000 + 0.5}')
size_k=$(( ctx_size / 1000 ))

if [ "$got_transcript" -eq 1 ] && [ "$cached_k" -gt 0 ]; then
  if [ -n "$expiry_disp" ]; then
    # Expiry segment gets its own color, then reverts to token_color for the rest.
    token_display="${total_k}k (${cached_k}k cached · ${expiry_color}${expiry_disp}${RESET}${token_color}) / ${size_k}k"
  else
    token_display="${total_k}k (${cached_k}k cached) / ${size_k}k"
  fi
else
  token_display="${total_k}k / ${size_k}k"
fi

# --- Effort / output style label ---
effort=$(echo "$input" | jq -r '.effort.level // ""')
style=$(echo "$input" | jq -r '.output_style.name // ""')

if [ -n "$effort" ]; then
  extra=" ${effort}"
elif [ -n "$style" ] && [ "$style" != "default" ]; then
  extra=" ${style}"
else
  extra=""
fi

# --- Output (single line) ---
# Build model+dir segment
if [ -n "$dir_display" ]; then
  left="${model_name}  ${dir_display}"
else
  left="${model_name}"
fi

if [ -n "$session_id" ]; then
  session_segment="  (${session_id})"
else
  session_segment=""
fi

# Row 1 — location: model + dir + git/worktree segment (git_segment carries its own colors)
line1="${DIM}${left}${RESET}"
[ -n "$git_segment" ] && line1="${line1}  ${git_segment}"

# Row 2 — session state: context budget (usage-colored) + effort/style + session id
line2="${token_color}${token_display}${RESET}${DIM}${extra}${session_segment}${RESET}"

printf '%s\n%s\n' "$line1" "$line2"
