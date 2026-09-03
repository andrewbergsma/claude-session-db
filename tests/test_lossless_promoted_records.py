"""Losslessness for records that DO have a dedicated destination (schema v10).

The v9 catch-all closed the "no table at all" hole. It did not close the other
one: a record type with a promoted column is still lossy when

  * the promotion keeps ONE field and drops the rest —
    `bridge-session` kept `bridgeSessionId` and dropped `lastSequenceNum`,
    `ownerAccountUuid`, `ownerOrganizationUuid`, `noHistoryBackfill`;
    `queue-operation` has no column for `reason`; `last-prompt` has none for
    `explicit`; `attachments` kept the `attachment` sub-object and dropped the
    record's own top-level fields; or
  * the promotion is LATEST-WINS onto a single `sessions` column, so every
    earlier value — every title the model ever gave the session, every prior
    mode — existed only in the JSONL.

Two fixes: `attachments.raw` (the escape hatch every other flow table already
had), and archiving the promoted-but-lossy types verbatim in `session_records`
IN ADDITION to their promoted columns.
"""
from __future__ import annotations

import json

import pytest

from claude_session_db import postgres
from claude_session_db.jsonl_records import (
    SESSION_RECORD_ALSO_ARCHIVED,
    SESSION_RECORD_TYPES,
    JSONLParser,
)


def _parse(tmp_path, records):
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records))
    return JSONLParser(tmp_path).parse_file(p)


# Verbatim shapes from the corpus.
BRIDGE = {"type": "bridge-session", "sessionId": "s1",
          "bridgeSessionId": "b-123", "lastSequenceNum": 42,
          "ownerAccountUuid": "acc-1", "ownerOrganizationUuid": "org-1",
          "noHistoryBackfill": True}
LAST_PROMPT = {"type": "last-prompt", "sessionId": "s1",
               "lastPrompt": "do the thing", "leafUuid": "leaf-1",
               "explicit": True}
QUEUE_OP = {"type": "queue-operation", "sessionId": "s1",
            "operation": "remove", "content": "queued text",
            "reason": "user_cancelled",
            "timestamp": "2026-09-01T00:00:00.000Z"}
ATTACHMENT = {"type": "attachment", "uuid": "att-1", "sessionId": "s1",
              "parentUuid": None, "timestamp": "2026-09-01T00:00:00.000Z",
              "cwd": "/Users/a/x", "gitBranch": "main", "version": "2.1.258",
              "userType": "external", "sessionKind": "bg",
              "attachment": {"type": "deferred_tools_delta", "tools": ["A"]}}


# --- the seven latest-wins types + queue-operation --------------------------

def test_the_also_archived_set_is_the_documented_one():
    assert SESSION_RECORD_ALSO_ARCHIVED == {
        "ai-title", "custom-title", "last-prompt", "permission-mode", "mode",
        "bridge-session", "agent-name", "queue-operation"}


def test_it_does_not_overlap_the_routed_set():
    """`SESSION_RECORD_TYPES` are types with NO destination; these have one."""
    assert not (SESSION_RECORD_ALSO_ARCHIVED & SESSION_RECORD_TYPES)


def test_the_seven_match_the_session_meta_value_fields():
    """One list of metadata types, not two that can drift."""
    from claude_session_db.jsonl_records import SessionMetaRecord
    assert set(SessionMetaRecord._VALUE_FIELDS) == (
        SESSION_RECORD_ALSO_ARCHIVED - {"queue-operation"})


@pytest.mark.parametrize("rec", [BRIDGE, LAST_PROMPT, QUEUE_OP])
def test_the_whole_record_reaches_session_records(tmp_path, rec):
    recs = _parse(tmp_path, [rec])
    assert len(recs["session_record"]) == 1
    assert recs["session_record"][0].raw == rec


def test_the_promoted_destination_is_unchanged(tmp_path):
    """ADDITION, never replacement: the session_meta / queue_operation records
    still arrive exactly as before."""
    recs = _parse(tmp_path, [BRIDGE, LAST_PROMPT, QUEUE_OP])
    assert [m.kind for m in recs["session_meta"]] == ["bridge-session", "last-prompt"]
    assert recs["session_meta"][0].value == "b-123"
    assert recs["session_meta"][1].value == "do the thing"
    assert len(recs["queue_operation"]) == 1
    assert recs["queue_operation"][0].operation == "remove"


