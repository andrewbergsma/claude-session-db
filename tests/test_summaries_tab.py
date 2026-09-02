"""The Summary rail tab — /api/summaries and the events-only dispatch.

Semantics under test (console/server.py):
  - summaries_payload merges RUNS from three independent sources: the
    `summary_passes` ledger (which alone sees the launchd phase-4 passes), the
    console-minted child runs on meta.json (the only durable record of a run
    this console spawned), and the in-process SUMMARY_RUNS tracker (rc + the
    in-flight state). A ledger row with no child reads "phase-4"; a child with
    no ledger row is an events capture.
  - ENTRIES dedupe by app:path across every transcript that wrote them, and a
    dry-run never outranks the real write it rehearsed (the Context tab's rule,
    which this reuses rather than re-implements). The corpus row supplies the
    title and confirms the entry actually landed.
  - DEGRADE, never 500: an unreachable ledger or knowledge DB costs only the
    part that needed it and lands in `warnings`; the transcript-derived half
    still renders.
  - A child (subagent) key is refused with 400, pointing at the parent.
  - "Capture events" is the SAME off-session dispatch with the /session-summary
    skill's own `--events` override — and deliberately takes NO pass ledger
    claim, because it writes no session entry and must not advance the pass
    number or imply a watermark.

Run:  uv run --extra dev pytest tests/test_summaries_tab.py -q
"""
from datetime import datetime, timezone

import pytest

from claude_session_db.console import server

SID = "aaaaaaaa-1111-2222-3333-444444444444"
CHILD1 = "cccccccc-0001-0000-0000-000000000001"
CHILD2 = "cccccccc-0002-0000-0000-000000000002"


def _dt(day, hour=12):
    return datetime(2026, 8, day, hour, tzinfo=timezone.utc)


FULL_SCOPE = {"delta": False, "pass": 1, "since": None, "source": "none",
              "prior": None, "mode": "auto", "records": None,
              "note": None, "warning": None}
DELTA_SCOPE = {"delta": True, "pass": 3, "since": "2026-08-20T09:00:00Z",
               "source": "leaf", "prior": "claudecode:session/2026-08-20/x",
               "mode": "auto", "records": 42, "note": None, "warning": None}


def _write_event(app, path, op, etype="event", ts="2026-08-21T10:00:00Z",
                 dry=False, err=False):
    return {"kind": "write", "ts": ts, "tool": "import_entries",
            "dry_run": dry, "is_error": err,
            "error": "refused" if err else None, "note": None,
            "refs": [{"app": app, "path": path, "etype": etype, "op": op}]}


@pytest.fixture
def wired(monkeypatch):
    """Every collaborator of summaries_payload stubbed to a quiet default; each
    test replaces only the source it is about."""
    monkeypatch.setattr(server, "resolve_summary_scope",
                        lambda sid, mode="auto": dict(FULL_SCOPE))
    monkeypatch.setattr(server, "_idle_warning", lambda sid: None)
    monkeypatch.setattr(server, "SUMMARIZING", {})
    monkeypatch.setattr(server, "SUMMARY_RUNS", {})
    monkeypatch.setattr(server, "_read_meta_overlay", lambda: {})
    monkeypatch.setattr(server, "_ledger_passes", lambda sid: [])
    monkeypatch.setattr(server, "_kmcp_entries", lambda sid, refs: [])
    monkeypatch.setattr(server, "build_session", lambda sid: None)
    return monkeypatch


# ---------------------------------------------------------------------------
# runs — the three-source merge
# ---------------------------------------------------------------------------

def test_a_ledger_pass_with_no_child_is_a_phase_4_run(wired):
    wired.setattr(server, "_ledger_passes", lambda sid: [
        {"pass": 1, "status": "written", "detail": None,
         "application": "claudecode", "path": "session/2026-08-10/a",
         "created_at": _dt(10), "updated_at": _dt(10, 13)}])
    out, code = server.summaries_payload(SID)
    assert code == 200
    (run,) = out["runs"]
    assert run["origin"] == "phase-4" and run["child"] is None
    assert run["ref"] == "claudecode:session/2026-08-10/a"
    assert run["status"] == "written" and run["in_ledger"] is True


