"""Schema-v9 `sessions` columns derived from records csd used to drop.

Four independent losses, one derivation seam (`_derive_from_session_records`):

  fork lineage       `messages.forked_from` has been dead since v2.1.212; the
                     replacement is `fork-context-ref`, and nothing read it.
  relocation         `sync._upsert_session` takes `cwd` from the FIRST
                     conversation record, so a v2.1.169 `/cd` or a worktree
                     move left the session filed under a directory it had left.
  worktree binding   `worktree-state` was dropped entirely.
  reported cost      `cost-state` carries Claude Code's own totalCostUSD and
                     per-model usage — the only external check on csd's
                     computed cost — and was dropped entirely.

Latest-wins is load-bearing for three of the four: a transcript is append-only,
so the LAST record of a kind is the current state.
"""
from __future__ import annotations

import json
import types

import pytest

from claude_session_db import postgres
from claude_session_db.jsonl_records import JSONLParser
from claude_session_db.sync import SessionSync


def _records(tmp_path, raw):
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in raw))
    return JSONLParser(tmp_path).parse_file(p)


def _derive(tmp_path, raw, ctx_last=None):
    return SessionSync._derive_from_session_records(
        _records(tmp_path, raw), ctx_last=ctx_last)


# --- fork lineage ----------------------------------------------------------

FORK = {"type": "fork-context-ref", "agentId": "a2087dee8d1e9e045",
        "parentSessionId": "999be60f-31a1-4776-aa35-2f1477ab0a22",
        "parentLastUuid": "de53d33c-9e32-489c-b82c-95372dbacf51",
        "contextLength": 351}


def test_fork_context_ref_populates_lineage(tmp_path):
    d = _derive(tmp_path, [FORK])
    assert d["forked_from_session_id"] == "999be60f-31a1-4776-aa35-2f1477ab0a22"
    assert d["forked_from_uuid"] == "de53d33c-9e32-489c-b82c-95372dbacf51"
    assert d["fork_context_length"] == 351
    assert d["fork_agent_id"] == "a2087dee8d1e9e045"


def test_no_fork_record_leaves_lineage_absent(tmp_path):
    """Absent, not zero: `upsert_session` COALESCEs, so a None never wipes a
    value another file set — but a 0 context length would be a lie."""
    d = _derive(tmp_path, [{"type": "relocated", "sessionId": "s",
                            "relocatedCwd": "/tmp"}])
    assert "forked_from_session_id" not in d
    assert "fork_context_length" not in d


def test_fork_context_length_must_be_an_int(tmp_path):
    bad = dict(FORK, contextLength="lots")
    assert _derive(tmp_path, [bad])["fork_context_length"] is None


def test_legacy_forked_from_column_survives():
    """`messages.forked_from` is legacy, NOT removed — the archive never drops
    a column, and pre-v2.1.212 sessions still carry real values in it."""
    assert "forked_from  JSONB" in postgres.SCHEMA_SQL


# --- relocation ------------------------------------------------------------

def test_relocated_sets_current_cwd(tmp_path):
    d = _derive(tmp_path, [{"type": "relocated", "sessionId": "s",
                            "relocatedCwd": "/Users/a/GitHub/x/.claude/worktrees/w"}])
    assert d["current_cwd"] == "/Users/a/GitHub/x/.claude/worktrees/w"


def test_last_relocation_wins(tmp_path):
    """A session can /cd more than once; the archive must show where it IS."""
    d = _derive(tmp_path, [
        {"type": "relocated", "sessionId": "s", "relocatedCwd": "/first"},
        {"type": "relocated", "sessionId": "s", "relocatedCwd": "/second"},
        {"type": "relocated", "sessionId": "s", "relocatedCwd": "/third"},
    ])
    assert d["current_cwd"] == "/third"


def test_current_cwd_falls_back_to_the_last_conversation_record(tmp_path):
    """Not every move emits `relocated`; the last record's cwd is the backstop."""
    last = types.SimpleNamespace(cwd="/Users/a/GitHub/x/worktree")
    d = _derive(tmp_path, [{"type": "atis-latch", "atis": "z", "sessionId": "s"}],
                ctx_last=last)
    assert d["current_cwd"] == "/Users/a/GitHub/x/worktree"


def test_relocated_beats_the_fallback(tmp_path):
    last = types.SimpleNamespace(cwd="/stale")
    d = _derive(tmp_path, [{"type": "relocated", "sessionId": "s",
                            "relocatedCwd": "/real"}], ctx_last=last)
    assert d["current_cwd"] == "/real"


def test_cwd_column_semantics_are_untouched():
    """`cwd` still means "where the session STARTED" — the repos lens and
    project attribution key off it, so redefining it would move data under
    every consumer."""
    assert '"cwd": ctx.cwd if ctx else None' in _sync_source()


def _sync_source():
    import pathlib
    from claude_session_db import sync
    return pathlib.Path(sync.__file__).read_text()


# --- worktree binding ------------------------------------------------------

WT = {"type": "worktree-state", "sessionId": "s",
      "worktreeSession": {
          "originalCwd": "/Users/a/GitHub/infra",
          "preEnterOriginalCwd": "/Users/a/GitHub/infra",
          "worktreePath": "/Users/a/GitHub/infra/.claude/worktrees/net-v1",
          "worktreeName": "net-v1", "worktreeBranch": "worktree-net-v1",
          "originalBranch": "main",
          "originalHeadCommit": "c5f866d586a76a4586ead2a83611def070bdf57d",
          "sessionId": "s"}}


