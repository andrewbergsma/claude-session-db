"""Repeatable (delta) session summarization — Stage 0 + Stage 1.

Semantics under test:
  - reconcile.fetch_kmcp_session_map: the collision guard DISCRIMINATES. A
    session_id claimed twice by the same app + same project is a repeat pass
    (keep the id, canonical = latest entry); cross-app / cross-project reuse
    still drops the id to the natural-key fallback.
  - reconcile.reconcile: an existing watermark is NEVER nulled when the kmcp
    entry stops resolving (otherwise the next pass re-summarizes everything).
  - reconcile.mark_summarized stamps the TRUE message tail, not the
    last-user-prompt leaf.
  - summarize._delta_gate: full scope with no prior capture and — critically —
    with no resolvable watermark; delta only when the tail is `real` and clears
    the record floor; pass ceiling; --no-delta.
  - session_digest.render(since=): the header quotes the DELTA span (not the
    file's), compaction payloads collapse to a one-line marker, and the window
    boundary honours WATERMARK_SLACK_S (shared with classify_delta).
  - summarize.summarize_one: a continuation entry is dated to the delta window
    END, spans the window, is tagged delta-capture, links the prior pass, and
    files a see_also back to it.

Run:  uv run --extra dev pytest tests/test_summary_delta.py -q
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from claude_session_db import postgres, reconcile
from claude_session_db import session_digest as sd
from claude_session_db import summarize as ph4

WM = datetime(2026, 8, 1, 10, 0, 0, tzinfo=timezone.utc)


def _ts(offset_s: int) -> str:
    return (WM + timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ---------------------------------------------------------------------------
# Fake psycopg surfaces (no DB in unit tests)
# ---------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, rows, sink):
        self._rows, self._sink = rows, sink
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        return self

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def executemany(self, sql, params):
        self._sink.extend(params)
        self.rowcount = len(params)


class FakeConn:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.upserts = []

    def cursor(self, row_factory=None):
        return FakeCursor(self.rows, self.upserts)

    def commit(self):
        pass

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _kmcp_rows(monkeypatch, rows):
    """Make reconcile.fetch_kmcp_session_map read `rows` instead of a DB."""
    monkeypatch.setattr(reconcile.psycopg, "connect",
                        lambda *a, **kw: FakeConn(rows))


def _entry(app, path, sid, project, created):
    return {"application": app, "path": path, "session_id": sid,
            "project_path": project, "started_at": "2026-08-01T10:00:00Z",
            "created_at": created}


# ---------------------------------------------------------------------------
# Stage 0.1 — collision-guard discrimination
# ---------------------------------------------------------------------------

def test_repeat_passes_keep_the_id_and_resolve_to_the_latest(monkeypatch):
    """Same app + same project = the delta-capture case, not id reuse."""
    rows = [
        _entry("claudecode", "session/2026-08-01/first", "SID-A", "/w/proj", 1),
        _entry("claudecode", "session/2026-08-14/second", "sid-a", "/w/proj/", 2),
    ]
    _kmcp_rows(monkeypatch, rows)
    seen, collisions, _ = reconcile.fetch_kmcp_session_map("dsn")

    # kept, and pointing at the NEWEST pass (paths normalize case-insensitively
    # on the id; the trailing slash on project_path must not split the group).
    assert seen["sid-a"] == ("claudecode", "session/2026-08-14/second")
    assert [c.resolved for c in collisions] == [True]
    assert collisions[0].kind == "repeat passes"


def test_cross_app_and_cross_project_reuse_still_pops(monkeypatch):
    rows = [
        # same project, different app => a copy; the bare-id pick is arbitrary.
        _entry("claudecode", "session/a", "sid-x", "/w/proj", 1),
        _entry("orchestration", "session/a", "sid-x", "/w/proj", 2),
        # same app, different project => genuinely different sessions.
        _entry("claudecode", "session/b", "sid-y", "/w/one", 3),
        _entry("claudecode", "session/c", "sid-y", "/w/two", 4),
        # no project path to compare => cannot discriminate, so don't.
        _entry("claudecode", "session/d", "sid-z", None, 5),
        _entry("claudecode", "session/e", "sid-z", None, 6),
    ]
    _kmcp_rows(monkeypatch, rows)
    seen, collisions, _ = reconcile.fetch_kmcp_session_map("dsn")

    assert "sid-x" not in seen and "sid-y" not in seen and "sid-z" not in seen
    assert all(not c.resolved for c in collisions)
    assert {c.session_id for c in collisions} == {"sid-x", "sid-y", "sid-z"}


def test_uncontested_id_maps_straight_through(monkeypatch):
    _kmcp_rows(monkeypatch, [_entry("claudecode", "session/solo", "sid-1",
                                    "/w/proj", 1)])
    seen, collisions, natkeys = reconcile.fetch_kmcp_session_map("dsn")
    assert seen == {"sid-1": ("claudecode", "session/solo")}
    assert collisions == []
    assert natkeys[("/w/proj", "2026-08-01T10:00")] == ("claudecode", "session/solo")


# ---------------------------------------------------------------------------
# Stage 0.2 — never NULL an existing watermark
# ---------------------------------------------------------------------------

def _archive_row(**kw):
    row = {"session_id": "s1", "first_prompt": "please refactor the ingest path",
           "message_count": 120, "user_prompt_count": 9, "tool_use_count": 40,
           "last_prompt_leaf_uuid": "leaf-9", "cwd": "/w/proj",
           "created_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
           "prev_state": None, "prev_watermark": None, "prev_leaf": None,
           "prev_app": None, "prev_path": None}
    row.update(kw)
    return row


def _reconcile_upserts(monkeypatch, rows):
    monkeypatch.setattr(reconcile, "fetch_kmcp_session_map",
                        lambda dsn: ({}, [], {}))
    conn = FakeConn(rows)
    reconcile.reconcile(conn, "kmcp-dsn")
    return {u["session_id"]: u for u in conn.upserts}


def test_watermark_carries_forward_when_the_kmcp_entry_stops_resolving(monkeypatch):
    got = _reconcile_upserts(monkeypatch, [
        _archive_row(prev_state="summarized", prev_watermark=80, prev_leaf="m-80"),
    ])["s1"]
    assert got["state"] == "pending"          # no kmcp hit -> back in the queue
    assert got["message_count_at_summary"] == 80   # ...but the prefix is remembered
    assert got["leaf_uuid_at_summary"] == "m-80"
    # The ledger no longer backs a location, so it is NOT claimed.
    assert got["kmcp_application"] is None and got["kmcp_path"] is None


def test_never_summarized_session_still_has_no_watermark(monkeypatch):
    got = _reconcile_upserts(monkeypatch, [_archive_row(session_id="s1")])["s1"]
    assert got["state"] == "pending"
    assert got["message_count_at_summary"] is None
    assert got["leaf_uuid_at_summary"] is None


def test_carry_forward_also_applies_to_not_required_rows(monkeypatch):
    got = _reconcile_upserts(monkeypatch, [
        _archive_row(session_id="s1", message_count=1, user_prompt_count=0,
                     prev_watermark=1, prev_leaf="m-1"),
    ])["s1"]
    assert (got["state"], got["reason"]) == ("not_required", "empty")
    assert got["message_count_at_summary"] == 1


# ---------------------------------------------------------------------------
# Stage 1.8 — mark_summarized stamps the true tail
# ---------------------------------------------------------------------------

def test_mark_summarized_stamps_the_last_message_not_the_last_prompt():
    import inspect
    src = inspect.getsource(reconcile.mark_summarized)
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.strip().startswith("--"))   # drop SQL comments
    assert "s.last_prompt_leaf_uuid" not in code
    assert "ORDER BY m.ts DESC LIMIT 1" in src
    assert "m.ts IS NOT NULL" in src   # NULLS FIRST would win a DESC sort


# ---------------------------------------------------------------------------
# Stage 1.3 — summary_passes schema
# ---------------------------------------------------------------------------

def test_summary_passes_ddl_is_additive_and_versioned():
    assert postgres.SCHEMA_VERSION >= 8
    ddl = postgres.SUMMARY_PASSES_DDL
    assert "CREATE TABLE IF NOT EXISTS summary_passes" in ddl
    assert "PRIMARY KEY (session_id, pass)" in ddl
    assert "in_flight" in ddl
    assert ddl in postgres.SCHEMA_SQL          # runs from initialize() too
    assert "DROP TABLE" not in ddl


# ---------------------------------------------------------------------------
# Stage 1.5 — the delta gate
# ---------------------------------------------------------------------------

def _tail(n_records: int, real: bool = True):
    """Post-watermark records: `real` adds a substantive user prompt."""
    recs = [{"type": "user", "timestamp": _ts(-600),
             "message": {"content": "the original prompt, long before the watermark"}}]
    if real:
        recs.append({"type": "user", "timestamp": _ts(30),
                     "message": {"content": "now wire the delta gate into the "
                                            "phase-4 pipeline and prove it"}})
    else:
        recs.append({"type": "user", "timestamp": _ts(30),
                     "message": {"content": "yes"}})
    for i in range(n_records - 1):
        recs.append({"type": "assistant", "timestamp": _ts(60 + i),
                     "message": {"content": [{"type": "text", "text": "ok"}]}})
    return recs


def _gate_env(monkeypatch, recs, watermark=WM, source="count"):
    monkeypatch.setattr(ph4, "_watermark_for", lambda sid, dsn, kmcp: (watermark, source))
    monkeypatch.setattr(ph4, "resolve_transcript", lambda sid, fp=None: "/tmp/x.jsonl")
    monkeypatch.setattr(ph4, "load_jsonl", lambda p: recs)


def _row(**kw):
    row = {"session_id": "s1", "file_path": "/tmp/x.jsonl", "reason": "grown",
           "prev_wm": 80, "prev_leaf": "m-80", "prev_app": "claudecode",
           "prev_path": "session/2026-08-01/first", "prev_pass": 1,
           "message_count": 500}
    row.update(kw)
    return row


def test_gate_full_scope_when_nothing_was_ever_captured(monkeypatch):
    _gate_env(monkeypatch, _tail(30))
    gate = ph4._delta_gate(_row(reason=None, prev_wm=None, prev_app=None,
                                prev_path=None, prev_pass=0), "dsn", "kdsn")
    assert (gate.mode, gate.pass_no, gate.skip) == ("full", 1, None)


def test_gate_never_claims_delta_without_a_resolvable_watermark(monkeypatch):
    """The failure this gate exists to prevent: full scope rendered as a tail."""
    _gate_env(monkeypatch, _tail(30), watermark=None, source="none")
    gate = ph4._delta_gate(_row(), "dsn", "kdsn")
    assert gate.mode == "full" and gate.is_delta is False and gate.skip is None
    # The ENTRY is standalone (summarize_one keys continuation off is_delta),
    # but the LEDGER pass still advances so pass 1's row is never overwritten.
    assert gate.pass_no == 2


def test_gate_opens_a_delta_window_for_a_real_grown_tail(monkeypatch):
    _gate_env(monkeypatch, _tail(30))
    gate = ph4._delta_gate(_row(), "dsn", "kdsn")
    assert gate.is_delta and gate.skip is None
    assert gate.pass_no == 2
    assert gate.report.klass == "real"
    assert gate.report.records >= 20
    assert gate.prev_ref == "claudecode:session/2026-08-01/first"
    # window end = the last post-watermark record, not the file's last record
    assert gate.end_ts == datetime.fromisoformat(_ts(60 + 28).replace("Z", "+00:00"))


def test_gate_skips_a_confirmation_only_tail(monkeypatch):
    _gate_env(monkeypatch, _tail(3, real=False))
    gate = ph4._delta_gate(_row(), "dsn", "kdsn")
    assert gate.skip and "not substantive" in gate.skip


def test_gate_skips_a_real_but_tiny_tail(monkeypatch):
    _gate_env(monkeypatch, _tail(4))
    gate = ph4._delta_gate(_row(), "dsn", "kdsn")
    assert gate.report.klass == "real"
    assert gate.skip and "below floor" in gate.skip


def test_gate_floor_counts_transcript_records_not_the_rollup_column(monkeypatch):
    """sessions.message_count rolls subagent children up — a busy sidechain must
    not buy a pass on its own."""
    _gate_env(monkeypatch, _tail(4))
    gate = ph4._delta_gate(_row(message_count=99999), "dsn", "kdsn")
    assert gate.skip and "below floor" in gate.skip


def test_gate_honours_the_pass_ceiling(monkeypatch):
    _gate_env(monkeypatch, _tail(50))
    gate = ph4._delta_gate(_row(prev_pass=ph4.MAX_PASSES), "dsn", "kdsn")
    assert gate.skip and "ceiling" in gate.skip


def test_gate_disabled_forces_full_scope(monkeypatch):
    _gate_env(monkeypatch, _tail(50))
    gate = ph4._delta_gate(_row(), "dsn", "kdsn", enabled=False)
    assert gate.mode == "full" and gate.skip is None


def test_gate_skips_when_the_transcript_is_gone(monkeypatch):
    _gate_env(monkeypatch, _tail(50))
    monkeypatch.setattr(ph4, "resolve_transcript", lambda sid, fp=None: None)
    gate = ph4._delta_gate(_row(), "dsn", "kdsn")
    assert gate.skip and "transcript not found" in gate.skip


def test_gate_degrades_to_full_when_watermark_resolution_raises(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("archive unreachable")
    _gate_env(monkeypatch, _tail(50))
    monkeypatch.setattr(ph4, "_watermark_for", boom)
    gate = ph4._delta_gate(_row(), "dsn", "kdsn")
    assert gate.mode == "full" and gate.skip is None


# ---------------------------------------------------------------------------
# Stage 1.9 — render(since=)
# ---------------------------------------------------------------------------

@pytest.fixture
def transcript(tmp_path):
    recs = [
        {"type": "user", "timestamp": _ts(-3600),
         "message": {"content": "the ORIGINAL task from long before the summary"}},
        {"type": "assistant", "timestamp": _ts(-3500),
         "message": {"content": [{"type": "text", "text": "old work"}]}},
        # exactly at the slack boundary -> still "captured", must NOT render
        {"type": "user", "timestamp": _ts(sd.WATERMARK_SLACK_S),
         "message": {"content": "boundary record, inside the slack"}},
        {"type": "user", "timestamp": _ts(30),
         "message": {"content": "NEW TAIL WORK starts here"}},
        {"type": "user", "timestamp": _ts(40), "isCompactSummary": True,
         "message": {"content": "This session is being continued from a previous "
                                "conversation. " + "RESTATED HISTORY " * 200}},
        {"type": "system", "subtype": "compact_boundary", "timestamp": _ts(41),
         "compactMetadata": {"trigger": "manual", "preTokens": 199236,
                             "postTokens": 9260}},
        {"type": "assistant", "timestamp": _ts(50),
         "message": {"content": [{"type": "text", "text": "tail narration"}]}},
    ]
    p = tmp_path / "sess.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    return p


def test_delta_header_quotes_the_window_not_the_whole_file(transcript):
    out = sd.render(transcript, since=WM)
    header = out.splitlines()[1]
    assert header.startswith("delta span:")
    assert _ts(30) in header            # first record IN the window
    assert _ts(-3600) not in header     # the file's own first record
    assert "(4 of 7 records)" in header
    assert "the ORIGINAL task" not in out


def test_full_digest_header_is_unchanged(transcript):
    out = sd.render(transcript)
    assert out.splitlines()[1] == f"span: {_ts(-3600)} -> {_ts(50)}   (7 records)"
    assert "delta span:" not in out


def test_slack_boundary_is_shared_with_the_delta_classifier(transcript):
    out = sd.render(transcript, since=WM)
    assert "boundary record, inside the slack" not in out
    assert "NEW TAIL WORK starts here" in out
    # same boundary the classifier uses
    recs = sd.load(transcript)
    from claude_session_db.session_mgmt import classify_delta
    assert classify_delta(recs, WM).records == 4


def test_compaction_payloads_collapse_to_one_line(transcript):
    out = sd.render(transcript, since=WM)
    assert "RESTATED HISTORY" not in out
    assert "⋯ conversation compacted here (auto, size not recorded) ⋯" in out
    assert "⋯ conversation compacted here (manual, 199K→9K tokens) ⋯" in out


def test_empty_window_says_so(transcript):
    out = sd.render(transcript, since=WM + timedelta(days=1))
    assert "(no records after the watermark)" in out
    assert "(0 of 7 records)" in out


# ---------------------------------------------------------------------------
# Stage 1.6 — a continuation entry
# ---------------------------------------------------------------------------

@pytest.fixture
def kmcp(monkeypatch):
    """Records every kmcp_call; answers just enough to reach the write."""
    calls = []

    def fake(tool, args, dsn, timeout_s=None):
        calls.append((tool, args))
        if tool == "get_application":
            return {"name": args["name"]}          # every app exists
        if tool == "get_entry":
            if args.get("sections"):               # read-back verify
                return {"content": {"session_id": "s1"}}
            return {"error": "Not found"}          # path is free
        if tool == "create_entry":
            return {"path": args["path"]}
        return {}

    monkeypatch.setattr(ph4, "kmcp_call", fake)
    monkeypatch.setattr(ph4, "call_ollama", lambda *a, **kw: {
        "title": "Delta gate wired in", "topic_slug": "delta-gate-wired",
        "description": "wired the gate", "summary": "Wired the delta gate.",
        "tools_used": [], "errors_encountered": [], "follow_up": [],
        "_usage": {}})
    return calls


def _sum_row(tmp_path):
    p = tmp_path / "s1.jsonl"
    p.write_text(json.dumps({"type": "user", "timestamp": _ts(30),
                             "message": {"content": "tail"}}) + "\n")
    return {"session_id": "s1", "file_path": str(p), "cwd": "/w/proj",
            "project_path": "/w/proj", "created_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
            "modified_at": datetime(2026, 8, 20, tzinfo=timezone.utc),
            "tool_use_count": 3, "total_output_tokens": 10,
            "duration_seconds": 60, "total_input_tokens": 100}


def _created(calls):
    return next(a for t, a in calls if t == "create_entry")


def test_continuation_entry_is_dated_to_the_delta_window_end(kmcp, tmp_path):
    end = datetime(2026, 8, 15, 9, 30, tzinfo=timezone.utc)
    gate = ph4.DeltaGate(mode="delta", pass_no=2, watermark=WM, source="count",
                         end_ts=end, prev_ref="claudecode:session/2026-08-01/first")
    app, path = ph4.summarize_one(_sum_row(tmp_path), "kdsn", "m", "http://x",
                                  4096, {}, lambda _m: None, gate)
    # NOT 2026-07-01 (the session's birth) and NOT 2026-08-20 (its mtime).
    assert path == "session/2026-08-15/delta-gate-wired"

    content = _created(kmcp)["content"]
    assert content["started_at"] == "2026-08-01T10:00:00Z"   # window open
    assert content["ended_at"] == "2026-08-15T09:30:00Z"     # window close
    assert content["linked_entries"] == ["claudecode:session/2026-08-01/first"]
    assert set(_created(kmcp)["tags"]) == {ph4.AUTO_TAG, ph4.DELTA_TAG}
    assert _created(kmcp)["title"].startswith("Session (cont. 2):")

    rel = next(a for t, a in kmcp if t == "create_relationship")
    assert rel["relationship_type"] == "see_also"
    assert rel["source_path"] == path
    assert rel["target_path"] == "session/2026-08-01/first"


def test_full_pass_keeps_the_session_span_and_no_delta_tag(kmcp, tmp_path):
    app, path = ph4.summarize_one(_sum_row(tmp_path), "kdsn", "m", "http://x",
                                  4096, {}, lambda _m: None,
                                  ph4.DeltaGate(mode="full", pass_no=1))
    assert path == "session/2026-07-01/delta-gate-wired"
    content = _created(kmcp)["content"]
    assert content["started_at"] == "2026-07-01T00:00:00Z"
    assert content["ended_at"] == "2026-08-20T00:00:00Z"
    assert "linked_entries" not in content
    assert _created(kmcp)["tags"] == [ph4.AUTO_TAG]
    assert not any(t == "create_relationship" for t, _ in kmcp)


def test_a_failed_see_also_does_not_fail_a_written_pass(monkeypatch, kmcp, tmp_path):
    real = ph4.kmcp_call

    def fake(tool, args, dsn, timeout_s=None):
        if tool == "create_relationship":
            raise ph4.KmcpError("knowledge-cli timed out")
        return real(tool, args, dsn, timeout_s)

    monkeypatch.setattr(ph4, "kmcp_call", fake)
    gate = ph4.DeltaGate(mode="delta", pass_no=2, watermark=WM, source="count",
                         end_ts=datetime(2026, 8, 15, tzinfo=timezone.utc),
                         prev_ref="claudecode:session/2026-08-01/first")
    app, path = ph4.summarize_one(_sum_row(tmp_path), "kdsn", "m", "http://x",
                                  4096, {}, lambda _m: None, gate)
    assert path.startswith("session/2026-08-15/")   # the entry is the payload


def test_delta_prompt_tells_the_model_not_to_restate(monkeypatch, kmcp, tmp_path):
    seen = {}
    monkeypatch.setattr(ph4, "call_ollama",
                        lambda prompt, *a, **kw: seen.setdefault("p", prompt) and None
                        or {"title": "t", "topic_slug": "s", "description": "d",
                            "summary": "sum", "tools_used": [],
                            "errors_encountered": [], "follow_up": [], "_usage": {}})
    gate = ph4.DeltaGate(mode="delta", pass_no=2, watermark=WM, source="count",
                         end_ts=datetime(2026, 8, 15, tzinfo=timezone.utc))
    ph4.summarize_one(_sum_row(tmp_path), "kdsn", "m", "http://x", 4096, {},
                      lambda _m: None, gate)
    assert "CONTINUATION SUMMARY" in seen["p"]
    assert "do NOT restate" in seen["p"].replace("Do NOT restate", "do NOT restate")
    assert "2026-08-01T10:00:00Z" in seen["p"]


# ---------------------------------------------------------------------------
# Stage 1.4 / 1.7 — queue plumbing
# ---------------------------------------------------------------------------

def test_pick_sql_joins_prior_capture_without_widening_the_view():
    sql = ph4._PICK_SQL
    for col in ("prev_wm", "prev_leaf", "prev_app", "prev_path", "prev_pass"):
        assert f"AS {col}" in sql or f"{col}" in sql
    assert "LEFT JOIN summary_state ss USING (session_id)" in sql
    assert "summary_passes" in sql


def test_advisory_lock_key_is_stable_and_id_specific():
    a = ph4._lock_key("e87f66d8-1111-2222-3333-444444444444")
    b = ph4._lock_key("e87f66d8-9999-8888-7777-666666666666")
    assert a == ph4._lock_key("e87f66d8-1111-2222-3333-444444444444")
    assert a != b                      # a shared short-id prefix is not a shared lock
    assert 0 < a < 2 ** 63
