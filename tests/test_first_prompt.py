"""`sessions.first_prompt` — picked by the v9 rule, not the string-only one.

v9 established the rule for classifying a user record: it is a prompt UNLESS it
carries a `tool_result` block, and `prompt_text` joins every text block of a
list-shaped prompt. `messages.message_type` was corrected to that rule and
backfilled — and `first_prompt` was left behind on `is_direct_prompt`, which is
True only for bare STRING content. 132 of 2,684 main sessions therefore carried
the wrong first prompt (an image paste, a document attachment, a multi-block
prompt: all skipped, so the SECOND prompt — or none — was stored).

Both derivation sites are covered: the main session and the sidechain child
(whose seed prompt is the Agent dispatch prompt).
"""
from __future__ import annotations

import pathlib

from claude_session_db import postgres, sync
from claude_session_db.jsonl_records import UserMessage
from claude_session_db.sync import SessionSync


def _user(content, **extra):
    return UserMessage.from_dict({
        "type": "user", "uuid": extra.pop("uuid", "u1"), "sessionId": "s1",
        "timestamp": "2026-09-01T00:00:00.000Z", "isSidechain": False,
        "message": {"role": "user", "content": content}, **extra})


IMAGE_PROMPT = [
    {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                 "data": "iVBORw0KG"}},
    {"type": "text", "text": "what is wrong with this chart?"},
]
TOOL_RESULT = [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]


def test_a_list_shaped_prompt_is_the_first_prompt():
    """The exact 132-session regression."""
    users = [_user(IMAGE_PROMPT, uuid="u1"), _user("second prompt", uuid="u2")]
    assert SessionSync._first_prompt(users) == "what is wrong with this chart?"


def test_a_string_prompt_still_works():
    assert SessionSync._first_prompt([_user("hello")]) == "hello"


def test_a_tool_result_is_never_the_first_prompt():
    users = [_user(TOOL_RESULT, uuid="u0"), _user("the real one", uuid="u1")]
    assert SessionSync._first_prompt(users) == "the real one"


def test_meta_records_are_skipped():
    users = [_user("<system reminder>", uuid="u0", isMeta=True),
             _user("the real one", uuid="u1")]
    assert SessionSync._first_prompt(users) == "the real one"


def test_a_multi_block_prompt_is_not_truncated_to_its_first_paragraph():
    users = [_user([{"type": "text", "text": "one"}, {"type": "text", "text": "two"}])]
    assert SessionSync._first_prompt(users) == "one\ntwo"


def test_no_prompt_at_all_is_none():
    assert SessionSync._first_prompt([_user(TOOL_RESULT)]) is None
    assert SessionSync._first_prompt([]) is None


def test_neither_derivation_site_uses_the_string_only_predicate():
    """Both `_upsert_session` and `_upsert_subagent_session` used it."""
    code = "\n".join(l for l in pathlib.Path(sync.__file__).read_text().splitlines()
                     if not l.lstrip().startswith("#"))
    assert "u.is_direct_prompt and not u.is_meta" not in code
    assert code.count("self._first_prompt(users)") == 2


# --- the backfill ----------------------------------------------------------

def _bf():
    return next(b for b in postgres.BACKFILLS if b["key"] == "v10_first_prompt")


def test_backfill_is_registered():
    assert _bf()["desc"]


def test_backfill_walks_the_sessions_primary_key():
    """Resumable from a cursor in `metadata`, like every other backfill."""
    sql = _bf()["sql"]
    assert "WHERE session_id > %(after)s ORDER BY session_id LIMIT %(limit)s" in sql
    assert "max(session_id) FROM batch" in sql


def test_backfill_reads_messages_not_the_files():
    sql = _bf()["sql"]
    assert "FROM messages" in sql
    assert "message_type = 'prompt'" in sql, "messages.message_type IS the v9 rule"


def test_backfill_takes_the_earliest_non_meta_main_chain_prompt():
    sql = _bf()["sql"]
    assert "NOT is_meta" in sql
    assert "NOT is_sidechain" in sql
    assert "ORDER BY ts NULLS LAST, uuid" in sql
    assert "LIMIT 1" in sql


def test_backfill_is_idempotent_and_only_writes_a_difference():
    assert "first_prompt IS DISTINCT FROM w.prompt_text" in _bf()["sql"]


def test_backfill_never_touches_child_session_rows():
    """A child row's messages live under the PARENT session_id keyed by
    agent_id, so this query cannot see them — it must not pretend to."""
    assert "WHERE NOT b.is_subagent" in _bf()["sql"]


def test_backfill_deletes_nothing():
    sql = _bf()["sql"].upper()
    assert "DELETE" not in sql and "TRUNCATE" not in sql


def test_backfill_is_ordered_after_the_v9_relabel():
    """It reads messages.message_type, which the v9 relabel corrects."""
    keys = [b["key"] for b in postgres.BACKFILLS]
    assert keys.index("v9_relabel_list_content_prompts") < keys.index("v10_first_prompt")
