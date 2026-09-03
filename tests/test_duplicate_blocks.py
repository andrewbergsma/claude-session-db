"""Cross-file duplication of content_blocks / tool_results (schema v10).

`messages` inserts ON CONFLICT (uuid) DO NOTHING, so a record present in two
transcripts yields ONE message row — owned by whichever file wrote it first.
`content_blocks` and `tool_results` have no uniqueness at all, and
`clear_file_data` deletes only by `source_file`, so the second file APPENDED a
second set of blocks and results beside the first. 2.7% of recent assistant
messages carried them, and 3,339 duplicated (message_uuid, tool_use_id) pairs
were measured across 300 recent sessions; sessions.tool_use_count and
error_count were inflated by exactly that.

Three halves to the fix, pinned here:
  1. ingest SKIPS block/result rows for a message owned by another file
     (skip, not delete — deleting would destroy rows another file's per-file
     clear/insert cycle owns);
  2. the aggregates count tool_use IDENTITY, so historical duplicates cannot
     inflate them;
  3. `v_duplicate_blocks` makes the history visible instead of silent.
"""
from __future__ import annotations

import re

from claude_session_db import postgres
from claude_session_db.sync import SessionSync, SyncStats


class _FakeArchive:
    """Minimal stand-in: records what got inserted, answers ownership."""

    def __init__(self, owned_elsewhere=()):
        self._owned = set(owned_elsewhere)
        self.asked = None
        self.content_blocks = None
        self.tool_results = None
        self.messages = None

    def message_uuids_owned_elsewhere(self, uuids, source_file):
        self.asked = (list(uuids), source_file)
        return {u for u in uuids if u in self._owned}

    def insert_messages(self, rows):
        self.messages = rows

    def insert_content_blocks(self, rows):
        self.content_blocks = rows

    def insert_tool_results(self, rows):
        self.tool_results = rows

    def __getattr__(self, name):
        if name.startswith(("insert_", "upsert_")):
            return lambda *a, **k: None
        raise AttributeError(name)


def _sync_with(archive):
    s = SessionSync.__new__(SessionSync)
    s.archive = archive
    s.verbose = False
    return s


ASSISTANT = {
    "type": "assistant", "uuid": "m1", "sessionId": "s1",
    "timestamp": "2026-09-01T00:00:00.000Z",
    "message": {"id": "msg_1", "model": "claude-opus-5",
                "content": [{"type": "tool_use", "id": "toolu_1",
                             "name": "Bash", "input": {"command": "ls"}}]},
}
USER_RESULT = {
    "type": "user", "uuid": "u1", "sessionId": "s1",
    "timestamp": "2026-09-01T00:00:01.000Z",
    "message": {"role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                             "content": "a\nb"}]},
}


def _records(tmp_path, raw):
    import json
    from claude_session_db.jsonl_records import JSONLParser
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in raw))
    return JSONLParser(tmp_path).parse_file(p)


def test_blocks_and_results_are_written_when_this_file_owns_the_message(tmp_path):
    arch = _FakeArchive()
    stats = SyncStats()
    _sync_with(arch)._insert_records(
        _records(tmp_path, [ASSISTANT, USER_RESULT]), "/f/a.jsonl", "s1", {}, stats)
    assert len(arch.content_blocks) == 1
    assert len(arch.tool_results) == 1
    assert stats.duplicate_rows_skipped == 0


def test_blocks_and_results_are_skipped_when_another_file_owns_the_message(tmp_path):
    """The duplication defect, at its source."""
    arch = _FakeArchive(owned_elsewhere={"m1", "u1"})
    stats = SyncStats()
    _sync_with(arch)._insert_records(
        _records(tmp_path, [ASSISTANT, USER_RESULT]), "/f/b.jsonl", "s1", {}, stats)
    assert arch.content_blocks == []
    assert arch.tool_results == []
    assert stats.duplicate_rows_skipped == 2


def test_only_the_foreign_message_is_skipped(tmp_path):
    """Per-message, not per-file: a file that shares ONE message with another
    still writes the blocks for its own."""
    second = dict(ASSISTANT, uuid="m2")
    second["message"] = {"id": "msg_2", "model": "claude-opus-5",
                         "content": [{"type": "text", "text": "hi"}]}
    arch = _FakeArchive(owned_elsewhere={"m1"})
    stats = SyncStats()
    _sync_with(arch)._insert_records(
        _records(tmp_path, [ASSISTANT, second]), "/f/b.jsonl", "s1", stats=stats,
        overflow={})
    assert [r["message_uuid"] for r in arch.content_blocks] == ["m2"]
    assert stats.duplicate_rows_skipped == 1


