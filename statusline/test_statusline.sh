#!/usr/bin/env bash
# Replay harness for statusline-command.sh.
#
#   ./test_statusline.sh              # assert every sample
#   ./test_statusline.sh --show       # print rendered output for every sample
#   SL_SCRIPT=/path/to/other.sh ./test_statusline.sh --show   # compare versions
#   SL_BASH=/bin/bash ./test_statusline.sh                     # macOS stock bash 3.2
#
# Each sample in samples/ is a status-line payload covering one shape of the
# Claude Code contract (see README.md). Assertions run against row 2 with ANSI
# escapes stripped, because row 1 carries live git state.

set -u

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
script=${SL_SCRIPT:-$here/statusline-command.sh}
samples=$here/samples
fixture=$samples/transcript-fixture.jsonl
totals=$samples/totals-fixture.jsonl

# Session-total state files go to a throwaway dir, never the live one.
SL_STATE_DIR=$(mktemp -d); export SL_STATE_DIR
trap 'rm -rf "$SL_STATE_DIR"' EXIT

show=0
[ "${1:-}" = "--show" ] && show=1

# Budget: Claude Code debounces status-line updates at 300ms and cancels an
# in-flight script when the next update fires. Stay well under that.
BUDGET_MS=${SL_BUDGET_MS:-250}

pass=0; fail=0

# strip ANSI SGR sequences
strip() { sed -e $'s/\033\\[[0-9;]*m//g'; }

run_sample() {
  # $1 sample file -> sets out, err, rc, ms, row1, row2
  local f=$1 payload t0 t1
  payload=$(cat "$f")
  # 09 and 12 reference transcript fixtures by placeholder so the repo is portable.
  payload=${payload//__FIXTURE__/$fixture}
  payload=${payload//__TOTALS__/$totals}

  t0=$(perl -MTime::HiRes=time -e 'printf "%d", time*1000')
  out=$(printf '%s' "$payload" | ${SL_BASH:-bash} "$script" 2>/tmp/_sl_err.$$)
  rc=$?
  t1=$(perl -MTime::HiRes=time -e 'printf "%d", time*1000')
  ms=$(( t1 - t0 ))
  err=$(cat /tmp/_sl_err.$$); rm -f /tmp/_sl_err.$$
  nrows=$(printf '%s\n' "$out" | grep -c '')
  row1=$(printf '%s\n' "$out" | sed -n 1p | strip)
  # The status row is always the last row; an unparseable payload drops row 1.
  last_row=$(printf '%s\n' "$out" | tail -1 | strip)
}

check() {
  # check <name> <regex> <actual>
  if printf '%s' "$3" | grep -qE "$2"; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    printf '  FAIL %s\n       expected /%s/\n       actual  %s\n' "$1" "$2" "$3" >&2
  fi
}

assert_sample() {
  local name=$1 want=$2 f=$samples/$1
  run_sample "$f"

  if [ "$show" -eq 1 ]; then
    printf '\n--- %s (%sms, rc=%s) ---\n%s\n%s\n' \
      "$name" "$ms" "$rc" "$row1" "$last_row"
    [ -n "$err" ] && printf 'stderr: %s\n' "$err"
    return
  fi

  if [ "$rc" -ne 0 ]; then
    fail=$((fail + 1)); printf '  FAIL %s: exit %s\n' "$name" "$rc" >&2
  else pass=$((pass + 1)); fi

  if [ -n "$err" ]; then
    fail=$((fail + 1)); printf '  FAIL %s: stderr not empty: %s\n' "$name" "$err" >&2
  else pass=$((pass + 1)); fi

  if [ "$nrows" -lt 1 ] || [ "$nrows" -gt 2 ]; then
    fail=$((fail + 1)); printf '  FAIL %s: expected 1-2 rows, got %s:\n%s\n' "$name" "$nrows" "$out" >&2
  else pass=$((pass + 1)); fi

  if [ "$ms" -gt "$BUDGET_MS" ]; then
    fail=$((fail + 1)); printf '  FAIL %s: %sms exceeds %sms budget\n' "$name" "$ms" "$BUDGET_MS" >&2
  else pass=$((pass + 1)); fi

  check "$name status row" "$want" "$last_row"
}

echo "statusline replay: $script"

#              sample                              expected status-row pattern
assert_sample 01-no-context-window.json          '^—k / 200k  \(1f2e3d4c\)$'
assert_sample 02-first-turn-nulls.json           '^0k / 200k  \(9c799c1b\)$'
assert_sample 03-normal-turn-5m-warm.json        '^64k / 200k  W 88k · exp [0-9]{2}:[0-9]{2} · 94% hit  high  \(a1b2c3d4\)$'
assert_sample 04-1m-window-1h-warm.json          '^316k / 1000k  W 352k · exp [0-9]{2}:[0-9]{2} · 91% hit 2 miss  high  \(9c799c1b\)$'
assert_sample 05-near-full-1m.json               '^942k / 1000k  W 1\.2M · exp [0-9]{2}:[0-9]{2} · 88% hit 7 miss  max  \(deadbeef\)$'
assert_sample 06-post-compact.json               '^0k / 200k  W 210k · cold · 87% hit 1 miss  medium  \(c0mpac7e\)$'
assert_sample 07-derive-from-current-usage.json  '^180k / 200k  \(derive01\)$'
assert_sample 08-resumed-no-prompt-cache.json    '^0k / 200k  high  \(resum3d0\)$'
assert_sample 09-legacy-transcript-fallback.json '^157k / 200k  R 153k · W 4k  \(1eg4cy00\)$'
assert_sample 10-rate-limits-spend-limit.json    '^150k / 1000k  W 180k · exp [0-9]{2}:[0-9]{2} · 97% hit  high  \(rl1m1t50\)$'
assert_sample 11-malformed.json                  '^—k / 200k$'
# Transcript sums win over the payload's cache_write_tokens (999999): msg_A's
# two block lines count once, the sidechain, malformed and still-being-written
# lines not at all.
assert_sample 12-session-totals.json             '^64k / 200k  R 205k · W 7k · exp [0-9]{2}:[0-9]{2} · 95% hit  high  \(t0ta1s00\)$'

if [ "$show" -eq 1 ]; then exit 0; fi

# Chunked catch-up: a 600-byte cap forces several renders, splits msg_A's
# duplicate lines across chunks and steps over the 2 KB tool_result line. It
# must converge on exactly the uncapped totals, marked "+" until it does.
rm -f "$SL_STATE_DIR"/*.tot
first=""; final=""
for _ in $(seq 1 20); do
  SL_TAIL_CAP=600 run_sample "$samples/12-session-totals.json"
  [ -z "$first" ] && first=$last_row
  final=$last_row
  case "$last_row" in *+*) ;; *) break ;; esac
done
check "12 capped: first render partial" 'R [0-9]+k\+ · W [0-9]+k\+' "$first"
check "12 capped: converges exactly"    '^64k / 200k  R 205k · W 7k · ' "$final"

printf '\n%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
