"""The unmodelled-record-type tripwire.

`JSONLParser.parse_file` has always collected `records["unknown"]` as
`[(line_num, record_type)]`, and until schema v9 **nothing read it**. That is
precisely how Claude Code v2.1.161-258 could add ten record types — atis-latch,
worktree-state, relocated, file-history-delta, history-suppression, frame-link,
cost-state, artifact-autoreact-ledger, artifact-comment-monitor,
fork-context-ref — and have csd drop every one of them without a single line of
output. Sweep health stayed green the whole time.

These tests pin the two halves of the fix:
  1. SyncStats counts unknown records and censuses them by type;
  2. both the human summary and the one-line sweep summary say so.
"""
from __future__ import annotations

import json

from claude_session_db.jsonl_records import JSONLParser
from claude_session_db.sync import SyncStats


def test_stats_counts_and_censuses():
    s = SyncStats(files_found=1, files_synced=1, messages=3)
    s.note_unknown("brand-new-type")
    s.note_unknown("brand-new-type")
    s.note_unknown("another-type")
    assert s.unknown == 3
    assert s.unknown_types == {"brand-new-type": 2, "another-type": 1}
    # ordered by frequency
    assert s.unknown_census().startswith("brand-new-type×2")
    assert "another-type×1" in s.unknown_census()


def test_census_is_empty_when_nothing_unknown():
    """Callers append it unconditionally, so it must be falsy, not 'none'."""
    assert SyncStats().unknown_census() == ""


def test_oneline_surfaces_the_tripwire():
    s = SyncStats(files_found=2, files_synced=2, messages=10)
    assert "UNMODELLED" not in s.oneline()
    s.note_unknown("brand-new-type", 7)
    line = s.oneline()
    assert "UNMODELLED 7" in line
    assert "brand-new-type×7" in line


def test_str_surfaces_the_tripwire_and_says_where_they_went():
    s = SyncStats()
    s.note_unknown("brand-new-type", 4)
    text = str(s)
    assert "UNMODELLED record types: 4 records" in text
    assert "session_records" in text, "must say the records were CAPTURED, not lost"


def test_census_is_capped_and_reports_the_overflow():
    s = SyncStats()
    for i in range(12):
        s.note_unknown(f"type-{i}", 12 - i)
    c = s.unknown_census(limit=3)
    assert c.count("×") == 3
    assert "+9 more" in c


def _write(tmp_path, records):
    p = tmp_path / "sess.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records))
    return p


def test_parser_still_reports_a_genuinely_new_type(tmp_path):
    """A type csd has never seen must land in `unknown` with its line number —
    the input the tripwire reads."""
    path = _write(tmp_path, [
        {"type": "ai-title", "sessionId": "s1", "aiTitle": "t"},
        {"type": "a-type-from-the-future", "sessionId": "s1", "whatever": 1},
    ])
    records = JSONLParser(tmp_path).parse_file(path)
    assert records["unknown"] == [(2, "a-type-from-the-future")]



def test_modelled_types_do_not_trip_the_wire(tmp_path):
    """The ten types routed to session_records in v9 are MODELLED — they must
    not re-report as unknown on every sweep, or the signal is pure noise."""
    path = _write(tmp_path, [
        {"type": "atis-latch", "atis": "abc", "sessionId": "s1"},
        {"type": "relocated", "sessionId": "s1", "relocatedCwd": "/tmp/x"},
        {"type": "cost-state", "sessionId": "s1", "totalCostUSD": 1.0},
    ])
    records = JSONLParser(tmp_path).parse_file(path)
    assert records["unknown"] == []
