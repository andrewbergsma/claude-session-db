"""The Σ chip — per-row summarization presence (console/server.py).

Semantics under test:
  - summary_presence(): a PURE memo read. It never grades, never opens a
    connection, and a miss degrades to state "unknown" (pending) instead of
    blocking the nav poll.
  - the (mtime_ns, size) memo: an unchanged transcript is never re-graded;
    a transcript that moved is served stale and re-graded on the next tick.
  - _grade_summary(): maps the ONE grader's report (summary_scope_report →
    _delta_gate) onto never | captured | delta, folds the summarize_attempts
    backoff in as failed, and degrades every failure to "unknown" — no DSN, an
    unreachable archive, a raising grader.
  - the live overlay: an in-flight console run reads "running"; a recorded
    failure reads "failed", whatever the archive last said.
  - child (`<parent>:<agent>`) sids have no presence — they summarize through
    their parent.

Run:  uv run --extra dev pytest tests/test_summary_presence.py -q
"""
import json
import time
from pathlib import Path

import pytest

from claude_session_db.console import server

SID = "eeeeeeee-1111-2222-3333-666666666666"
SID2 = "eeeeeeee-1111-2222-3333-777777777777"


def _transcript(path: Path, n=1):
    recs = [{"type": "user", "uuid": f"u-{i}", "timestamp": f"2026-08-20T10:0{i}:00Z",
             "message": {"content": f"do {i}"}} for i in range(n)]
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    return path


def _report(scope="full", summarized=False, **kw):
    """A summary_scope_report() shape."""
    out = {"session": SID, "ref": SID, "mode": "auto", "scope": scope,
           "pass": 1, "summarized": summarized, "since": None, "source": "none",
           "source_label": "none", "prior": None, "records": None,
           "reason": None, "digest": f"csd digest {SID}"}
    out.update(kw)
    return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CSD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(server, "SUMMARY_FILE", tmp_path / "summary.json")
    monkeypatch.setattr(server, "SUM_STAGGER_S", 0.0)
    monkeypatch.setattr(server, "CSD_DSN", "postgresql://stub/archive")
    monkeypatch.setattr(server, "_attempt_note", lambda sid: None)
    server._SUM_MEMO.clear()
    server._SUM_WANTED.clear()
    server.SUMMARIZING.clear()
    return _transcript(tmp_path / f"{SID}.jsonl")


def _stub_report(monkeypatch, rep, counter=None):
    """Bind the ONE grader to a canned report (and count the calls)."""
    from claude_session_db import summarize as ph4

    def fake(sid, dsn, kmcp_dsn=None, mode="auto"):
        if counter is not None:
            counter.append(sid)
        return rep(sid) if callable(rep) else rep
    monkeypatch.setattr(ph4, "summary_scope_report", fake)


# ---- presence is a pure read ------------------------------------------------

def test_presence_before_any_grade_is_unknown_and_pending(env):
    p = server.summary_presence(SID, env)
    assert p["state"] == "unknown" and p["pending"] is True
    assert "not graded yet" in p["reason"]
    # …and the row registered itself as wanted for the background grader.
    assert SID in server._SUM_WANTED


def test_presence_never_grades_on_the_request_path(env, monkeypatch):
    def boom(sid):
        raise AssertionError("summary_presence must never grade")
    monkeypatch.setattr(server, "_grade_summary", boom)
    for _ in range(3):
        assert server.summary_presence(SID, env)["state"] == "unknown"


def test_child_sessions_have_no_presence(env):
    assert server.summary_presence(f"{SID}:abc123", env) is None


def test_missing_transcript_still_degrades(env, tmp_path):
    gone = tmp_path / "not-there.jsonl"
    p = server.summary_presence(SID2, gone)
    assert p["state"] == "unknown" and p["pending"] is True
    assert SID2 not in server._SUM_WANTED       # no signature, nothing to grade


# ---- the grader mapping -----------------------------------------------------

def test_grade_never_when_no_prior_capture(env, monkeypatch):
    _stub_report(monkeypatch, _report("full", summarized=False,
                                      reason="no prior summary found"))
    assert server._grade_summary(SID)["state"] == "never"


