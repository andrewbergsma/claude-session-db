#!/usr/bin/env bash
#
# UAT — PR #1: "cut sweep/reconcile noise, kill the silent lock-hang,
#               harden the gate against unreliable session_id"
# Merged as be7e6c7 on main.
#
# What this PR touched (the surfaces under test):
#   1. csd sweep                — dropped the noisy `doing` column; tool/err counts
#                                 instead; quiesced-flood collapse; quiet ingest preamble
#   2. csd reconcile-summaries  — no more 200s silent lock-hang (catalog-checked
#                                 schema self-heal under bounded lock_timeout);
#                                 staged progress; honest session_id-reuse reporting;
#                                 natural-key fallback for the gate
#   3. summary_state gate       — recovers real summarized sessions that lack /
#                                 reuse a session_id, via precise natural key
#
# Run:  bash scripts/uat_pr1.sh
# Non-destructive: only reads + idempotent reconcile (no --rebuild, no deletes).
# Exit non-zero on any hard assertion failure; "OBSERVE" steps are manual eyeballs.

set -uo pipefail

PASS=0; FAIL=0
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }
note() { printf '  \033[33mOBSERVE\033[0m %s\n' "$1"; }
hdr()  { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

# csd is a shell function in interactive zsh; in this script call the venv binary.
CSD="uv run csd"

hdr "0. Sanity — connect to the archive"
if $CSD dsn >/tmp/uat_dsn.txt 2>&1; then
  ok "csd dsn resolves ($(grep -o 'claude_sessions' /tmp/uat_dsn.txt | head -1))"
else
  bad "csd dsn failed — see /tmp/uat_dsn.txt"; cat /tmp/uat_dsn.txt; exit 1
fi

# ---------------------------------------------------------------------------
hdr "1. sweep — noise cut + activity counts + quiesced collapse"

$CSD sweep --no-ingest >/tmp/uat_sweep.txt 2>&1
cat /tmp/uat_sweep.txt

# 1a. The dropped `doing` column must be gone; the new columns must be present.
if grep -Eq '[0-9]+ tools' /tmp/uat_sweep.txt; then
  ok "sweep shows 'N tools' activity counts (replaces the old `doing` line)"
else
  note "no active sessions in window → no tool counts to show (re-run with --window 1440)"
fi

# 1b. Regression guard: none of the old `doing` noise sources should appear.
if grep -Eq 'system-reminder|mcp__|\"type\":|BEGIN.*PRIVATE KEY|sk-ant-' /tmp/uat_sweep.txt; then
  bad "sweep output still leaks tool-result noise (the bug this PR fixed)"
else
  ok "no MCP JSON / system-reminder / secret noise in sweep output"
fi

# 1c. Quiesced flood is collapsed to a single count line (when there are >5 done).
if grep -Eq '\+[0-9]+ quiesced' /tmp/uat_sweep.txt; then
  ok "quiesced sessions collapsed to a '+N quiesced' count line"
else
  note "fewer than the threshold of quiesced sessions → no collapse line (fine)"
fi

# 1d. Perf guard: sweep must return well under the 60s statement timeout.
START=$(date +%s)
$CSD sweep --no-ingest >/dev/null 2>&1
ELAPSED=$(( $(date +%s) - START ))
if [ "$ELAPSED" -lt 30 ]; then
  ok "sweep --no-ingest completed in ${ELAPSED}s (< 60s timeout; CTE regression gone)"
else
  bad "sweep took ${ELAPSED}s — investigate, the activity-CTE regression may be back"
fi

# 1e. Quiet ingest preamble — full sweep should NOT print the 12-line block / per-file lines.
$CSD sweep >/tmp/uat_sweep_full.txt 2>&1
if grep -Eq 'Syncing |Found [0-9]+ files' /tmp/uat_sweep_full.txt; then
  bad "ingest preamble still verbose (expected a single one-line SyncStats summary)"
else
  ok "ingest preamble quiet — one-line SyncStats summary only"
fi

# ---------------------------------------------------------------------------
hdr "2. reconcile-summaries — no lock-hang, honest reporting, natkey recovery"

# 2a. Must complete promptly (the bug was a ~200s silent hang). Bound at 90s.
START=$(date +%s)
timeout 90 $CSD reconcile-summaries >/tmp/uat_recon.txt 2>&1
RC=$?
ELAPSED=$(( $(date +%s) - START ))
cat /tmp/uat_recon.txt
if [ "$RC" -eq 124 ]; then
  bad "reconcile-summaries hit the 90s timeout — the silent lock-hang may be back"
elif [ "$RC" -ne 0 ]; then
  # A clean lock-contention ClickException is an acceptable, non-hanging outcome.
  if grep -q 'blocked on a lock held by a concurrent' /tmp/uat_recon.txt; then
    ok "reconcile surfaced the lock-contention message cleanly (no silent hang) in ${ELAPSED}s"
  else
    bad "reconcile exited $RC for another reason — see /tmp/uat_recon.txt"
  fi
else
  ok "reconcile-summaries completed in ${ELAPSED}s (no hang)"
fi

# 2b. Staged summary line present.
if grep -q 'Reconciled .* sessions (.* reclassified this run)' /tmp/uat_recon.txt; then
  ok "reports total + rows actually reclassified this run"
else
  bad "missing the 'Reconciled N sessions (M reclassified this run)' summary"
fi

# 2c. Honest session_id-reuse framing — must NOT imply deletable duplicates.
if grep -q 'not duplicate documents' /tmp/uat_recon.txt; then
  ok "session_id reuse characterized honestly (cross-app copies + in-app collisions)"
elif grep -qi 'duplicate' /tmp/uat_recon.txt; then
  note "a 'duplicate' string appears — confirm it's the honest 'not duplicate documents' framing"
else
  note "no session_id reuse reported this run (0 collisions) — fine"
fi

# 2d. Natural-key fallback wired in (may legitimately recover 0 on a settled db).
if grep -q 'via natural-key fallback' /tmp/uat_recon.txt; then
  ok "natural-key fallback recovered real summarized sessions"
else
  note "0 natkey recoveries this run (idempotent / already settled) — wording absent is OK"
fi

# 2e. Idempotency — a second run should reclassify ~0 (changed count near zero).
$CSD reconcile-summaries >/tmp/uat_recon2.txt 2>&1
CHANGED2=$(grep -oE 'Reconciled [0-9]+ sessions \(([0-9]+) reclassified' /tmp/uat_recon2.txt | grep -oE '\(([0-9]+)' | tr -d '(')
note "2nd reconcile reclassified='${CHANGED2:-?}' (expect 0 / very low — idempotent)"

# 2f. summary_state.reason CHECK allows 'natkey' (the catalog-guarded ALTER landed).
$CSD query "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint WHERE conname='summary_state_reason_check'" >/tmp/uat_chk.txt 2>&1
if grep -q 'natkey' /tmp/uat_chk.txt; then
  ok "summary_state_reason_check permits 'natkey'"
else
  bad "reason CHECK does not list 'natkey' — the widening ALTER did not apply"
fi

# ---------------------------------------------------------------------------
hdr "3. gate queue — unsummarized / mark-summarized round-trip"

$CSD unsummarized >/tmp/uat_unsum.txt 2>&1 && \
  ok "unsummarized serves the pending residue without error" || \
  bad "unsummarized errored — see /tmp/uat_unsum.txt"
head -5 /tmp/uat_unsum.txt

note "manual: pick a pending session id from above, run \`csd mark-summarized <id>\`,"
note "        then re-run \`csd unsummarized\` and confirm it drops out of the queue."

# ---------------------------------------------------------------------------
hdr "RESULT"
printf '  %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] && { echo "  UAT GREEN"; exit 0; } || { echo "  UAT RED"; exit 1; }
