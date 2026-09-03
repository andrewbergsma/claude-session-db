"""Unknown assistant content blocks are kept, under their own type.

`parse_content_block` returned None for anything that was not thinking / text /
tool_use, and `sync._insert_records` skips a None. Two losses, one of them
silent and worse than the obvious one:

  1. the block itself was dropped. Claude Code's `fallback` block (v2.1.215+) —
     `{"type":"fallback","from":{"model":…},"to":{"model":…}}`, the marker for a
     server-side model fallback, exactly what a cost or reliability lens wants —
     went that way.
  2. `block_index` comes from `enumerate(msg.content_blocks)` over the FILTERED
     list, so dropping a block shifted every later block in that message down
     one index. The ordering of the blocks that WERE kept was quietly wrong.
"""
from __future__ import annotations

from claude_session_db import postgres
from claude_session_db.jsonl_records import (
    AssistantMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    UnknownBlock,
    parse_content_block,
)
from claude_session_db.sync import SessionSync


# Verbatim from the corpus (4 occurrences in a 30-day scan).
FALLBACK = {"type": "fallback",
            "from": {"model": "claude-fable-5"},
            "to": {"model": "claude-opus-4-8"}}


def test_fallback_block_is_parsed_not_dropped():
    blk = parse_content_block(FALLBACK)
    assert isinstance(blk, UnknownBlock)
    assert blk.block_type == "fallback"
    assert blk.payload == FALLBACK


def test_known_blocks_are_unaffected():
    assert isinstance(parse_content_block({"type": "text", "text": "x"}), TextBlock)
    assert isinstance(parse_content_block({"type": "thinking", "thinking": "x",
                                           "signature": "s"}), ThinkingBlock)
    assert isinstance(parse_content_block({"type": "tool_use", "id": "t", "name": "Bash",
                                           "input": {}}), ToolUseBlock)


def test_a_type_never_seen_before_is_also_kept():
    """There will be a next one; this branch is so it costs nothing."""
    blk = parse_content_block({"type": "some_future_block", "data": [1, 2, 3]})
    assert isinstance(blk, UnknownBlock)
    assert blk.block_type == "some_future_block"
    assert blk.payload["data"] == [1, 2, 3]


def test_block_without_a_type_still_yields_a_block():
    blk = parse_content_block({"no_type_field": True})
    assert isinstance(blk, UnknownBlock)
    assert blk.block_type == "unknown"


def test_non_dict_is_still_none():
    assert parse_content_block("not a block") is None


def _assistant(content):
    return AssistantMessage.from_dict({
        "type": "assistant", "uuid": "a1", "sessionId": "s1", "parentUuid": None,
        "timestamp": "2026-08-01T00:00:00.000Z", "cwd": "/x", "gitBranch": "m",
        "version": "2.1.258", "isSidechain": False, "userType": "external",
        "message": {"id": "m1", "type": "message", "role": "assistant",
                    "model": "claude-opus-5", "stop_reason": "end_turn",
                    "stop_sequence": None, "content": content,
                    "usage": {"input_tokens": 1, "output_tokens": 1}}})


def test_block_index_is_no_longer_shifted_by_an_unknown_block():
    """The silent half of the bug: with `fallback` dropped, the tool_use that
    followed it was recorded at index 1 instead of 2."""
    msg = _assistant([{"type": "text", "text": "before"},
                      FALLBACK,
                      {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}])
    assert len(msg.content_blocks) == 3
    rows = [SessionSync._content_block_row(None, "a1", "s1", i, b, "f.jsonl")
            for i, b in enumerate(msg.content_blocks)]
    assert [(r["block_index"], r["block_type"]) for r in rows] == [
        (0, "text"), (1, "fallback"), (2, "tool_use")]


def test_row_carries_the_real_type_and_the_verbatim_payload():
    row = SessionSync._content_block_row(
        None, "a1", "s1", 0, parse_content_block(FALLBACK), "f.jsonl")
    assert row["block_type"] == "fallback"     # NOT a generic "unknown" bucket
    assert row["block_payload"] == FALLBACK
    assert row["char_count"] == len(
        __import__("json").dumps(FALLBACK, default=str))
    # it is not a tool_use, so the tool columns stay clean
    assert row["tool_use_id"] is None and row["tool_name"] is None
    assert row["tool_input"] is None


def test_unknown_blocks_are_not_counted_as_text_or_tools():
    """A fallback marker is neither narration nor a tool call; letting it leak
    into either would distort every token/tool lens that reads them."""
    msg = _assistant([{"type": "text", "text": "hi"}, FALLBACK])
    assert len(msg.text_blocks) == 1
    assert msg.tool_use_blocks == []
    assert msg.full_text == "hi"


def test_schema_has_the_payload_column_and_it_is_bound():
    import inspect
    assert "ADD COLUMN block_payload JSONB" in postgres.SCHEMA_SQL
    src = inspect.getsource(postgres.SessionArchive.insert_content_blocks)
    assert '"block_payload"' in src
    assert "block_payload" in src.split("_batch_insert")[1]   # in the JSONB set


def test_the_fallback_block_is_dated_to_its_first_observation():
    """It was dated to v2.1.247 — the release csd happened to notice it in.
    A corpus scan puts the earliest `fallback` block at v2.1.215, and the wrong
    date makes every "when did this start" question answer wrong."""
    from claude_session_db import jsonl_records, postgres
    doc = jsonl_records.UnknownBlock.__doc__
    assert "v2.1.215" in doc
    assert "2.1.247" not in doc
    assert "2.1.247" not in postgres.SCHEMA_SQL