def test_a_console_child_folds_into_its_ledger_row(wired):
    """One pass, two records of it — not two rows."""
    wired.setattr(server, "_ledger_passes", lambda sid: [
        {"pass": 2, "status": "written", "detail": None,
         "application": "claudecode", "path": "session/2026-08-20/b",
         "created_at": _dt(20), "updated_at": _dt(20, 13)}])
    wired.setattr(server, "_read_meta_overlay", lambda: {
        CHILD2: {"summary_of": SID, "summary_pass": 2, "summary_kind": "summary",
                 "title": "Summary of thing (pass 2)",
                 "set_at": "2026-08-20T12:30:00+00:00"},
        "unrelated": {"summary_of": "some-other-session", "summary_pass": 1}})
    out, _ = server.summaries_payload(SID)
    assert len(out["runs"]) == 1
    run = out["runs"][0]
    assert run["child"] == CHILD2 and run["origin"] == "console"
    assert run["ref"] == "claudecode:session/2026-08-20/b"


def test_an_events_child_never_folds_into_a_pass(wired):
    """An --events capture takes no pass number; folding it into pass 2 would
    make a partial capture look like the summary that closed the window."""
    wired.setattr(server, "_ledger_passes", lambda sid: [
        {"pass": 2, "status": "written", "detail": None,
         "application": "claudecode", "path": "session/2026-08-20/b",
         "created_at": _dt(20), "updated_at": _dt(20)}])
    wired.setattr(server, "_read_meta_overlay", lambda: {
        CHILD1: {"summary_of": SID, "summary_pass": 2, "summary_kind": "events",
                 "title": "Events of thing", "set_at": "2026-08-22T09:00:00+00:00"}})
    out, _ = server.summaries_payload(SID)
    assert len(out["runs"]) == 2
    kinds = {r["kind"]: r for r in out["runs"]}
    assert kinds["events"]["child"] == CHILD1
    assert kinds["events"]["in_ledger"] is False
    assert kinds["summary"]["child"] is None
    # newest first: the events capture (Aug 22) leads pass 2 (Aug 20)
    assert out["runs"][0]["kind"] == "events"


def test_the_in_process_tracker_supplies_the_in_flight_state(wired):
    wired.setattr(server, "SUMMARY_RUNS", {
        SID: {"child": CHILD1, "pass": 3, "started": 1.0, "ended": None,
              "rc": None, "kind": "summary"}})
    wired.setattr(server, "SUMMARIZING", {SID: "running"})
    out, _ = server.summaries_payload(SID)
    (run,) = out["runs"]
    assert run["status"] == "in_flight" and run["running"] is True
    assert run["child"] == CHILD1 and run["origin"] == "console"
    assert out["grade"]["running"] is True


def test_a_finished_console_run_with_no_ledger_row_settles_from_rc(wired):
    wired.setattr(server, "SUMMARY_RUNS", {
        SID: {"child": CHILD1, "pass": 1, "started": 1.0, "ended": 9.0,
              "rc": 2, "kind": "events"}})
    out, _ = server.summaries_payload(SID)
    (run,) = out["runs"]
    assert run["status"] == "failed" and run["detail"] == "child rc=2"


def test_a_legacy_child_takes_its_pass_from_its_title(wired):
    """Runs minted before summary_pass existed on the overlay still number."""
    wired.setattr(server, "_read_meta_overlay", lambda: {
        CHILD2: {"summary_of": SID, "title": "Summary of thing (pass 4)",
                 "set_at": "2026-08-25T08:00:00+00:00"}})
    out, _ = server.summaries_payload(SID)
    assert out["runs"][0]["pass"] == 4


