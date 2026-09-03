"""`sessions.session_kind` — the column v9 added and never filled.

v9 added `session_kind` to BOTH `messages` and `sessions`, wrote a backfill for
`messages` (`v9_message_effort_usage`, from `raw->>'sessionKind'`) and wrote
none for `sessions`. Ingest fills the session column only when a transcript is
re-synced, so on the live archive it was NULL on every row: the column existed,
the partial index existed, and nothing could answer "which sessions are
background sessions?".

`sessionKind` is CONSTANT per session, which is exactly what makes the sessions
column recomputable from the messages column instead of a 2,000-file re-parse.
"""
from __future__ import annotations

import pathlib

from claude_session_db import postgres, sync


def _bf():
    return next(b for b in postgres.BACKFILLS if b["key"] == "v10_session_kind")


def test_backfill_is_registered():
    assert _bf()["desc"]


def test_backfill_walks_the_sessions_primary_key():
    sql = _bf()["sql"]
    assert "WHERE session_id > %(after)s ORDER BY session_id LIMIT %(limit)s" in sql
    assert "max(session_id) FROM batch" in sql


def test_backfill_reads_the_message_column():
    sql = _bf()["sql"]
    assert "FROM messages" in sql
    assert "session_kind IS NOT NULL" in sql


def test_backfill_only_fills_nulls_and_never_overwrites():
    """Ingest's derivation is authoritative; this is a gap-filler."""
    sql = _bf()["sql"]
    assert sql.count("session_kind IS NULL") >= 2
    assert "s.session_kind IS NULL" in sql


def test_backfill_deletes_nothing():
    sql = _bf()["sql"].upper()
    assert "DELETE" not in sql and "TRUNCATE" not in sql


def test_the_sessions_column_and_its_index_already_exist():
    """Additive: v10 adds no DDL for this — only the missing data."""
    assert "ADD COLUMN session_kind TEXT" in postgres.SCHEMA_SQL
    assert "idx_sessions_kind" in postgres.SCHEMA_SQL


def test_the_stale_docstring_is_corrected():
    """`_derive_session_kind` claimed there was deliberately no per-message
    column. There is one, and it is what this backfill reads."""
    doc = sync.SessionSync._derive_session_kind.__doc__
    assert "no\n        per-message column" not in doc
    assert "messages.session_kind" in doc
    assert "v10_session_kind" in doc


def test_derivation_still_reads_any_carrying_record(tmp_path):
    import json
    from claude_session_db.jsonl_records import JSONLParser
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps({
        "type": "assistant", "uuid": "a1", "sessionId": "s1",
        "sessionKind": "bg", "timestamp": "2026-09-01T00:00:00.000Z",
        "message": {"id": "m", "model": "claude-opus-5", "content": []}}))
    recs = JSONLParser(tmp_path).parse_file(p)
    assert sync.SessionSync._derive_session_kind(recs) == "bg"