def test_messages_are_inserted_before_ownership_is_asked(tmp_path):
    """Order is load-bearing: this file's own messages must exist before the
    archive can say who owns them, or every message looks foreign."""
    calls = []

    class Ordered(_FakeArchive):
        def insert_messages(self, rows):
            calls.append("messages")

        def message_uuids_owned_elsewhere(self, uuids, source_file):
            calls.append("ownership")
            return set()

    _sync_with(Ordered())._insert_records(
        _records(tmp_path, [ASSISTANT]), "/f/a.jsonl", "s1", {}, SyncStats())
    assert calls == ["messages", "ownership"]


def test_ownership_query_never_deletes():
    """The chosen approach is SKIP. Nothing in the seam may issue a DELETE."""
    import inspect
    doc = postgres.SessionArchive.message_uuids_owned_elsewhere.__doc__ or ""
    assert "SKIP, not delete" in doc
    body = inspect.getsource(postgres.SessionArchive.message_uuids_owned_elsewhere)
    code = body.split('"""')[2]             # strip the docstring, keep the code
    assert "SELECT uuid FROM messages" in code
    for verb in ("DELETE", "UPDATE", "TRUNCATE", "INSERT"):
        assert verb not in code.upper(), f"the ownership probe must be read-only ({verb})"


def test_stats_surface_the_skip():
    s = SyncStats()
    assert "Duplicate block/result" not in str(s)
    s.duplicate_rows_skipped = 12
    assert "Duplicate block/result rows skipped: 12" in str(s)
    assert "v_duplicate_blocks" in str(s)


# --- aggregates ------------------------------------------------------------

def _aggregate_sql():
    import inspect
    return inspect.getsource(postgres.SessionArchive.recompute_session_aggregates)


def test_tool_use_count_is_distinct_by_tool_use_id():
    sql = _aggregate_sql()
    assert "count(DISTINCT coalesce(nullif(cb.tool_use_id, '')" in sql
    # and never the old row count
    assert not re.search(r"count\(\*\) AS cnt\s*\n\s*FROM content_blocks", sql)


def test_error_count_is_distinct_by_message_and_tool_use():
    assert "count(DISTINCT (tr.message_uuid, tr.tool_use_id))" in _aggregate_sql()


def test_child_session_aggregates_are_distinct_too():
    sql = _aggregate_sql()
    # both the main and the subagent statement must use identity counts
    assert sql.count("count(DISTINCT (tr.message_uuid, tr.tool_use_id))") == 3
    assert sql.count("'blk:' || cb.block_id") >= 3


def test_a_null_tool_use_id_still_counts():
    """count(DISTINCT) drops NULLs — a tool_use block with no id would vanish
    from the count entirely without the block_id fallback."""
    assert "'blk:' || cb.block_id" in _aggregate_sql()


# --- the diagnostic view ---------------------------------------------------

def test_duplicate_view_is_declared_with_the_documented_columns():
    sql = postgres.VIEWS_SQL
    assert "CREATE OR REPLACE VIEW v_duplicate_blocks AS" in sql
    for col in ("message_uuid", "session_id", "kind", "source_files", "row_count"):
        assert col in sql


def test_duplicate_view_covers_both_tables():
    sql = postgres.VIEWS_SQL.split("v_duplicate_blocks AS", 1)[1]
    head = sql.split("CREATE OR REPLACE VIEW", 1)[0]
    assert "FROM content_blocks" in head and "FROM tool_results" in head
    assert head.count("count(DISTINCT") >= 2


def test_operator_cleanup_recipe_is_documented_in_code():
    """The historical duplicates are NOT deleted automatically; the operator
    needs the recipe where the view is defined."""
    sql = postgres.VIEWS_SQL
    assert "OPERATOR CLEANUP RECIPE" in sql
    assert "LIMIT 20000" in sql, "must be batched — a bulk DELETE convoyed this DB once"
    assert "cb.source_file <> m.source_file" in sql


def test_schema_version_is_10():
    assert postgres.SCHEMA_VERSION == 10
