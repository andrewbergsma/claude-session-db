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
#      The payload carries the context/cache state; the one thing it lacks,
#      session-total cache reads, is summed incrementally from a remembered
#      byte offset — O(bytes appended since the last render).
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

| (num($cw.used_percentage)
   // (if $tot != null and $size > 0 then ($tot * 100 / $size) else null end)) as $pct

| [ $mid
  , $mname
  , (str(.session_id) // "")
  , (str(.workspace.current_dir) // str(.cwd) // "")
  , ($size | tostring)
  , (if $tot    != null then ($tot    | floor | tostring) else "" end)
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
  , (if $pc != null and (num($pc.cache_write_tokens) != null)
     then ($pc.cache_write_tokens | floor | tostring) else "" end)
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
ctx_size=200000; total_tokens=""; used_pct=""
pc_present=0; pc_warm=0; pc_expires=""; pc_ttl=""; pc_hit=""; pc_misses=""
pc_write=""; effort=""; style=""; transcript_path=""

if [ -n "$row" ]; then
  IFS=$'\037' read -r model_id model_name session_id current_dir ctx_size \
    total_tokens used_pct pc_present pc_warm pc_expires \
    pc_ttl pc_hit pc_misses pc_write effort style transcript_path <<<"$row"
fi

# Numeric hygiene: anything that must go into arithmetic gets a definite value.
case "$ctx_size"     in ''|*[!0-9]*) ctx_size=200000 ;; esac
case "$pc_present"   in 1) ;; *) pc_present=0 ;; esac
case "$pc_warm"      in 1) ;; *) pc_warm=0 ;; esac
case "$pc_expires"   in ''|*[!0-9]*) pc_expires="" ;; esac
case "$pc_misses"    in ''|*[!0-9]*) pc_misses="" ;; esac
case "$pc_write"     in ''|*[!0-9]*) pc_write="" ;; esac

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
  t=$(tail -n 400 "$transcript_path" 2>/dev/null \
    | grep -F '"type":"assistant"' 2>/dev/null \
    | tail -1 \
    | jq -r '.message.usage // empty
             | (.input_tokens // 0) + (.cache_creation_input_tokens // 0)
               + (.cache_read_input_tokens // 0)' 2>/dev/null)
  case "$t" in ''|*[!0-9]*) : ;; *) total_tokens=$t ;; esac
fi

# ---------------------------------------------------------------------------
# Session cache totals: cumulative cache reads / writes for the main thread.
#
# The payload carries a cumulative write total (prompt_cache.cache_write_tokens)
# but no cumulative read total, so both are summed from the transcript. Rule 2
# still holds: a per-session state file remembers the byte offset already
# summed, and each render reads only the bytes appended since — O(new bytes).
# A resumed session with a large transcript catches up in SL_TAIL_CAP-sized
# chunks, one per render; the totals carry a trailing "+" until caught up.
#
# The transcript writes one line per content block, each repeating its
# message's usage, so lines are de-duplicated by message.id (last one wins —
# the id and its counted usage persist in the state across chunk boundaries).
# Sidechain lines are skipped, matching prompt_cache's main-thread scope.
# ---------------------------------------------------------------------------
TAIL_CAP=${SL_TAIL_CAP:-16000000}
STATE_DIR=${SL_STATE_DIR:-${TMPDIR:-/tmp}/claude-statusline}

# stdin (a regular file) from byte $1 onward. `dd skip= count=0` only lseeks
# the shared fd, then cat streams the rest. Not `tail -c +N`: BSD tail
# writes in tiny chunks, ~30ms/MB — a 16 MB chunk took 470ms vs 40ms here.
from_offset() { dd bs=1 skip="$1" count=0 2>/dev/null; cat; }

TOT_PROG='
def n(k): (.[k] // 0) | if type == "number" then . else 0 end;
  [inputs] as $lines
# A sentinel newline is appended to the chunk, so the last input is whatever
# followed the final newline: a line still being written or cut by the cap,
# or "". It is never counted; the next render re-reads it whole.
| $lines[:-1] as $done
| reduce ( $done[]
           | select(contains("\"usage\""))
           | (fromjson? // null)
           | select(type == "object" and .type == "assistant"
                    and .isSidechain != true)
           | .message
           | select(type == "object" and (.usage | type) == "object")
           | { id: ((.id // "") | tostring),
               r: (.usage | n("cache_read_input_tokens")),
               w: (.usage | n("cache_creation_input_tokens")) } ) as $m
    ( {r: $r, w: $w, id: $id, lr: $lr, lw: $lw};
      if $m.id != "" and $m.id == .id
      then .r += $m.r - .lr | .w += $m.w - .lw | .lr = $m.r | .lw = $m.w
      else .r += $m.r | .w += $m.w | .id = $m.id | .lr = $m.r | .lw = $m.w
      end )
| [ ($done | map(utf8bytelength + 1) | add // 0), .r, .w, .id, .lr, .lw ]
| map(tostring) | join("\u001f")
'

tot_reads=""; tot_writes=""; tot_partial=0
if [ -n "$session_id" ] && [ -n "$transcript_path" ] && [ -f "$transcript_path" ]; then
  size=$(stat -c %s "$transcript_path" 2>/dev/null || stat -f %z "$transcript_path" 2>/dev/null)
  case "$size" in ''|*[!0-9]*) size="" ;; esac
  state_file="$STATE_DIR/${session_id//\//_}.tot"

  s_path=""; s_off=0; s_r=0; s_w=0; s_id=""; s_lr=0; s_lw=0
  [ -f "$state_file" ] && IFS=$'\037' read -r s_path s_off s_r s_w s_id s_lr s_lw <"$state_file"
  for v in s_off s_r s_w s_lr s_lw; do
    case "${!v}" in ''|*[!0-9]*) printf -v "$v" 0 ;; esac
  done
  # A different transcript, or one that shrank, invalidates the running sums.
  if [ "$s_path" != "$transcript_path" ] || { [ -n "$size" ] && [ "$s_off" -gt "$size" ]; }; then
    s_off=0; s_r=0; s_w=0; s_id=""; s_lr=0; s_lw=0
  fi

  if [ -n "$size" ] && [ "$size" -gt "$s_off" ]; then
    len=$(( size - s_off ))
    # More than one cap behind: this render cannot reach the end.
    [ "$len" -gt "$TAIL_CAP" ] && { len=$TAIL_CAP; tot_partial=1; }
    res=$({ from_offset "$s_off" <"$transcript_path" | head -c "$len"; printf '\n'; } \
      | jq -R -n -r --argjson r "$s_r" --argjson w "$s_w" --arg id "$s_id" \
          --argjson lr "$s_lr" --argjson lw "$s_lw" "$TOT_PROG" 2>/dev/null)
    IFS=$'\037' read -r adv n_r n_w n_id n_lr n_lw <<<"$res"
    case "$adv$n_r$n_w$n_lr$n_lw" in
      ''|*[!0-9]*) : ;;
      *)
        # A capped chunk with no complete line is one line longer than the
        # cap — never an assistant line. Step over it by its exact length.
        if [ "$adv" -eq 0 ] && [ "$tot_partial" -eq 1 ]; then
          adv=$(from_offset "$s_off" <"$transcript_path" | head -n 1 | wc -c | tr -d ' ')
          case "$adv" in ''|*[!0-9]*) adv=0 ;; esac
        fi
        s_off=$(( s_off + adv )); s_r=$n_r; s_w=$n_w; s_id=$n_id; s_lr=$n_lr; s_lw=$n_lw
        mkdir -p "$STATE_DIR" 2>/dev/null \
          && printf '%s\037%s\037%s\037%s\037%s\037%s\037%s\n' \
               "$transcript_path" "$s_off" "$s_r" "$s_w" "$s_id" "$s_lr" "$s_lw" \
               >"$state_file.$$" 2>/dev/null \
          && mv -f "$state_file.$$" "$state_file" 2>/dev/null
        rm -f "$state_file.$$" 2>/dev/null
        ;;
    esac
  fi
  tot_reads=$s_r; tot_writes=$s_w
fi
# No transcript to sum: the payload's own cumulative write count still stands.
[ -z "$tot_writes" ] && [ -n "$pc_write" ] && tot_writes=$pc_write

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
  token_display="${total_k}k / ${size_k}k"
else
  # No context signal yet (first turn, or right after /compact).
  token_color="$DIM"
  token_display="—k / ${size_k}k"
fi

# ---------------------------------------------------------------------------
# Cache segment: session totals, then prompt_cache state (v2.1.251+).
# ---------------------------------------------------------------------------
# 950 -> 950, 352000 -> 352k, 41234567 -> 41.2M, 1234567890 -> 1.2B
fmt_tokens() {
  awk -v n="$1" 'BEGIN{
    if      (n < 1000)     printf "%d", n
    else if (n < 999500)   printf "%dk", n/1000 + 0.5
    else if (n < 999950000) printf "%.1fM", n/1e6
    else                   printf "%.1fB", n/1e9 }'
}

cache_bits=()
more=""; [ "$tot_partial" -eq 1 ] && more="+"
case "$tot_reads"  in ''|*[!0-9]*) tot_reads=0 ;; esac
case "$tot_writes" in ''|*[!0-9]*) tot_writes=0 ;; esac
[ "$tot_reads"  -gt 0 ] && cache_bits+=("R $(fmt_tokens "$tot_reads")${more}")
[ "$tot_writes" -gt 0 ] && cache_bits+=("W $(fmt_tokens "$tot_writes")${more}")

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
      cache_bits+=("${exp_color}exp ${exp_clock}${RESET}")
    fi
  elif [ "$pc_warm" -eq 0 ]; then
    cache_bits+=("${RED}cold${RESET}")
  fi

  if [ -n "$pc_hit" ]; then
    hit_pct=$(awk -v h="$pc_hit" 'BEGIN{printf "%d", h*100 + 0.5}' 2>/dev/null)
    case "$hit_pct" in ''|*[!0-9]*) hit_pct="" ;; esac
    if [ -n "$hit_pct" ]; then
      if [ -n "$pc_misses" ] && [ "$pc_misses" -gt 0 ]; then
        cache_bits+=("${hit_pct}% hit ${YELLOW}${pc_misses} miss${RESET}")
      else
        cache_bits+=("${hit_pct}% hit")
      fi
    fi
  fi
fi

cache_display=""
if [ "${#cache_bits[@]}" -gt 0 ]; then
  cache_display="  ${cache_bits[0]}"
  for ((i = 1; i < ${#cache_bits[@]}; i++)); do
    cache_display="${cache_display} · ${cache_bits[$i]}"
  done
fi

# --- Effort / output style label ---
if [ -n "$effort" ]; then
  extra="  ${effort}"
elif [ -n "$style" ] && [ "$style" != "default" ]; then
  extra="  ${style}"
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

# Row 2 — session state: context budget + cache totals/state + effort/style + session id.
line2="${token_color}${token_display}${RESET}${cache_display}${DIM}${extra}${session_segment}${RESET}"

# A payload we could not parse at all still renders row 2 rather than a blank row.
if [ -n "$line1" ]; then
  printf '%s\n%s\n' "$line1" "$line2"
else
  printf '%s\n' "$line2"
fi
