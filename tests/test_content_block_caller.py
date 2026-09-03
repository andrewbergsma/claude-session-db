"""`content_blocks.caller` — parsed since forever, written by nobody.

`jsonl_records.ToolUseBlock.from_dict` has always parsed `tool_use.caller` into
a `ToolUseCaller`, and `sync._content_block_row` never bound it — so the field
was dropped on 100% of tool_use blocks. Today it is `{"type":"direct"}` on
every block in the corpus, which is precisely the kind of field that is
uninteresting right up until it is not (see `fallback` blocks, v9).

v10 stores it VERBATIM as JSONB, and — unusually for a content_blocks column —
recovers the history, because `messages.raw` holds the assistant record whole.
"""
from __future__ import annotations

import inspect

from claude_session_db import postgres, sync
from claude_session_db.jsonl_records import ToolUseBlock, parse_content_block


TOOL_USE = {"type": "tool_use", "id": "toolu_1", "name": "Bash",
            "input": {"command": "ls"}, "caller": {"type": "direct"}}


# --- parse -----------------------------------------------------------------

def test_the_caller_object_is_kept_verbatim():
    blk = ToolUseBlock.from_dict(TOOL_USE)
    assert blk.caller.type == "direct"
    assert blk.caller.raw == {"type": "direct"}


def test_an_unfamiliar_caller_shape_survives_whole():
    """The escape hatch earns its keep on the shape nobody has seen yet."""
    blk = ToolUseBlock.from_dict(dict(TOOL_USE, caller={
        "type": "delegated", "agentId": "a1", "depth": 2}))
    assert blk.caller.type == "delegated"
    assert blk.caller.raw == {"type": "delegated", "agentId": "a1", "depth": 2}


def test_an_absent_caller_is_distinguishable_from_a_direct_one():
    """`type` still reads "direct" for convenience, but `raw` is empty — the
    archive must not invent a field the transcript did not carry."""
    blk = ToolUseBlock.from_dict({k: v for k, v in TOOL_USE.items() if k != "caller"})
    assert blk.caller.type == "direct"
    assert blk.caller.raw == {}


# --- the row ---------------------------------------------------------------

def _row(block):
    return sync.SessionSync._content_block_row(
        None, "m1", "s1", 0, block, "/f/a.jsonl")


def test_the_row_carries_the_caller():
    assert _row(parse_content_block(TOOL_USE))["caller"] == {"type": "direct"}


def test_an_absent_caller_is_null_not_a_synthesized_default():
    block = parse_content_block({k: v for k, v in TOOL_USE.items() if k != "caller"})
    assert _row(block)["caller"] is None


def test_non_tool_use_blocks_have_no_caller():
    assert _row(parse_content_block({"type": "text", "text": "hi"}))["caller"] is None
    assert _row(parse_content_block(
        {"type": "fallback", "from": {"model": "a"}}))["caller"] is None


# --- schema ----------------------------------------------------------------

def test_column_is_declared_guarded_and_additive():
    sql = postgres.SCHEMA_SQL
    assert "ALTER TABLE content_blocks ADD COLUMN caller JSONB" in sql
    assert "column_name = 'caller'" in sql, "the ALTER must be catalog-guarded"
    assert "DROP COLUMN" not in sql


def test_it_is_bound_and_wrapped_as_jsonb():
    src = inspect.getsource(postgres.SessionArchive.insert_content_blocks)
    assert '"caller"' in src
    assert '{"tool_input", "block_payload", "caller"}' in src


def test_it_is_deliberately_unindexed_and_says_why():
    sql = postgres.SCHEMA_SQL
    assert "idx_cb_caller" not in sql
    assert "Deliberately UNINDEXED" in sql


# --- the backfill ----------------------------------------------------------

def _bf():
    return next(b for b in postgres.BACKFILLS if b["key"] == "v10_content_block_caller")


def test_backfill_is_registered():
    assert _bf()["desc"]


def test_backfill_walks_the_messages_primary_key():
    sql = _bf()["sql"]
    assert "WHERE uuid > %(after)s ORDER BY uuid LIMIT %(limit)s" in sql
    assert "max(uuid) FROM batch" in sql


def test_backfill_matches_on_tool_use_id_not_block_index():
    """Pre-v9, a dropped unknown block shifted every later block_index in the
    message — the historical index does not address the raw array."""
    sql = _bf()["sql"]
    assert "cb.tool_use_id  = blk.tool_use_id" in sql
    assert "block_index" not in sql


def test_backfill_reads_the_raw_message_content():
    sql = _bf()["sql"]
    assert "b.raw->'message'->'content'" in sql
    assert "e->'caller'" in sql
    assert "e ? 'caller'" in sql, "only where the record actually has one"


def test_backfill_guards_against_non_array_content():
    """jsonb_array_elements in a LATERAL runs before the WHERE could filter it;
    on string content it raises and kills the batch."""
    sql = _bf()["sql"]
    assert "CASE WHEN jsonb_typeof(b.raw->'message'->'content') = 'array'" in sql
    assert "ELSE '[]'::jsonb END" in sql


def test_backfill_only_fills_nulls():
    assert "cb.caller IS NULL" in _bf()["sql"]
    assert "cb.block_type   = 'tool_use'" in _bf()["sql"]


def test_backfill_skips_blocks_with_no_id():
    """A tool_use with an empty id cannot be matched, and matching on '' would
    smear one caller across every id-less block in the message."""
    assert "coalesce(e->>'id', '') <> ''" in _bf()["sql"]


def test_backfill_deletes_nothing():
    sql = _bf()["sql"].upper()
    assert "DELETE" not in sql and "TRUNCATE" not in sql
