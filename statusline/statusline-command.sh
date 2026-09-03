#!/usr/bin/env bash
# Claude Code statusline script
# Receives the status-line JSON payload on stdin from Claude Code.
#
# Contract: https://code.claude.com/docs/en/statusline
# Verified against Claude Code v2.1.259 (2026-09).
#
# Design rules (see README.md "Why it is shaped this way"):
#   1. ONE jq pass over the payload. Claude Code cancels an in-flight status
#      line script when the next update triggers (300ms debounce), so a slow
#      script renders intermittently or not at all.
#   2. NEVER read the whole transcript. That is O(session size) and was the
#      dominant cost of the previous version (~0.6s on a 37 MB transcript).
#      The payload now carries everything the transcript was being mined for.
#   3. Every segment degrades independently. A missing field drops its own
#      segment; it never blanks the line.

input=$(cat)

# --- ANSI color codes ---
DIM=$'\033[2m'
RESET=$'\033[0m'
GREEN=$'\033[32m'
YELLOW=$'\033[33m'
RED=$'\033[31m'
CYAN=$'\033[36m'
MAGENTA=$'\033[35m'

# ---------------------------------------------------------------------------
# Single jq pass over the payload.
#
# Emits one @tsv row of fixed-order fields. @tsv escapes any embedded tab or
# newline, so the row is always exactly one line with stable field count.
# Every lookup is null-guarded; jq never errors on a missing or null key.
# ---------------------------------------------------------------------------
JQ_PROG='
def num(v): if (v | type) == "number" then v else null end;
def str(v): if (v | type) == "string" then v else null end;

  (.context_window // {})                as $cw
| (if (.context_window.current_usage | type) == "object"
   then .context_window.current_usage else {} end) as $cu
| (if (.prompt_cache | type) == "object" then .prompt_cache else null end) as $pc
| (str(.model.id) // "")                 as $mid
| (str(.model.display_name) // "")       as $mname

# Context window size: the payload is authoritative (200000, or 1000000 for
# extended-context models). Only fall back to sniffing the model id/name.
| ( num($cw.context_window_size)
    // (if ($mid + " " + $mname | test("1m|1 ?m context|long ?context"; "i"))
        then 1000000 else 200000 end) )   as $size

# Tokens currently in the context window, input-only — the same basis Claude
# Code uses for used_percentage (output_tokens are excluded).
#   1. context_window.total_input_tokens  (authoritative, current turn)
#   2. derived from current_usage         (older payloads)
#   3. derived from used_percentage       (last resort)
| ( num($cw.total_input_tokens)
    // ( if ($cu | length) > 0
         then ((num($cu.input_tokens) // 0)
               + (num($cu.cache_creation_input_tokens) // 0)
               + (num($cu.cache_read_input_tokens) // 0))
         else null end )
    // ( if num($cw.used_percentage) != null
         then (($cw.used_percentage / 100) * $size | floor)
         else null end ) )                as $tot

| (num($cu.cache_read_input_tokens))      as $cached
| (num($cw.used_percentage)
   // (if $tot != null and $size > 0 then ($tot * 100 / $size) else null end)) as $pct

| [ $mid
  , $mname
  , (str(.session_id) // "")
  , (str(.workspace.current_dir) // str(.cwd) // "")
  , ($size | tostring)
  , (if $tot    != null then ($tot    | floor | tostring) else "" end)
  , (if $cached != null then ($cached | floor | tostring) else "" end)
  , (if $pct    != null then ($pct    | tostring)         else "" end)
  , (if $pc != null then "1" else "0" end)
  , (if $pc != null and $pc.warm == true then "1" else "0" end)
  , (if $pc != null and (num($pc.expires_at) != null)
     then ($pc.expires_at | floor | tostring) else "" end)
  , (if $pc != null then (str($pc.ttl) // "") else "" end)
  , (if $pc != null and (num($pc.hit_ratio) != null)
     then ($pc.hit_ratio | tostring) else "" end)
  , (if $pc != null and (num($pc.misses) != null)
     then ($pc.misses | floor | tostring) else "" end)
  , (str(.effort.level) // "")
  , (str(.output_style.name) // "")
  , (str(.transcript_path) // "")
  ]
# US (0x1f) as the field separator, not tab: tab is IFS whitespace, so bash
# collapses runs of tabs and every empty field would shift the whole row.
| map(tostring)
| join("\u001f")
'

row=$(printf '%s' "$input" | jq -r "$JQ_PROG" 2>/dev/null)

model_id=""; model_name=""; session_id=""; current_dir=""
ctx_size=200000; total_tokens=""; cached_tokens=""; used_pct=""
pc_present=0; pc_warm=0; pc_expires=""; pc_ttl=""; pc_hit=""; pc_misses=""
effort=""; style=""; transcript_path=""

if [ -n "$row" ]; then
  IFS=$'\037' read -r model_id model_name session_id current_dir ctx_size \
    total_tokens cached_tokens used_pct pc_present pc_warm pc_expires \
    pc_ttl pc_hit pc_misses effort style transcript_path <<<"$row"
fi

# Numeric hygiene: anything that must go into arithmetic gets a definite value.
case "$ctx_size"     in ''|*[!0-9]*) ctx_size=200000 ;; esac
case "$pc_present"   in 1) ;; *) pc_present=0 ;; esac
case "$pc_warm"      in 1) ;; *) pc_warm=0 ;; esac
case "$pc_expires"   in ''|*[!0-9]*) pc_expires="" ;; esac
case "$pc_misses"    in ''|*[!0-9]*) pc_misses="" ;; esac

# Drop the trailing "(... context)" suffix from the display name; the window
# size is already shown on row 2 via the "/<size>k" context budget.
model_name=${model_name%% (*}

# --- Working directory (basename, or ~/... if under $HOME) ---
dir_display=""
if [ -n "$current_dir" ]; then
  if [ "$current_dir" = "$HOME" ]; then
    dir_display="~"
  elif [ "${current_dir#"$HOME"/}" != "$current_dir" ]; then
    dir_display="~/${current_dir#"$HOME"/}"
  else
    dir_display="$current_dir"
  fi
fi

# ---------------------------------------------------------------------------
# Legacy fallback: only when the payload carried no context signal at all.
#
# Pre-v2.0.65 payloads have no context_window. Rather than scan the whole
# transcript (O(session size), ~0.5s on a 37 MB JSONL), read only the tail.
# `tail -n` seeks from the end, so this is O(tail) — 9ms on that same file.
# ---------------------------------------------------------------------------
if [ -z "$total_tokens" ] && [ -n "$transcript_path" ] && [ -f "$transcript_path" ]; then
  usage_json=$(tail -n 400 "$transcript_path" 2>/dev/null \
    | grep -F '"type":"assistant"' 2>/dev/null \
    | tail -1 \
    | jq -r '.message.usage // empty' 2>/dev/null)
  if [ -n "$usage_json" ]; then
    read -r t c < <(printf '%s' "$usage_json" | jq -r '
      "\((.input_tokens // 0) + (.cache_creation_input_tokens // 0)
         + (.cache_read_input_tokens // 0)) \(.cache_read_input_tokens // 0)"' 2>/dev/null)
    case "$t" in ''|*[!0-9]*) : ;; *) total_tokens=$t ;; esac
    case "$c" in ''|*[!0-9]*) : ;; *) cached_tokens=$c ;; esac
  fi
fi

# --- Git / worktree status (branch, worktree marker, dirty, ahead/behind) ---
# Not in the payload; one `git status` call supplies all four pieces.
git_segment=""
if [ -n "$current_dir" ] && [ -d "$current_dir" ] \
   && git -C "$current_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  status_v2=$(git -C "$current_dir" status --porcelain=v2 --branch 2>/dev/null)
  git_branch=$(printf '%s\n' "$status_v2" | awk '/^# branch.head / {print $3; exit}')
  if [ -n "$git_branch" ] && [ "$git_branch" != "(detached)" ]; then
    git_ahead=0; git_behind=0
    ab=$(printf '%s\n' "$status_v2" | awk '/^# branch.ab / {print $3, $4; exit}')
    if [ -n "$ab" ]; then
      git_ahead=$(printf '%s\n' "$ab" | awk '{gsub(/[+]/,"",$1); print $1+0}')
      git_behind=$(printf '%s\n' "$ab" | awk '{gsub(/[-]/,"",$2); print $2+0}')
    fi
    # Changed/untracked entries: lines starting with 1, 2, u, or ?.
    git_dirty=$(printf '%s\n' "$status_v2" | grep -cE '^(1|2|u|\?) ')
    case "$git_dirty" in ''|*[!0-9]*) git_dirty=0 ;; esac

    # Linked worktree iff git-dir and git-common-dir diverge.
    is_worktree=0
    gd=$(git -C "$current_dir" rev-parse --git-dir 2>/dev/null)
    gcd=$(git -C "$current_dir" rev-parse --git-common-dir 2>/dev/null)
    [ -n "$gd" ] && [ -n "$gcd" ] && [ "$gd" != "$gcd" ] && is_worktree=1

    git_segment="${CYAN}⎇ ${git_branch}${RESET}"
    [ "$is_worktree" -eq 1 ] && git_segment="${git_segment} ${MAGENTA}⧉${RESET}"
    [ "$git_dirty" -gt 0 ] && git_segment="${git_segment} ${YELLOW}±${git_dirty}${RESET}"
    ab_disp=""
    [ "$git_ahead"  -gt 0 ] && ab_disp="↑${git_ahead}"
    [ "$git_behind" -gt 0 ] && ab_disp="${ab_disp}↓${git_behind}"
    [ -n "$ab_disp" ] && git_segment="${git_segment} ${DIM}${ab_disp}${RESET}"
  fi
fi

# ---------------------------------------------------------------------------
# Context budget segment
# ---------------------------------------------------------------------------
size_k=$(( ctx_size / 1000 ))

if [ -n "$total_tokens" ]; then
  # Percentage of the *actual* window, so a 1M session is not permanently red.
  if [ -n "$used_pct" ]; then
    pct_int=$(awk -v p="$used_pct" 'BEGIN{printf "%d", (p<0?0:p)+0.5}' 2>/dev/null)
  else
    pct_int=$(awk -v t="$total_tokens" -v s="$ctx_size" \
      'BEGIN{if(s>0) printf "%d", t*100/s+0.5; else print 0}' 2>/dev/null)
  fi
  case "$pct_int" in ''|*[!0-9]*) pct_int=0 ;; esac

  if   [ "$pct_int" -lt 75 ]; then token_color="$GREEN"
  elif [ "$pct_int" -lt 90 ]; then token_color="$YELLOW"
  else                             token_color="$RED"
  fi

  total_k=$(awk -v t="$total_tokens" 'BEGIN{printf "%d", t/1000 + 0.5}')
  token_display="${total_k}k"

  # --- Cache detail, from the prompt_cache block (v2.1.251+) --------------
  cache_bits=()
  if [ -n "$cached_tokens" ] && [ "$cached_tokens" -gt 0 ] 2>/dev/null; then
    cached_k=$(awk -v c="$cached_tokens" 'BEGIN{printf "%d", c/1000 + 0.5}')
    [ "$cached_k" -gt 0 ] && cache_bits+=("${cached_k}k cached")
  fi

  if [ "$pc_present" -eq 1 ]; then
    if [ "$pc_warm" -eq 1 ] && [ -n "$pc_expires" ]; then
      exp_clock=$(date -r "$pc_expires" +%H:%M 2>/dev/null)
      [ -z "$exp_clock" ] && exp_clock=$(date -d "@$pc_expires" +%H:%M 2>/dev/null)
      if [ -n "$exp_clock" ]; then
        remain=$(( pc_expires - $(date +%s) ))
        if   [ "$remain" -le 0 ];  then exp_color="$RED"
        elif [ "$remain" -le 60 ]; then exp_color="$YELLOW"
        else                            exp_color="$GREEN"
        fi
        cache_bits+=("${exp_color}exp ${exp_clock}${RESET}${token_color}")
      fi
    elif [ "$pc_warm" -eq 0 ]; then
      cache_bits+=("${RED}cold${RESET}${token_color}")
    fi

    if [ -n "$pc_hit" ]; then
      hit_pct=$(awk -v h="$pc_hit" 'BEGIN{printf "%d", h*100 + 0.5}' 2>/dev/null)
      case "$hit_pct" in ''|*[!0-9]*) hit_pct="" ;; esac
      if [ -n "$hit_pct" ]; then
        if [ -n "$pc_misses" ] && [ "$pc_misses" -gt 0 ]; then
          cache_bits+=("${hit_pct}% hit ${YELLOW}${pc_misses} miss${RESET}${token_color}")
        else
          cache_bits+=("${hit_pct}% hit")
        fi
      fi
    fi
  fi

  if [ "${#cache_bits[@]}" -gt 0 ]; then
    joined="${cache_bits[0]}"
    for ((i = 1; i < ${#cache_bits[@]}; i++)); do
      joined="${joined} · ${cache_bits[$i]}"
    done
    token_display="${token_display} (${joined})"
  fi

  token_display="${token_display} / ${size_k}k"
else
  # No context signal yet (first turn, or right after /compact).
  token_color="$DIM"
  token_display="—k / ${size_k}k"
fi

# --- Effort / output style label ---
if [ -n "$effort" ]; then
  extra=" ${effort}"
elif [ -n "$style" ] && [ "$style" != "default" ]; then
  extra=" ${style}"
else
  extra=""
fi

# --- Output (two rows) ---
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

# Row 1 — location: model + dir + git/worktree segment.
line1=""
[ -n "$left" ] && line1="${DIM}${left}${RESET}"
if [ -n "$git_segment" ]; then
  [ -n "$line1" ] && line1="${line1}  "
  line1="${line1}${git_segment}"
fi

# Row 2 — session state: context budget + cache detail + effort/style + session id.
line2="${token_color}${token_display}${RESET}${DIM}${extra}${session_segment}${RESET}"

# A payload we could not parse at all still renders row 2 rather than a blank row.
if [ -n "$line1" ]; then
  printf '%s\n%s\n' "$line1" "$line2"
else
  printf '%s\n' "$line2"
fi
