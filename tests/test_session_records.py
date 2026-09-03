"""`session_records` — the catch-all that ends silent record-type loss.

Before schema v9, a session-scoped record type with no dedicated table was
collected into `records["unknown"]` and then dropped: the archive claims to be
LOSSLESS, and for eleven record types added by Claude Code v2.1.161-258 it
simply was not. This file pins the routing, the payload fidelity, the natural
key, and the modelled/unmodelled split that keeps the tripwire from crying wolf
about types csd already handles.

Payload shapes below are verbatim from a 30-day scan of ~/.claude/projects.
"""
from __future__ import annotations

import json
from datetime import timezone

import pytest

from claude_session_db.jsonl_records import (
    SESSION_RECORD_TYPES,
    JSONLParser,
    SessionRecord,
)
from claude_session_db import postgres


# Real payloads, copied out of the corpus.
SAMPLES = {
    "atis-latch": {
        "type": "atis-latch", "atis": "905262160b2cf328",
        "sessionId": "15e98ef9-4e7e-42a7-81b8-d07feacb9ca1"},
    "worktree-state": {
        "type": "worktree-state",
        "worktreeSession": {
            "originalCwd": "/Users/andrew/GitHub/infrastructure",
            "preEnterOriginalCwd": "/Users/andrew/GitHub/infrastructure",
            "worktreePath": "/Users/andrew/GitHub/infrastructure/.claude/worktrees/net-v1",
            "worktreeName": "net-v1", "worktreeBranch": "worktree-net-v1",
            "originalBranch": "main",
            "originalHeadCommit": "c5f866d586a76a4586ead2a83611def070bdf57d",
            "sessionId": "4b375bd6-50db-4bd8-ada5-055e9a6d6d05"},
        "sessionId": "4b375bd6-50db-4bd8-ada5-055e9a6d6d05"},
    "relocated": {
        "type": "relocated", "sessionId": "4b375bd6-50db-4bd8-ada5-055e9a6d6d05",
        "relocatedCwd": "/Users/andrew/GitHub/infrastructure/.claude/worktrees/net-v1"},
    "file-history-delta": {
        "type": "file-history-delta",
        "messageId": "ab133476-2742-4974-a826-ffd3063d213a",
        "snapshotMessageId": "89a7b8d1-a2f4-405c-9f18-079dfa60c3ae",
        "trackingPath": "/private/tmp/claude-501/x/scratchpad/writeback.py",
        "backup": {"backupFileName": None, "version": 1,
                   "backupTime": "2026-08-14T00:27:49.152Z",
                   "realParentDir": "/private/tmp/claude-501/x/scratchpad"},
        "timestamp": "2026-08-14T00:27:49.152Z"},
    "history-suppression": {
        "type": "history-suppression",
        "sessionId": "03f59014-4978-45e1-b279-c5257b7630b7",
        "cause": "restored_owner_mismatch",
        "vetoedAgainstAccountUuid": "69e03fd1-e25a-4bac-a98b-0619c4a30fef",
        "ts": "2026-08-22T14:25:20.020Z"},
    "frame-link": {
        "type": "frame-link", "sessionId": "218b1f28-3871-4bb6-a628-b740a1efc432",
        "path": "/private/tmp/claude-501/x/scratchpad/vopak-timeline.html",
        "frameUrl": "https://claude.ai/code/artifact/3aaf44b5-9bda",
        "title": "Vopak Barge Dock Topsides", "artifactCount": 1,
        "timestamp": "2026-08-10T22:37:43.269Z"},
    "cost-state": {
        "type": "cost-state", "sessionId": "2e9ce14d-5acc-44e0-8ccf-3fb054d0261b",
        "totalCostUSD": 29.018776200000012, "totalAPIDuration": 2155095,
        "totalAPIDurationWithoutRetries": 2154013, "totalToolDuration": 374575,
        "totalLinesAdded": 193, "totalLinesRemoved": 0, "totalDuration": 441459675,
        "startTime": 1787832755466, "hasUnknownModelCost": False,
        "modelUsage": {"claude-opus-5": {"inputTokens": 23153, "outputTokens": 47484,
                                         "cacheReadInputTokens": 5800024,
                                         "cacheCreationInputTokens": 203724,
                                         "webSearchRequests": 0,
                                         "costUSD": 5.476152000000001}}},
    "artifact-autoreact-ledger": {
        "type": "artifact-autoreact-ledger", "v": 1,
        "sessionId": "e4d72eeb-00ef-469e-8bf7-97919674baad",
        "accountUuid": "29a8fc5d-bc84-4812-81d6-7828e75afe08",
        "artifacts": {"b8b04d84": {"savedAt": 1788310104632, "everBaselined": True}}},
    "artifact-comment-monitor": {
        "type": "artifact-comment-monitor", "v": 1,
        "sessionId": "49a6e646-6c03-4a19-a5a1-dbda1d3f31a5",
        "artifacts": {"f9a017ef": {"state": "armed", "writtenAtMs": 1787289268297,
                                   "title": "Ardent & Sterling Build-Out"}}},
    "fork-context-ref": {
        "type": "fork-context-ref", "agentId": "a2087dee8d1e9e045",
        "parentSessionId": "999be60f-31a1-4776-aa35-2f1477ab0a22",
        "parentLastUuid": "de53d33c-9e32-489c-b82c-95372dbacf51",
        "contextLength": 351},
}