def test_grade_captured_when_tail_not_substantive(env, monkeypatch):
    _stub_report(monkeypatch, _report("none", summarized=True, **{
        "pass": 2, "since": "2026-08-20T10:00:00Z",
        "prior": "claudecode:session/2026-08-20/x",
        "reason": "delta not substantive — summarizing the FULL session again"}))
    g = server._grade_summary(SID)
    assert g["state"] == "captured" and g["pass"] == 2
    assert g["prior"] == "claudecode:session/2026-08-20/x"
    assert g["since"] == "2026-08-20T10:00:00Z"


def test_grade_delta_when_real_new_work(env, monkeypatch):
    _stub_report(monkeypatch, _report("delta", summarized=True, **{
        "pass": 3, "since": "2026-08-21T09:00:00Z", "records": 143,
        "source": "leaf"}))
    g = server._grade_summary(SID)
    assert g["state"] == "delta" and g["records"] == 143
    assert g["source"] == "leaf" and g["pass"] == 3


def test_grade_captured_but_unwatermarked_reads_as_delta(env, monkeypatch):
    """Captured with no resolvable watermark: the next pass restates the whole
    session. That is re-capture work, not a settled row."""
    _stub_report(monkeypatch, _report("full", summarized=True, **{
        "pass": 2, "reason": "no summary watermark resolvable — full scope"}))
    assert server._grade_summary(SID)["state"] == "delta"


# ---- degrade, never block ---------------------------------------------------

def test_grade_without_dsn_is_unknown(env, monkeypatch):
    monkeypatch.setattr(server, "CSD_DSN", None)
    g = server._grade_summary(SID)
    assert g["state"] == "unknown" and "no archive DSN" in g["reason"]


def test_grade_survives_a_raising_grader(env, monkeypatch):
    from claude_session_db import summarize as ph4

    def boom(*a, **k):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(ph4, "summary_scope_report", boom)
    g = server._grade_summary(SID)
    assert g["state"] == "unknown" and "connection refused" in g["reason"]


def test_graders_own_degrade_path_is_not_never(env, monkeypatch):
    """`prior capture unresolved` means the archive answered nothing — claiming
    "never summarized" off that would be a lie the chip must not tell."""
    _stub_report(monkeypatch, _report("full", summarized=False, **{
        "reason": "prior capture unresolved (OperationalError: down) — full scope"}))
    assert server._grade_summary(SID)["state"] == "unknown"


def test_refresh_never_raises_when_everything_fails(env, monkeypatch):
    monkeypatch.setattr(server, "_grade_summary",
                        lambda sid: (_ for _ in ()).throw(RuntimeError("x")))
    server.summary_presence(SID, env)
    with pytest.raises(RuntimeError):
        server._refresh_summaries()          # the tick itself propagates…
    # …and the thread body is what swallows it, so the lens never dies.
    assert server.summary_presence(SID, env)["state"] == "unknown"


# ---- the (mtime_ns, size) memo ---------------------------------------------

def test_refresh_grades_wanted_rows_then_presence_serves_them(env, monkeypatch):
    calls = []
    _stub_report(monkeypatch, _report("delta", summarized=True,
                                      **{"pass": 2, "since": "2026-08-21T09:00:00Z"}),
                 counter=calls)
    server.summary_presence(SID, env)                 # register
    assert server._refresh_summaries() == 1
    p = server.summary_presence(SID, env)
    assert p["state"] == "delta" and p["pending"] is False and p["stale"] is False
    assert len(calls) == 1


def test_unchanged_transcript_is_never_regraded(env, monkeypatch):
    calls = []
    _stub_report(monkeypatch, _report("none", summarized=True), counter=calls)
    server.summary_presence(SID, env)
    server._refresh_summaries()
    for _ in range(3):
        server.summary_presence(SID, env)
        assert server._refresh_summaries() == 0
    assert len(calls) == 1


def test_moved_transcript_is_stale_then_regraded(env, monkeypatch):
    calls = []
    _stub_report(monkeypatch, _report("none", summarized=True), counter=calls)
    server.summary_presence(SID, env)
    server._refresh_summaries()
    _transcript(env, n=4)                              # new work lands
    p = server.summary_presence(SID, env)
    assert p["stale"] is True and p["state"] == "captured"   # last known, marked
    assert server._refresh_summaries() == 1
    assert server.summary_presence(SID, env)["stale"] is False
    assert len(calls) == 2


