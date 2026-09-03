#!/usr/bin/env bash
# Replay harness for statusline-command.sh.
#
#   ./test_statusline.sh              # assert every sample
#   ./test_statusline.sh --show       # print rendered output for every sample
#   SL_SCRIPT=/path/to/other.sh ./test_statusline.sh --show   # compare versions
#
# Each sample in samples/ is a status-line payload covering one shape of the
# Claude Code contract (see README.md). Assertions run against row 2 with ANSI
# escapes stripped, because row 1 carries live git state.

set -u

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
script=${SL_SCRIPT:-$here/statusline-command.sh}
samples=$here/samples
fixture=$samples/transcript-fixture.jsonl

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
  # 09 references the transcript fixture by placeholder so the repo is portable.
  payload=${payload//__FIXTURE__/$fixture}

  t0=$(perl -MTime::HiRes=time -e 'printf "%d", time*1000')
  out=$(printf '%s' "$payload" | bash "$script" 2>/tmp/_sl_err.$$)
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
assert_sample 01-no-context-window.json          '^—k / 200k'
assert_sample 02-first-turn-nulls.json           '^0k / 200k  \(9c799c1b\)$'
assert_sample 03-normal-turn-5m-warm.json        '^64k \(61k cached · exp [0-9]{2}:[0-9]{2} · 94% hit\) / 200k high  \(a1b2c3d4\)$'
assert_sample 04-1m-window-1h-warm.json          '^316k \(314k cached · exp [0-9]{2}:[0-9]{2} · 91% hit 2 miss\) / 1000k high  \(9c799c1b\)$'
assert_sample 05-near-full-1m.json               '^942k \(938k cached · exp [0-9]{2}:[0-9]{2} · 88% hit 7 miss\) / 1000k max  \(deadbeef\)$'
assert_sample 06-post-compact.json               '^0k \(cold · 87% hit 1 miss\) / 200k medium  \(c0mpac7e\)$'
assert_sample 07-derive-from-current-usage.json  '^180k \(179k cached\) / 200k  \(derive01\)$'
assert_sample 08-resumed-no-prompt-cache.json    '^0k / 200k high  \(resum3d0\)$'
assert_sample 09-legacy-transcript-fallback.json '^157k \(153k cached\) / 200k  \(1eg4cy00\)$'
assert_sample 10-rate-limits-spend-limit.json    '^150k \(147k cached · exp [0-9]{2}:[0-9]{2} · 97% hit\) / 1000k high  \(rl1m1t50\)$'
assert_sample 11-malformed.json                  '^—k / 200k'

if [ "$show" -eq 1 ]; then exit 0; fi

printf '\n%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
