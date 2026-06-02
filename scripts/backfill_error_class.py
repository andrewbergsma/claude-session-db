#!/usr/bin/env python3
"""
backfill_error_class — classify existing is_error tool_results in place.

The error taxonomy (transcript_analyzer.classify_error) is now populated at ingest,
but rows archived before that change have error_class = NULL. This script applies the
same classifier to the existing error rows via a targeted UPDATE — no full re-ingest
of the 1.3 GB archive.

Idempotent: re-running re-classifies (handy after editing ERROR_PATTERNS). Pass
--only-null to touch only rows not yet classified.

Usage:
    python3 scripts/backfill_error_class.py [--only-null] [--dry-run]
"""
from __future__ import annotations

import argparse
from collections import Counter

from claude_session_db.postgres import SessionArchive, resolve_dsn
from claude_session_db.transcript_analyzer import classify_error


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only-null", action="store_true",
                    help="Only classify rows where error_class IS NULL.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Classify and report counts, but do not write.")
    args = ap.parse_args()

    where = "tr.is_error" + (" AND tr.error_class IS NULL" if args.only_null else "")
    select = f"""
        SELECT tr.result_id, tr.content_text, cb.tool_name
        FROM tool_results tr
        LEFT JOIN content_blocks cb ON tr.tool_use_id = cb.tool_use_id
        WHERE {where}
    """

    with SessionArchive(resolve_dsn()) as a:
        conn = a.connect()
        with conn.cursor() as cur:
            cur.execute(select)
            rows = cur.fetchall()  # (result_id, content_text, tool_name)

        counts: Counter[str] = Counter()
        updates: list[tuple[str, int]] = []
        for result_id, content_text, tool_name in rows:
            cls = classify_error(tool_name or "", content_text or "")
            counts[cls] += 1
            updates.append((cls, result_id))

        print(f"{len(rows):,} error rows classified"
              + (" (only-null)" if args.only_null else ""))
        for cls, n in counts.most_common():
            print(f"  {cls:<22} {n:>6,}")

        if args.dry_run:
            print("\n--dry-run: no writes.")
            return

        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE tool_results SET error_class = %s WHERE result_id = %s",
                updates,
            )
        conn.commit()
        print(f"\nWrote error_class for {len(updates):,} rows.")


if __name__ == "__main__":
    main()
