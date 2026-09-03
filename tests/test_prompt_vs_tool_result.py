"""User records: prompt vs tool_result, classified on evidence not shape.

`sync._user_row` typed a user record as
`"prompt" if msg.is_direct_prompt else "tool_result"`, and `is_direct_prompt`
is True only when `message.content` is a bare STRING. So every user prompt with
list-shaped content — an image paste, a document attachment, any multi-block
prompt — was filed as a tool_result.

That is not cosmetic. Those rows drop out of:
  * `sessions.user_prompt_count` (the recompute filters on
    `message_type='prompt'`),
  * `first_prompt` derivation,
  * the reconcile gate's empty/trivial heuristics, which decide whether a
    session is even worth summarizing,
  * every "what did the user actually ask" query in the console and angles.

A user record is one of exactly two things, and the only positive evidence for
"tool result" is a `tool_result` block. Content shape is not evidence.
"""
from __future__ import annotations

import json

from claude_session_db import postgres
from claude_session_db.jsonl_records import UserMessage


def _msg(content, **extra):
    return UserMessage.from_dict({
        "type": "user", "uuid": "u1", "sessionId": "s1", "parentUuid": None,
        "timestamp": "2026-08-01T00:00:00.000Z", "cwd": "/x", "gitBranch": "main",
        "version": "2.1.258", "isSidechain": False, "userType": "external",
        "message": {"role": "user", "content": content}, **extra})


def _type_of(msg):
    """The classifier as sync._user_row applies it."""
    return "tool_result" if msg.is_tool_result else "prompt"


# --- the regression --------------------------------------------------------

def test_image_paste_prompt_is_a_prompt():
    m = _msg([{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                           "data": "iVBORw0KG"}},
              {"type": "text", "text": "what is wrong with this chart?"}])
    assert m.is_direct_prompt is False        # it is a LIST — the old predicate
    assert _type_of(m) == "prompt"            # ...but it is plainly a prompt
    assert m.prompt_text == "what is wrong with this chart?"


def test_document_attachment_prompt_is_a_prompt():
    m = _msg([{"type": "document", "source": {"type": "file", "file_id": "f1"}},
              {"type": "text", "text": "summarize this"}])
    assert _type_of(m) == "prompt"
    assert m.prompt_text == "summarize this"


def test_list_of_text_blocks_is_a_prompt():
    m = _msg([{"type": "text", "text": "first"}, {"type": "text", "text": "second"}])
    assert _type_of(m) == "prompt"


def test_multi_text_block_prompt_is_joined_not_truncated():
    """The old prompt_text returned only the FIRST text block, silently
    truncating a multi-block prompt to its first paragraph."""
    m = _msg([{"type": "text", "text": "first"},
              {"type": "image", "source": {}},
              {"type": "text", "text": "second"}])
    assert m.prompt_text == "first\nsecond"


# --- what must NOT change --------------------------------------------------

def test_string_content_is_still_a_prompt():
    m = _msg("plain question")
    assert _type_of(m) == "prompt"
    assert m.prompt_text == "plain question"


def test_a_real_tool_result_is_still_a_tool_result():
    m = _msg([{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}])
    assert _type_of(m) == "tool_result"


def test_mixed_tool_result_and_text_is_a_tool_result():
    """A tool_result block is positive evidence and outranks accompanying text
    — this is the shape that must NOT be relabelled."""
    m = _msg([{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"},
              {"type": "text", "text": "and here is some narration"}])
    assert _type_of(m) == "tool_result"


def test_multiple_tool_results_in_one_message():
    m = _msg([{"type": "tool_result", "tool_use_id": "t1", "content": "a"},
              {"type": "tool_result", "tool_use_id": "t2", "content": "b"}])
    assert _type_of(m) == "tool_result"
    assert m.tool_result_ids == ["t1", "t2"]


def test_empty_list_content_is_a_prompt_not_a_tool_result():
    m = _msg([])
    assert _type_of(m) == "prompt"
    assert m.prompt_text is None


def test_prompt_text_is_none_when_there_is_no_text():
    m = _msg([{"type": "image", "source": {}}])
    assert _type_of(m) == "prompt"
    assert m.prompt_text is None


def test_sync_uses_the_new_predicate():
    import pathlib
    from claude_session_db import sync
    # code lines only — the old predicate is quoted in the explanatory comment
    code = "\n".join(l for l in pathlib.Path(sync.__file__).read_text().splitlines()
                     if not l.lstrip().startswith("#"))
    assert '"tool_result" if msg.is_tool_result else "prompt"' in code
    assert '"prompt" if msg.is_direct_prompt else "tool_result"' not in code


# --- the backfill ----------------------------------------------------------

def _relabel():
    return next(b for b in postgres.BACKFILLS
                if b["key"] == "v9_relabel_list_content_prompts")


def test_relabel_backfill_is_registered():
    assert _relabel()


def test_relabel_backfill_only_touches_rows_with_no_tool_result_block():
    """Double-guarded. It must be impossible for this to relabel a genuine
    tool result, so the predicate is positive-evidence-absence in the RAW
    record, not a heuristic over the derived columns."""
    sql = _relabel()["sql"]
    assert "NOT EXISTS" in sql
    assert "e->>'type' = 'tool_result'" in sql
    assert "m.message_type = 'tool_result'" in sql   # only ever corrects, never re-types
    assert "m.role = 'user'" in sql


def test_relabel_backfill_only_touches_array_content():
    assert "jsonb_typeof(b.raw->'message'->'content') = 'array'" in _relabel()["sql"]


def test_relabel_backfill_fills_prompt_text_without_overwriting():
    """coalesce, not assignment: a prompt_text already derived stays."""
    assert "coalesce(m.prompt_text, c.txt)" in _relabel()["sql"]


def test_relabel_backfill_joins_text_blocks_in_order():
    """Same rule as the parser — a multi-block prompt must not be truncated to
    its first paragraph in the archive either."""
    sql = _relabel()["sql"]
    assert "string_agg" in sql and "ORDER BY ord" in sql


def test_relabel_backfill_walks_the_primary_key():
    sql = _relabel()["sql"]
    assert "WHERE uuid > %(after)s ORDER BY uuid LIMIT %(limit)s" in sql