def test_the_dropped_fields_are_now_recoverable(tmp_path):
    """The point of the exercise, field by field."""
    recs = _parse(tmp_path, [BRIDGE, LAST_PROMPT, QUEUE_OP])
    by_kind = {r.kind: r.raw for r in recs["session_record"]}
    assert by_kind["bridge-session"]["lastSequenceNum"] == 42
    assert by_kind["bridge-session"]["ownerAccountUuid"] == "acc-1"
    assert by_kind["bridge-session"]["ownerOrganizationUuid"] == "org-1"
    assert by_kind["bridge-session"]["noHistoryBackfill"] is True
    assert by_kind["last-prompt"]["explicit"] is True
    assert by_kind["queue-operation"]["reason"] == "user_cancelled"


def test_per_record_history_is_retained(tmp_path):
    """Latest-wins keeps ONE title on `sessions`; every earlier one is now a row."""
    recs = _parse(tmp_path, [
        {"type": "ai-title", "sessionId": "s1", "aiTitle": "first guess"},
        {"type": "ai-title", "sessionId": "s1", "aiTitle": "second guess"},
        {"type": "ai-title", "sessionId": "s1", "aiTitle": "final"},
    ])
    assert [r.raw["aiTitle"] for r in recs["session_record"]] == [
        "first guess", "second guess", "final"]
    assert [r.line_num for r in recs["session_record"]] == [1, 2, 3]


def test_they_are_modelled_and_never_trip_the_unmodelled_wire(tmp_path):
    """They have a destination; the census must not report them as unmodelled."""
    recs = _parse(tmp_path, [BRIDGE, LAST_PROMPT, QUEUE_OP,
                             {"type": "mode", "sessionId": "s1", "mode": "plan"}])
    assert recs["unknown"] == []
    assert all(r.modelled is True for r in recs["session_record"])


def test_exactly_one_session_record_per_line(tmp_path):
    """(source_file, source_line) is the PK — two rows for one line is a
    constraint violation, not a nicety."""
    recs = _parse(tmp_path, [BRIDGE, {"type": "atis-latch", "atis": "x",
                                      "sessionId": "s1"},
                             {"type": "brand-new", "sessionId": "s1"}])
    lines = [r.line_num for r in recs["session_record"]]
    assert lines == sorted(lines)
    assert len(lines) == len(set(lines))


def test_a_type_with_a_lossless_destination_is_not_double_written(tmp_path):
    """`user` / `assistant` / `system` / `pr-link` keep the whole record
    already; archiving them again would double the archive for nothing."""
    recs = _parse(tmp_path, [
        {"type": "pr-link", "sessionId": "s1", "prNumber": 1, "prUrl": "u",
         "timestamp": "2026-09-01T00:00:00.000Z"},
        {"type": "summary", "summary": "x", "leafUuid": "l"},
    ])
    assert recs["session_record"] == []


# --- attachments.raw -------------------------------------------------------

def test_attachment_record_keeps_the_whole_record(tmp_path):
    att = _parse(tmp_path, [ATTACHMENT])["attachment"][0]
    assert att.raw == ATTACHMENT
    assert att.raw["sessionKind"] == "bg"      # never had a column at all


def test_schema_declares_a_guarded_additive_raw_column():
    sql = postgres.SCHEMA_SQL
    assert "ALTER TABLE attachments ADD COLUMN raw JSONB" in sql
    assert "table_name = 'attachments'" in sql, "the ALTER must be catalog-guarded"
    assert "DROP COLUMN" not in sql


def test_raw_is_bound_and_wrapped_as_jsonb():
    import inspect
    src = inspect.getsource(postgres.SessionArchive.insert_attachments)
    assert '"raw"' in src
    assert '{"attachment", "raw"}' in src, "a raw dict on a JSONB column is an error"


def test_sync_writes_the_attachment_raw():
    import inspect
    from claude_session_db import sync
    src = inspect.getsource(sync.SessionSync._insert_records)
    assert '"raw": att.raw,' in src


def test_no_backfill_is_claimed_for_attachments_raw():
    """The data was dropped at ingest, so there is nothing in the archive to
    recover it from — only a re-sync fills it. A backfill that pretended
    otherwise would be a lie."""
    assert not any("attachment" in b["key"] for b in postgres.BACKFILLS)
    assert "NOT backfillable" in postgres.SCHEMA_SQL