# ---------------------------------------------------------------------------
# entries — dedupe, verdicts, and the corpus join
# ---------------------------------------------------------------------------

def _sessions(map_):
    return lambda sid: map_.get(sid)


def test_writes_from_the_session_and_from_each_run_are_deduped(wired):
    wired.setattr(server, "_read_meta_overlay", lambda: {
        CHILD1: {"summary_of": SID, "summary_pass": 1, "summary_kind": "summary",
                 "title": "Summary of thing (pass 1)",
                 "set_at": "2026-08-21T09:00:00+00:00"}})
    wired.setattr(server, "build_session", _sessions({
        SID: {"events": [_write_event("claudecode", "event/2026-08-21/a", "created")]},
        CHILD1: {"events": [
            _write_event("claudecode", "event/2026-08-21/a", "updated",
                         ts="2026-08-21T11:00:00Z"),
            _write_event("claudecode", "session/2026-08-21/t", "created",
                         etype="session", ts="2026-08-21T11:05:00Z")]}}))
    out, _ = server.summaries_payload(SID)
    keys = [e["key"] for e in out["entries"]]
    assert keys == ["claudecode:session/2026-08-21/t",
                    "claudecode:event/2026-08-21/a"]      # newest first
    dup = out["entries"][1]
    assert dup["verdict"] == "updated"                    # the LATER write wins
    assert dup["by_labels"] == ["this session", "pass 1 run"]


def test_a_dry_run_never_outranks_the_write_it_rehearsed(wired):
    wired.setattr(server, "build_session", _sessions({SID: {"events": [
        _write_event("claudecode", "lesson/x", "created", etype="lesson",
                     ts="2026-08-21T10:00:00Z"),
        _write_event("claudecode", "lesson/x", "created", etype="lesson",
                     ts="2026-08-21T10:05:00Z", dry=True)]}}))
    out, _ = server.summaries_payload(SID)
    assert out["entries"][0]["verdict"] == "created"


def test_a_dry_run_alone_renders_as_a_dry_run(wired):
    wired.setattr(server, "build_session", _sessions({SID: {"events": [
        _write_event("claudecode", "lesson/x", "created", etype="lesson",
                     dry=True)]}}))
    out, _ = server.summaries_payload(SID)
    assert out["entries"][0]["verdict"] == "dry-run"


def test_a_refused_write_renders_as_an_error(wired):
    wired.setattr(server, "build_session", _sessions({SID: {"events": [
        _write_event("claudecode", "lesson/x", "created", etype="lesson",
                     err=True)]}}))
    e = server.summaries_payload(SID)[0]["entries"][0]
    assert e["verdict"] == "error" and e["error"] == "refused"
    assert e["in_kmcp"] is False


def test_the_corpus_row_supplies_title_and_confirms_the_write_landed(wired):
    wired.setattr(server, "build_session", _sessions({SID: {"events": [
        _write_event("claudecode", "session/2026-08-21/t", "created",
                     etype="session")]}}))
    wired.setattr(server, "_kmcp_entries", lambda sid, refs: [
        {"application": "claudecode", "path": "session/2026-08-21/t",
         "entity_type": "session", "title": "Session: the thing",
         "description": None, "version": 1,
         "created_at": _dt(21), "updated_at": _dt(21)}])
    e = server.summaries_payload(SID)[0]["entries"][0]
    assert e["in_kmcp"] is True and e["title"] == "Session: the thing"
    assert e["verdict"] == "created"


def test_a_corpus_only_entry_still_lists_with_a_version_derived_verdict(wired):
    """A pass written before the console existed leaves no transcript here —
    the corpus row is the only evidence, and version says which way it landed."""
    wired.setattr(server, "_kmcp_entries", lambda sid, refs: [
        {"application": "claudecode", "path": "session/2026-01-01/old",
         "entity_type": "session", "title": "Old", "description": None,
         "version": 3, "created_at": _dt(1), "updated_at": _dt(2)}])
    e = server.summaries_payload(SID)[0]["entries"][0]
    assert e["verdict"] == "updated" and e["by_labels"] == []