def _parse(tmp_path, records):
    p = tmp_path / "sess.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records))
    return JSONLParser(tmp_path).parse_file(p)


def test_all_ten_types_are_declared():
    assert SESSION_RECORD_TYPES == set(SAMPLES), "SAMPLES and the routed set drifted"
    assert len(SESSION_RECORD_TYPES) == 10


@pytest.mark.parametrize("kind", sorted(SAMPLES))
def test_each_type_routes_to_session_records(tmp_path, kind):
    recs = _parse(tmp_path, [SAMPLES[kind]])
    assert len(recs["session_record"]) == 1, f"{kind} was not routed"
    assert recs["unknown"] == [], f"{kind} is modelled and must not trip the wire"
    r = recs["session_record"][0]
    assert r.kind == kind
    assert r.modelled is True
    assert r.line_num == 1


@pytest.mark.parametrize("kind", sorted(SAMPLES))
def test_payload_is_kept_verbatim(tmp_path, kind):
    """The whole point: nothing is normalized away on the route in."""
    recs = _parse(tmp_path, [SAMPLES[kind]])
    assert recs["session_record"][0].raw == SAMPLES[kind]


def test_session_id_is_extracted_where_present(tmp_path):
    recs = _parse(tmp_path, [SAMPLES["relocated"], SAMPLES["atis-latch"],
                             SAMPLES["cost-state"]])
    ids = [r.session_id for r in recs["session_record"]]
    assert ids == ["4b375bd6-50db-4bd8-ada5-055e9a6d6d05",
                   "15e98ef9-4e7e-42a7-81b8-d07feacb9ca1",
                   "2e9ce14d-5acc-44e0-8ccf-3fb054d0261b"]


def test_worktree_state_session_id_falls_back_to_the_nested_copy(tmp_path):
    payload = dict(SAMPLES["worktree-state"])
    payload.pop("sessionId")
    recs = _parse(tmp_path, [payload])
    assert recs["session_record"][0].session_id == "4b375bd6-50db-4bd8-ada5-055e9a6d6d05"


def test_file_history_delta_has_no_session_id(tmp_path):
    """It carries messageId, not sessionId — the row falls back to the owning
    session at sync time, and the parser must not invent one."""
    recs = _parse(tmp_path, [SAMPLES["file-history-delta"]])
    assert recs["session_record"][0].session_id == ""


@pytest.mark.parametrize("kind,expect_ts", [
    ("file-history-delta", True),    # `timestamp`
    ("frame-link", True),            # `timestamp`
    ("history-suppression", True),   # `ts`
    ("atis-latch", False),           # carries no time at all
    ("relocated", False),
    ("cost-state", False),           # startTime is the SESSION start, not this record's
])
def test_timestamp_is_taken_only_where_the_record_has_one(tmp_path, kind, expect_ts):
    r = _parse(tmp_path, [SAMPLES[kind]])["session_record"][0]
    assert (r.timestamp is not None) is expect_ts
    if expect_ts:
        assert r.timestamp.tzinfo is not None


def test_history_suppression_reads_the_ts_field(tmp_path):
    r = _parse(tmp_path, [SAMPLES["history-suppression"]])["session_record"][0]
    assert r.timestamp.astimezone(timezone.utc).isoformat() == "2026-08-22T14:25:20.020000+00:00"


def test_fork_context_ref_carries_the_agent_id(tmp_path):
    r = _parse(tmp_path, [SAMPLES["fork-context-ref"]])["session_record"][0]
    assert r.agent_id == "a2087dee8d1e9e045"


def test_a_never_seen_type_is_captured_AND_reported(tmp_path):
    """Both halves. Captured verbatim so it is not lost; reported so it does
    not stay unnoticed for another 100 releases."""
    future = {"type": "a-type-from-the-future", "sessionId": "s1", "shape": {"x": 1}}
    recs = _parse(tmp_path, [SAMPLES["relocated"], future])
    assert recs["unknown"] == [(2, "a-type-from-the-future")]
    unmodelled = [r for r in recs["session_record"] if not r.modelled]
    assert len(unmodelled) == 1
    assert unmodelled[0].raw == future
    assert unmodelled[0].line_num == 2


def test_line_numbers_are_the_natural_key(tmp_path):
    """(source_file, source_line) is the PK — the records carry no uuid, so the
    line number has to be right or a re-sync duplicates or clobbers rows."""
    recs = _parse(tmp_path, [
        {"type": "ai-title", "sessionId": "s", "aiTitle": "t"},
        SAMPLES["atis-latch"],
        {"type": "ai-title", "sessionId": "s", "aiTitle": "t2"},
        SAMPLES["relocated"],
    ])
    assert [r.line_num for r in recs["session_record"]] == [2, 4]


def test_schema_declares_the_table_and_its_key():
    sql = postgres.SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS session_records" in sql
    assert "PRIMARY KEY (source_file, source_line)" in sql
    assert "payload     JSONB NOT NULL" in sql
    assert "is_modelled" in sql


def test_table_is_cleared_per_source_file():
    """Idempotent re-sync: a file's rows go before its rows come back."""
    assert "session_records" in postgres.PER_FILE_TABLES


def test_schema_version_is_9():
    assert postgres.SCHEMA_VERSION == 9