def test_ttl_forces_a_regrade_without_a_transcript_change(env, monkeypatch):
    calls = []
    _stub_report(monkeypatch, _report("none", summarized=True), counter=calls)
    server.summary_presence(SID, env)
    server._refresh_summaries()
    server._SUM_MEMO[SID]["checked_at"] = time.time() - server.SUM_TTL_S - 1
    assert server._refresh_summaries() == 1
    assert len(calls) == 2


def test_memo_persists_and_reloads(env, monkeypatch):
    _stub_report(monkeypatch, _report("delta", summarized=True,
                                      **{"pass": 2, "since": "2026-08-21T09:00:00Z"}))
    server.summary_presence(SID, env)
    server._refresh_summaries()
    assert server.SUMMARY_FILE.exists()
    store = json.loads(server.SUMMARY_FILE.read_text())
    assert store["sessions"][SID]["state"] == "delta"
    server._SUM_MEMO.clear()
    for sid, row in (server._read_summary_store().get("sessions") or {}).items():
        server._SUM_MEMO[sid] = row
    assert server.summary_presence(SID, env)["state"] == "delta"


def test_invalidate_forces_a_regrade(env, monkeypatch):
    calls = []
    _stub_report(monkeypatch, _report("none", summarized=True), counter=calls)
    server.summary_presence(SID, env)
    server._refresh_summaries()
    server._invalidate_summary(SID)                    # a pass landed
    server.summary_presence(SID, env)
    assert server._refresh_summaries() == 1
    assert len(calls) == 2


# ---- the live overlay + the attempts backoff -------------------------------

def test_running_run_overrides_the_graded_state(env, monkeypatch):
    _stub_report(monkeypatch, _report("full", summarized=False))
    server.summary_presence(SID, env)
    server._refresh_summaries()
    assert server.summary_presence(SID, env)["state"] == "never"
    server.SUMMARIZING[SID] = "running"
    assert server.summary_presence(SID, env)["state"] == "running"
    server.SUMMARIZING[SID] = "done"                   # verdict, not a grade
    assert server.summary_presence(SID, env)["state"] == "never"
    server.SUMMARIZING[SID] = "summary failed (rc=1)"
    p = server.summary_presence(SID, env)
    assert p["state"] == "failed" and "rc=1" in p["reason"]


def test_attempts_backoff_reads_as_failed(env, monkeypatch):
    _stub_report(monkeypatch, _report("full", summarized=False))
    monkeypatch.setattr(server, "_attempt_note",
                        lambda sid: {"attempts": 3, "error": "ollama timeout",
                                     "backing_off": True, "age_s": 120})
    g = server._grade_summary(SID)
    assert g["state"] == "failed" and g["attempts"]["attempts"] == 3
    assert "backoff" in g["reason"] and "ollama timeout" in g["reason"]


def test_a_captured_session_is_not_marked_failed(env, monkeypatch):
    """_clear_attempts deletes the row on success; a lingering one must not
    repaint a session that IS captured as broken."""
    _stub_report(monkeypatch, _report("none", summarized=True, **{"pass": 2}))
    monkeypatch.setattr(server, "_attempt_note",
                        lambda sid: {"attempts": 1, "error": "old",
                                     "backing_off": False, "age_s": 99999})
    assert server._grade_summary(SID)["state"] == "captured"


# ---- the payload family -----------------------------------------------------

def test_nav_row_ships_summary_beside_tldr_and_timeline(env, monkeypatch):
    _stub_report(monkeypatch, _report("delta", summarized=True,
                                      **{"pass": 2, "since": "2026-08-21T09:00:00Z"}))
    monkeypatch.setattr(server, "PROJECTS", env.parent)
    row = server._nav_row(env, {}, {})
    assert row is not None
    assert set(("tldr", "timeline", "summary")) <= set(row)
    assert row["summary"]["state"] == "unknown"        # cached-first: not graded
    server._refresh_summaries()
    assert server._nav_row(env, {}, {})["summary"]["state"] == "delta"