def test_both_the_ledger_refs_and_the_written_refs_go_to_the_one_query(wired):
    """The refs arm covers what the transcripts wrote too — otherwise a patched
    owner or a lesson reports 'not in corpus' purely for never being looked up."""
    seen = {}
    wired.setattr(server, "_ledger_passes", lambda sid: [
        {"pass": 1, "status": "written", "detail": None,
         "application": "claudecode", "path": "session/2026-08-10/a",
         "created_at": _dt(10), "updated_at": _dt(10)}])
    wired.setattr(server, "build_session", _sessions({SID: {"events": [
        _write_event("claudecode", "lesson/patched-owner", "patched",
                     etype="lesson")]}}))

    def _k(sid, refs):
        seen["sid"], seen["refs"] = sid, refs
        seen["calls"] = seen.get("calls", 0) + 1
        return []
    wired.setattr(server, "_kmcp_entries", _k)
    out, _ = server.summaries_payload(SID)
    assert seen["refs"] == ["claudecode:lesson/patched-owner",
                            "claudecode:session/2026-08-10/a"]
    assert seen["calls"] == 1               # bounded: ONE query per source
    assert out["corpus_ok"] is True


# ---------------------------------------------------------------------------
# degrade doctrine + the child-key refusal
# ---------------------------------------------------------------------------

def test_an_unreachable_ledger_degrades_to_the_console_runs(wired):
    def boom(sid):
        raise RuntimeError("connection refused")
    wired.setattr(server, "_ledger_passes", boom)
    wired.setattr(server, "_read_meta_overlay", lambda: {
        CHILD1: {"summary_of": SID, "summary_pass": 1, "summary_kind": "summary",
                 "title": "Summary of thing (pass 1)",
                 "set_at": "2026-08-21T09:00:00+00:00"}})
    out, code = server.summaries_payload(SID)
    assert code == 200 and len(out["runs"]) == 1
    assert any("pass ledger unavailable" in w and "connection refused" in w
               for w in out["warnings"])


def test_an_unreachable_knowledge_db_still_renders_the_transcript_half(wired):
    def boom(sid, refs):
        raise RuntimeError("no route to host")
    wired.setattr(server, "_kmcp_entries", boom)
    wired.setattr(server, "build_session", _sessions({SID: {"events": [
        _write_event("claudecode", "event/2026-08-21/a", "created")]}}))
    out, code = server.summaries_payload(SID)
    assert code == 200 and len(out["entries"]) == 1
    assert out["entries"][0]["in_kmcp"] is False
    # corpus_ok is what stops the UI turning an unqueried ref into an accusation
    assert out["corpus_ok"] is False
    assert any("knowledge DB unavailable" in w for w in out["warnings"])


def test_an_unreadable_run_transcript_isolates_to_a_warning(wired):
    wired.setattr(server, "_read_meta_overlay", lambda: {
        CHILD1: {"summary_of": SID, "summary_pass": 1, "summary_kind": "summary",
                 "title": "Summary of thing (pass 1)",
                 "set_at": "2026-08-21T09:00:00+00:00"}})

    def _bs(sid):
        if sid == CHILD1:
            raise OSError("gone")
        return {"events": [_write_event("claudecode", "event/x", "created")]}
    wired.setattr(server, "build_session", _bs)
    out, code = server.summaries_payload(SID)
    assert code == 200 and len(out["entries"]) == 1
    assert any("unreadable" in w for w in out["warnings"])


def test_an_unresolvable_scope_is_surfaced_not_swallowed(wired):
    wired.setattr(server, "resolve_summary_scope", lambda sid, mode="auto": dict(
        FULL_SCOPE, note="prior capture unresolved (RuntimeError: x) — full scope"))
    out, code = server.summaries_payload(SID)
    assert code == 200 and out["grade"]["scope"] == "full"
    assert any("prior capture unresolved" in w for w in out["warnings"])


