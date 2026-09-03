"""`sessions.worktree_active` — the worktree EXIT, which was unrecordable.

`worktree-state` signals leaving a worktree with `worktreeSession: null` — 38%
of the records in the live archive — and the session upsert COALESCEs, so a
null can never clear `sessions.worktree_session`. A session that had ever
entered a worktree read as still inside it forever.

v10 splits the two questions:
  * `worktree_session` — the last BINDING ever seen (unchanged, COALESCE);
  * `worktree_active`  — the current STATE, written last-observation-wins.
        NULL  no worktree-state record has ever been seen
        true  the last one carried a worktreeSession object
        false the last one carried `worktreeSession: null`

The second payload shape (`enteredExisting: true`, no originalBranch /
originalHeadCommit — the session joined a worktree that already existed) is a
binding like any other and is pinned here so a key-count guard is never
reintroduced.
"""
from __future__ import annotations

import json

import pytest

from claude_session_db import postgres
from claude_session_db.jsonl_records import JSONLParser
from claude_session_db.sync import SessionSync


def _derive(tmp_path, raw):
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in raw))
    return SessionSync._derive_from_session_records(JSONLParser(tmp_path).parse_file(p))


FULL = {"type": "worktree-state", "sessionId": "s",
        "worktreeSession": {
            "originalCwd": "/Users/a/GitHub/infra",
            "preEnterOriginalCwd": "/Users/a/GitHub/infra",
            "worktreePath": "/Users/a/GitHub/infra/.claude/worktrees/net-v1",
            "worktreeName": "net-v1", "worktreeBranch": "worktree-net-v1",
            "originalBranch": "main",
            "originalHeadCommit": "c5f866d586a76a4586ead2a83611def070bdf57d",
            "sessionId": "s"}}

# Verbatim from the corpus: no originalBranch, no originalHeadCommit.
ENTERED_EXISTING = {"type": "worktree-state", "sessionId": "s",
                    "worktreeSession": {
                        "originalCwd": "/Users/a/GitHub/curves",
                        "preEnterOriginalCwd": "/Users/a/GitHub/curves",
                        "worktreePath": "/Users/a/GitHub/curves/.claude/worktrees/ftc",
                        "worktreeName": "ftc", "worktreeBranch": "wt/ftc",
                        "sessionId": "s", "enteredExisting": True}}

EXIT = {"type": "worktree-state", "sessionId": "s", "worktreeSession": None}


def test_a_binding_sets_active_true(tmp_path):
    d = _derive(tmp_path, [FULL])
    assert d["worktree_active"] is True
    assert d["worktree_session"] == FULL["worktreeSession"]


def test_the_second_payload_shape_is_accepted(tmp_path):
    """`enteredExisting: true` with no originalBranch/originalHeadCommit."""
    d = _derive(tmp_path, [ENTERED_EXISTING])
    assert d["worktree_active"] is True
    assert d["worktree_session"] == ENTERED_EXISTING["worktreeSession"]
    assert d["worktree_session"]["enteredExisting"] is True


def test_a_null_payload_sets_active_false(tmp_path):
    """The exit signal. Before v10 this record changed nothing at all."""
    d = _derive(tmp_path, [EXIT])
    assert d["worktree_active"] is False


def test_exit_after_entry_keeps_the_binding_and_flips_the_state(tmp_path):
    """Latest-wins on the state; the binding is the last one ever SEEN, so the
    worktree the session was in is not lost when it leaves."""
    d = _derive(tmp_path, [FULL, EXIT])
    assert d["worktree_active"] is False
    assert d["worktree_session"] == FULL["worktreeSession"]


def test_re_entry_flips_it_back(tmp_path):
    d = _derive(tmp_path, [FULL, EXIT, ENTERED_EXISTING])
    assert d["worktree_active"] is True
    assert d["worktree_session"] == ENTERED_EXISTING["worktreeSession"]


def test_no_worktree_record_leaves_the_state_unknown(tmp_path):
    """NULL means 'never seen one', which is not the same as 'not in one'."""
    d = _derive(tmp_path, [{"type": "relocated", "sessionId": "s",
                            "relocatedCwd": "/tmp/x"}])
    assert "worktree_active" not in d
    assert "worktree_session" not in d


def test_a_record_with_no_worktree_session_key_is_still_skipped(tmp_path):
    """Absent key != null value: it says nothing, so it must set nothing."""
    d = _derive(tmp_path, [{"type": "worktree-state", "sessionId": "s"}])
    assert "worktree_active" not in d
    assert "worktree_session" not in d


# --- schema / upsert -------------------------------------------------------

def test_column_is_declared_guarded_and_bound():
    sql = postgres.SCHEMA_SQL
    assert "ADD COLUMN worktree_active BOOLEAN" in sql
    assert "column_name = 'worktree_active'" in sql, "the ALTER must be catalog-guarded"
    assert "DROP COLUMN" not in sql
    assert "worktree_active" in postgres.SessionArchive._SESSION_COLS


def test_the_column_is_last_wins_not_coalesce():
    """A COALESCE-only rule is exactly why the exit was unrecordable."""
    assert postgres.SessionArchive._SESSION_LAST_WINS_COLS == {"worktree_active"}
    sql = postgres.SessionArchive._session_upsert_sql(postgres.SessionArchive)
    assert ("worktree_active=CASE WHEN EXCLUDED.worktree_active IS NOT NULL "
            "THEN EXCLUDED.worktree_active ELSE sessions.worktree_active END") in sql
    assert "worktree_active=COALESCE(" not in sql


def test_worktree_session_keeps_coalesce_semantics():
    sql = postgres.SessionArchive._session_upsert_sql(postgres.SessionArchive)
    assert "worktree_session=COALESCE(EXCLUDED.worktree_session, sessions.worktree_session)" in sql


def test_an_upsert_without_the_column_does_not_erase_the_state():
    """`backfill_subagent_sessions` and any second file upsert rows that carry
    no worktree_active; those must leave the stored state alone."""
    sql = postgres.SessionArchive._session_upsert_sql(postgres.SessionArchive)
    assert "ELSE sessions.worktree_active END" in sql


def test_the_overview_view_exposes_both():
    v = postgres.VIEWS_SQL
    assert "s.worktree_session, s.worktree_active" in v


def test_binding_values_are_bound_as_jsonb_and_the_flag_is_not():
    j = postgres.SessionArchive._SESSION_JSONB_COLS
    assert "worktree_session" in j
    assert "worktree_active" not in j