def test_worktree_state_is_stored_verbatim(tmp_path):
    """JSONB, not a flattening: eight keys today, and the JSONB escape hatch is
    how this archive absorbs Claude Code field drift without a migration."""
    d = _derive(tmp_path, [WT])
    assert d["worktree_session"] == WT["worktreeSession"]
    assert d["worktree_session"]["worktreeBranch"] == "worktree-net-v1"


def test_worktree_state_without_the_nested_object_is_skipped(tmp_path):
    d = _derive(tmp_path, [{"type": "worktree-state", "sessionId": "s"}])
    assert "worktree_session" not in d


# --- reported cost ---------------------------------------------------------

COST = {"type": "cost-state", "sessionId": "s", "totalCostUSD": 29.0187762,
        "totalAPIDuration": 2155095, "totalToolDuration": 374575,
        "totalLinesAdded": 193, "totalLinesRemoved": 4,
        "totalDuration": 441459675, "hasUnknownModelCost": False,
        "modelUsage": {"claude-opus-5": {"inputTokens": 23153,
                                         "outputTokens": 47484,
                                         "costUSD": 5.476152}}}


def test_cost_state_populates_jsonb_and_scalars(tmp_path):
    d = _derive(tmp_path, [COST])
    assert d["cost_state"] == COST                    # verbatim, modelUsage included
    assert d["reported_cost_usd"] == 29.0187762
    assert d["reported_total_duration_ms"] == 441459675
    assert d["reported_api_duration_ms"] == 2155095
    assert d["reported_tool_duration_ms"] == 374575
    assert d["reported_lines_added"] == 193
    assert d["reported_lines_removed"] == 4
    assert d["has_unknown_model_cost"] is False


def test_latest_cost_state_wins(tmp_path):
    """cost-state is REWRITTEN as the session runs — an early row would report
    a fraction of the true spend."""
    early = dict(COST, totalCostUSD=1.0)
    late = dict(COST, totalCostUSD=42.5)
    d = _derive(tmp_path, [early, late])
    assert d["reported_cost_usd"] == 42.5


def test_has_unknown_model_cost_absent_is_none_not_false(tmp_path):
    """False means "Claude Code priced everything"; None means "it never said".
    Collapsing the two would make the drift view claim a clean comparison it
    cannot support."""
    d = _derive(tmp_path, [{k: v for k, v in COST.items()
                            if k != "hasUnknownModelCost"}])
    assert d["has_unknown_model_cost"] is None


# --- session kind ----------------------------------------------------------

def test_session_kind_is_read_off_any_carrying_record(tmp_path):
    recs = _records(tmp_path, [
        {"type": "user", "uuid": "u1", "sessionId": "s", "parentUuid": None,
         "timestamp": "2026-08-01T00:00:00.000Z", "sessionKind": "bg",
         "message": {"role": "user", "content": "hi"}},
    ])
    assert SessionSync._derive_session_kind(recs) == "bg"


def test_session_kind_is_none_when_absent(tmp_path):
    recs = _records(tmp_path, [
        {"type": "user", "uuid": "u1", "sessionId": "s", "parentUuid": None,
         "timestamp": "2026-08-01T00:00:00.000Z",
         "message": {"role": "user", "content": "hi"}},
    ])
    assert SessionSync._derive_session_kind(recs) is None


# --- schema / view ---------------------------------------------------------

@pytest.mark.parametrize("col", [
    "forked_from_session_id", "forked_from_uuid", "fork_context_length",
    "fork_agent_id", "current_cwd", "worktree_session", "cost_state",
    "reported_cost_usd", "reported_total_duration_ms", "reported_api_duration_ms",
    "reported_tool_duration_ms", "reported_lines_added", "reported_lines_removed",
    "has_unknown_model_cost", "session_kind",
])
def test_column_is_declared_and_bound(col):
    assert col in postgres.SCHEMA_SQL, f"{col} missing from the v9 migration"
    assert col in postgres.SessionArchive._SESSION_COLS, f"{col} never bound on upsert"


def test_v9_migration_is_guarded_and_additive():
    sql = postgres.SCHEMA_SQL
    # guarded by a catalog check so the ACCESS EXCLUSIVE ALTER fires once
    assert "column_name = 'forked_from_session_id'" in sql
    # nothing is dropped or rewritten
    assert "DROP COLUMN" not in sql
    assert "ADD COLUMN forked_from_session_id TEXT" in sql
    assert "ALTER COLUMN" not in sql.split("-- Migration (idempotent, guarded): schema v9")[1]


def test_jsonb_session_columns_are_wrapped():
    """A raw dict bound to a JSONB column is a psycopg ProgrammingError."""
    assert postgres.SessionArchive._SESSION_JSONB_COLS == {"worktree_session",
                                                           "cost_state"}


def test_cost_drift_view_exists_and_separates_the_explanations():
    v = postgres.VIEWS_SQL
    assert "CREATE OR REPLACE VIEW v_session_cost_drift" in v
    assert "computed_cost_usd" in v and "reported_cost_usd" in v
    assert "drift_usd" in v and "drift_pct" in v
    # an unpriced model is the FIRST thing to check when the two disagree
    assert "unpriced_messages" in v
    # the harness's own per-model breakdown, for attributing the gap
    assert "reported_model_usage" in v
    # the defect this view found on its first run: one API response can appear
    # as several `messages` rows, so v_message_cost sums it more than once
    assert "api_message_ratio" in v
    assert "distinct_api_messages" in v
    # the other two attributable causes
    assert "sidechain_messages" in v
    assert "fallback_messages" in v