def test_a_child_key_is_refused_pointing_at_the_parent(wired):
    out, code = server.summaries_payload(f"{SID}:0123456789abcdef0")
    assert code == 400 and out["parent"] == SID
    assert "parent" in out["error"]


def test_the_grade_reads_as_the_button_labels_do(wired):
    wired.setattr(server, "resolve_summary_scope",
                  lambda sid, mode="auto": dict(DELTA_SCOPE))
    wired.setattr(server, "_idle_warning", lambda sid: "session wrote 12s ago")
    g = server.summaries_payload(SID)[0]["grade"]
    assert g["scope"] == "delta" and g["pass"] == 3
    assert g["source_label"] == "leaf_uuid" and g["summarized"] is True
    assert g["idle_warning"] == "session wrote 12s ago"


# ---------------------------------------------------------------------------
# "Capture events" — the same dispatch, the skill's own --events override
# ---------------------------------------------------------------------------

class _Proc:
    pid = 4242
    envelope_note = None


@pytest.fixture
def dispatch(monkeypatch, tmp_path):
    """Capture what summarize_session hands spawn_claude, writing nothing."""
    seen = {}

    def _spawn(args, cwd, session_id=None, log_path=None, action=None,
               envelope_ctx=None):
        seen.update(args=args, cwd=cwd, child=session_id, action=action)
        return _Proc()

    def _claim(sid, pass_no):
        seen["claimed"] = pass_no
        return {"conn": None, "refused": None, "note": None}

    monkeypatch.setattr(server, "spawn_claude", _spawn)
    monkeypatch.setattr(server, "_claim_pass", _claim)
    monkeypatch.setattr(server, "resolve_summary_scope",
                        lambda sid, mode="auto": dict(FULL_SCOPE))
    monkeypatch.setattr(server, "_idle_warning", lambda sid: None)
    monkeypatch.setattr(server, "find_session", lambda sid: None)
    monkeypatch.setattr(server, "set_archived",
                        lambda sid, on, reason="": seen.update(archived=on))
    monkeypatch.setattr(server, "_read_meta_overlay", lambda: {})
    monkeypatch.setattr(server, "_update_meta",
                        lambda sid, **f: seen.setdefault("meta", []).append((sid, f)))
    monkeypatch.setattr(server, "set_title",
                        lambda sid, t: seen.setdefault("titles", []).append(t))
    monkeypatch.setattr(server, "_SUMMARY_LOG_DIR", tmp_path)
    monkeypatch.setattr(server, "_await_summary",
                        lambda *a, **k: None)      # the watcher thread no-ops
    monkeypatch.setattr(server, "SUMMARIZING", {})
    monkeypatch.setattr(server, "SUMMARY_RUNS", {})
    return seen


def test_capture_events_appends_the_skills_own_events_override(dispatch):
    r = server.summarize_session(SID, "/tmp", events_only=True)
    assert r["ok"] and r["events_only"] is True and r["action"] == "events"
    assert dispatch["args"][1] == f"/session-summary {SID} --events"
    assert dispatch["action"] == "summarize"          # same envelope, same skill


def test_capture_events_takes_no_pass_ledger_claim_and_never_archives(dispatch):
    """It writes no session entry — a ledger row would advance the pass number
    and imply a watermark the reconcile gate would later stamp."""
    r = server.summarize_session(SID, "/tmp", archive=True, events_only=True)
    assert "claimed" not in dispatch and "archived" not in dispatch
    assert r["archived"] is False
    assert "changelog events only" in r["note"]
    assert any(f.get("summary_kind") == "events" for _sid, f in dispatch["meta"])
    assert dispatch["titles"] == ["Events of aaaaaaaa"]


def test_a_plain_summarize_still_claims_its_pass(dispatch):
    server.summarize_session(SID, "/tmp", archive=False)
    assert dispatch["claimed"] == 1
    assert dispatch["args"][1] == f"/session-summary {SID}"
