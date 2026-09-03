"""Schema-v9 `messages` columns: effort, sessionKind, and usage sub-fields.

`effort` sits at the TOP LEVEL of 98.5% of assistant records (235,687 of
239,367 in a 30-day scan) and csd never parsed it — the single biggest
per-turn cost/quality lever, visible only by digging into `raw`. Same story for
the three usage sub-fields Claude Code added in the same window:
`output_tokens_details.thinking_tokens` (55%), `server_tool_use` (64%),
`iterations` (64%).

The backfill tests here are about SHAPE, not a live database: the properties
that matter (PK-ordered walk, per-batch commit, resumable cursor, idempotent
guard) are all visible in the SQL and the loop.
"""
from __future__ import annotations

import json

import pytest

from claude_session_db import postgres
from claude_session_db.jsonl_records import AssistantMessage, Usage


ASSISTANT = {
    "type": "assistant", "uuid": "a1", "sessionId": "s1", "parentUuid": None,
    "timestamp": "2026-08-01T00:00:00.000Z", "cwd": "/x", "gitBranch": "main",
    "version": "2.1.258", "isSidechain": False, "userType": "external",
    "effort": "high", "sessionKind": "bg",
    "message": {
        "id": "msg_1", "type": "message", "role": "assistant",
        "model": "claude-opus-5", "stop_reason": "end_turn", "stop_sequence": None,
        "content": [{"type": "text", "text": "hi"}],
        "usage": {
            "input_tokens": 10, "output_tokens": 200,
            "cache_creation_input_tokens": 5, "cache_read_input_tokens": 900,
            "cache_creation": {"ephemeral_5m_input_tokens": 5,
                               "ephemeral_1h_input_tokens": 0},
            "output_tokens_details": {"thinking_tokens": 150},
            "server_tool_use": {"web_search_requests": 2},
            "iterations": 3, "service_tier": "standard",
            "inference_geo": "us", "speed": "standard"},
    },
}


def test_effort_is_parsed():
    assert AssistantMessage.from_dict(ASSISTANT).effort == "high"


def test_effort_absent_is_none():
    d = {k: v for k, v in ASSISTANT.items() if k != "effort"}
    assert AssistantMessage.from_dict(d).effort is None


def test_session_kind_is_parsed():
    assert AssistantMessage.from_dict(ASSISTANT).session_kind == "bg"


def test_usage_subfields_are_promoted():
    u = AssistantMessage.from_dict(ASSISTANT).usage
    assert u.thinking_tokens == 150
    assert u.server_tool_use == {"web_search_requests": 2}
    assert u.iterations == 3


def test_usage_subfields_absent_are_none_not_zero():
    """0 iterations and "we were never told" are different facts."""
    u = Usage.from_dict({"input_tokens": 1, "output_tokens": 1})
    assert u.thinking_tokens is None
    assert u.server_tool_use is None
    assert u.iterations is None


def test_output_tokens_details_without_thinking_tokens():
    u = Usage.from_dict({"input_tokens": 1, "output_tokens": 1,
                         "output_tokens_details": {}})
    assert u.thinking_tokens is None


def test_non_dict_output_tokens_details_does_not_raise():
    u = Usage.from_dict({"input_tokens": 1, "output_tokens": 1,
                         "output_tokens_details": "surprise"})
    assert u.thinking_tokens is None


def test_raw_usage_is_still_kept_whole():
    """The promoted columns are a convenience, never a replacement — the JSONB
    escape hatch is what absorbs the NEXT usage field."""
    u = AssistantMessage.from_dict(ASSISTANT).usage
    assert u.raw == ASSISTANT["message"]["usage"]


@pytest.mark.parametrize("col", ["effort", "session_kind", "thinking_tokens",
                                 "server_tool_use", "iterations"])
def test_column_declared_in_migration(col):
    assert col in postgres.SCHEMA_SQL


def test_message_columns_are_bound_on_insert():
    import inspect
    src = inspect.getsource(postgres.SessionArchive.insert_messages)
    for col in ("effort", "session_kind", "thinking_tokens", "server_tool_use",
                "iterations"):
        assert f'"{col}"' in src, f"{col} declared but never bound"
    # a raw dict bound to a JSONB column is a psycopg ProgrammingError
    assert '"server_tool_use"' in src.split("jsonb_cols")[1]


def test_migration_is_guarded_and_adds_only():
    sql = postgres.SCHEMA_SQL
    assert "column_name = 'effort'" in sql          # fires exactly once
    assert "ADD COLUMN effort TEXT" in sql
    assert "DROP COLUMN" not in sql


def test_no_generated_columns_on_messages():
    """PG16 has only STORED generated columns, and adding one rewrites the
    table — a multi-GB ACCESS EXCLUSIVE rewrite of 1.3M rows inside a 5-minute
    sweep tick. Plain columns + a bounded backfill instead."""
    assert "GENERATED ALWAYS AS" not in postgres.SCHEMA_SQL


# --- the backfill machinery ------------------------------------------------

def _backfill(key):
    return next(b for b in postgres.BACKFILLS if b["key"] == key)


def test_effort_backfill_is_registered():
    assert _backfill("v9_message_effort_usage")


def test_backfill_walks_the_primary_key():
    """Not `WHERE effort IS NULL` — that re-scans the whole table every batch,
    making the pass O(n^2/batch). `uuid > cursor ORDER BY uuid` rides the PK."""
    sql = _backfill("v9_message_effort_usage")["sql"]
    assert "WHERE uuid > %(after)s ORDER BY uuid LIMIT %(limit)s" in sql
    assert "next_cursor" in sql and "scanned" in sql and "updated" in sql


def test_backfill_is_idempotent():
    """Re-running must write nothing, or the heap bloats on every sweep."""
    assert "IS DISTINCT FROM" in _backfill("v9_message_effort_usage")["sql"]


def test_backfill_reads_the_documented_json_paths():
    sql = _backfill("v9_message_effort_usage")["sql"]
    assert "raw->>'effort'" in sql
    assert "raw->>'sessionKind'" in sql
    assert "'output_tokens_details'" in sql and "'thinking_tokens'" in sql
    assert "usage->'server_tool_use'" in sql
    assert "usage->>'iterations'" in sql


def test_backfill_placeholders_are_psycopg_safe():
    """A lone `%` anywhere in the string — comments included — raises before
    the query reaches Postgres. See tests/test_sql_placeholders.py."""
    from psycopg._queries import _split_query
    for b in postgres.BACKFILLS:
        _split_query(b["sql"].encode())


def test_backfill_is_bounded_and_resumable():
    import inspect
    src = inspect.getsource(postgres.SessionArchive.run_backfills)
    assert "deadline" in src, "must be time-bounded — it runs on the sweep path"
    assert "cursor_key" in src, "must persist a cursor to resume"
    assert "conn.commit()" in src, "must commit per batch, not hold one txn"
    assert "psycopg.Error" in src, "a failed backfill must not stop ingest"


def test_backfill_budget_is_smaller_than_a_sweep_interval():
    assert postgres.BACKFILL_MAX_SECONDS <= 60
    assert postgres.BACKFILL_BATCH_ROWS >= 1000
