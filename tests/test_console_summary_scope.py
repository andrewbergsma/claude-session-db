"""Stage 2 — the console's half of repeatable delta summarization.

Semantics under test (console/server.py):
  - resolve_summary_scope: auto opens a delta window only when the gate does;
    force windows it anyway; off is the historic full scope; and NOTHING here
    raises — an unreachable archive degrades to full pass 1 with the reason
    surfaced, exactly like resolve_envelope.
  - _prior_capture: a session captured BEFORE the pass ledger existed still
    counts as pass 1, so its continuation is pass 2 rather than a second pass 1.
  - _envelope_prompt: a delta pass hands the child the window, the literal
    `session_digest.py --since` command and the prior entry to link back to.
  - summarize_session(dry_run=True): resolves scope without claiming, spawning
    or archiving; the idle warning is surfaced, never enforced.

Run:  uv run --extra dev pytest tests/test_console_summary_scope.py -q
"""
import time

import pytest

from claude_session_db import summarize as ph4
from claude_session_db.console import server

SID = "dddddddd-1111-2222-3333-555555555555"
WM_ISO = "2026-08-01T10:00:00Z"


class _Gate:
    """A _delta_gate verdict, shaped by the case under test."""

    def __init__(self, **kw):
        d = {"mode": "delta", "pass_no": 2, "watermark": object(),
             "source": "leaf", "report": None, "end_ts": None,
             "prev_ref": "claudecode:session/2026-08-01/first", "skip": None}
        d.update(kw)
        self.__dict__.update(d)


@pytest.fixture
def scoped(monkeypatch):
    """Wire resolve_summary_scope's two collaborators, nothing else."""
    monkeypatch.setattr(server, "CSD_DSN", "dsn")
    monkeypatch.setattr(server, "KMCP_DSN", "kdsn")
    monkeypatch.setattr(ph4, "_iso", lambda dt: WM_ISO if dt is not None else None)

    def install(row=None, gate=None):
        monkeypatch.setattr(server, "_prior_capture",
                            lambda sid: row if row is not None
                            else {"session_id": sid, "prev_pass": 1})
        monkeypatch.setattr(ph4, "_delta_gate",
                            lambda r, dsn, kdsn, **kw: gate or _Gate())
    return install


def test_auto_opens_the_delta_window_the_gate_opened(scoped):
    scoped()
    sc = server.resolve_summary_scope(SID, "auto")
    assert sc["delta"] and sc["since"] == WM_ISO and sc["pass"] == 2
    assert sc["prior"] == "claudecode:session/2026-08-01/first"
    assert sc["warning"] is None


def test_auto_falls_back_to_full_and_SAYS_it_restates(scoped):
    """A thin tail has nothing to say — but the fallback re-summarizes the whole
    session, and that is the surprising half, so it must be surfaced."""
    scoped(gate=_Gate(skip="delta not substantive (class=confirmation_only, 3 records)"))
    sc = server.resolve_summary_scope(SID, "auto")
    assert sc["delta"] is False and sc["since"] is None
    assert "FULL session again" in sc["warning"] and "not substantive" in sc["warning"]


def test_force_windows_a_tail_the_gate_would_have_skipped(scoped):
    scoped(gate=_Gate(skip="delta below floor (4 < 20 records)"))
    sc = server.resolve_summary_scope(SID, "force")
    assert sc["delta"] and sc["since"] == WM_ISO
    assert "below floor" in sc["warning"] and "dispatched anyway" in sc["warning"]


def test_pass_ceiling_stays_a_delta_even_on_auto(scoped):
    """Past the ceiling the honest scope is still the tail: falling back to full
    would restate every earlier pass at once."""
    scoped(gate=_Gate(skip=f"pass ceiling reached (6/{ph4.MAX_PASSES})"))
    sc = server.resolve_summary_scope(SID, "auto")
    assert sc["delta"] and "ceiling" in sc["warning"]


def test_off_is_the_historic_full_scope(scoped):
    scoped(gate=_Gate(mode="full", watermark=None, source="none"))
    sc = server.resolve_summary_scope(SID, "off")
    assert sc["delta"] is False and sc["since"] is None
    assert "disabled" in sc["note"]


def test_no_watermark_is_never_dressed_up_as_a_continuation(scoped):
    scoped(gate=_Gate(mode="full", watermark=None, source="none", pass_no=2))
    sc = server.resolve_summary_scope(SID, "force")
    assert sc["delta"] is False and sc["since"] is None
    assert "no summary watermark" in sc["note"]


def test_an_unreachable_archive_degrades_to_full_pass_1(monkeypatch):
    """Doctrine: the resolver can never block the console."""
    def boom(sid):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(server, "_prior_capture", boom)
    sc = server.resolve_summary_scope(SID, "auto")
    assert sc == {"delta": False, "pass": 1, "since": None, "source": "none",
                  "prior": None, "mode": "auto", "records": None,
                  "warning": None,
                  "note": "prior capture unresolved (RuntimeError: connection "
                          "refused) — full scope"}


# ---------------------------------------------------------------------------
# _prior_capture — the pass number a legacy capture implies
# ---------------------------------------------------------------------------

class _FakeCur:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, prior, has_table, ledger):
        self._prior, self._has, self._ledger = prior, has_table, ledger
        self.read_only = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if "to_regclass" in sql:
            return _FakeCur({"t": "summary_passes" if self._has else None})
        if "max(pass)" in sql:
            return _FakeCur({"p": self._ledger})
        return _FakeCur(self._prior)


def _capture(monkeypatch, prior, has_table=True, ledger=None):
    import psycopg
    monkeypatch.setattr(server, "CSD_DSN", "dsn")
    monkeypatch.setattr(psycopg, "connect",
                        lambda *a, **kw: _FakeConn(prior, has_table, ledger))
    return server._prior_capture(SID)


def test_a_pre_ledger_capture_still_counts_as_pass_1(monkeypatch):
    row = _capture(monkeypatch, {"state": "summarized", "prev_wm": 120,
                                 "prev_leaf": "m-120", "prev_app": "claudecode",
                                 "prev_path": "session/2026-08-01/first"},
                   ledger=None)
    assert row["prev_pass"] == 1          # => the next pass is 2, not a second 1


def test_the_ledger_wins_when_it_is_ahead(monkeypatch):
    row = _capture(monkeypatch, {"state": "summarized", "prev_wm": 120},
                   ledger=3)
    assert row["prev_pass"] == 3


def test_a_never_captured_session_is_pass_0(monkeypatch):
    row = _capture(monkeypatch, {"state": None, "prev_wm": None,
                                 "prev_leaf": None, "prev_app": None}, ledger=None)
    assert row["prev_pass"] == 0 and row["session_id"] == SID


def test_a_missing_ledger_table_is_not_an_error(monkeypatch):
    row = _capture(monkeypatch, {"state": "summarized", "prev_wm": 9},
                   has_table=False)
    assert row["prev_pass"] == 1


# ---------------------------------------------------------------------------
# the child's framing
# ---------------------------------------------------------------------------

def test_envelope_prompt_hands_the_child_the_window_and_the_command():
    p = server._envelope_prompt(
        {"guardrails": [], "agent_ref": None},
        {"session_id": SID, "transcript": "/tmp/s.jsonl", "since": WM_ISO,
         "pass": 3, "prior": "claudecode:session/2026-08-01/first"})
    assert "CONTINUATION PASS 3" in p
    assert f'--since "{WM_ISO}"' in p and "session_digest.py" in p
    assert "/tmp/s.jsonl" in p
    assert "linked_entries" in p and "claudecode:session/2026-08-01/first" in p
    assert "must come from that tail alone" in p


def test_envelope_prompt_is_byte_for_byte_the_old_one_without_a_window():
    ctx = {"session_id": SID, "transcript": "/tmp/s.jsonl"}
    p = server._envelope_prompt({"guardrails": [], "agent_ref": None}, ctx)
    assert "CONTINUATION" not in p and "--since" not in p


def test_a_delta_pass_with_no_resolvable_prior_says_so():
    p = server._envelope_prompt(
        {"guardrails": [], "agent_ref": None},
        {"session_id": SID, "transcript": "/tmp/s.jsonl", "since": WM_ISO,
         "pass": 2, "prior": None})
    assert "could not be resolved" in p


# ---------------------------------------------------------------------------
# dispatch — dry_run resolves without claiming, spawning or archiving
# ---------------------------------------------------------------------------

@pytest.fixture
def dispatchable(monkeypatch, tmp_path):
    src = tmp_path / f"{SID}.jsonl"
    src.write_text("{}\n")
    monkeypatch.setattr(server, "find_session", lambda sid: src)
    monkeypatch.setattr(server, "SUMMARIZING", {})
    for name in ("spawn_claude", "_claim_pass", "set_archived"):
        monkeypatch.setattr(server, name, _never_called(name))
    return src


def _never_called(name):
    def guard(*a, **kw):
        raise AssertionError(f"{name}() must not run on a dry run")
    return guard


def test_dry_run_resolves_the_scope_without_dispatching(monkeypatch, scoped,
                                                       dispatchable):
    scoped()
    r = server.summarize_session(SID, "/tmp/proj", archive=True,
                                 delta="auto", dry_run=True)
    assert r["ok"] and r["dry_run"] and r["archived"] is False
    assert (r["pass"], r["since"], r["delta_mode"]) == (2, WM_ISO, "delta")
    assert r["prior"] == "claudecode:session/2026-08-01/first"


def test_a_freshly_written_transcript_warns_but_never_blocks(monkeypatch, scoped,
                                                             dispatchable):
    scoped()
    monkeypatch.setattr(server, "answer_blocked", lambda sid: None)
    r = server.summarize_session(SID, "", delta="auto", dry_run=True)
    assert r["ok"] and "NOT in this summary" in r["warning"]


def test_a_quiesced_transcript_carries_no_warning(monkeypatch, scoped,
                                                  dispatchable):
    scoped()
    old = time.time() - (server.SUMMARIZE_IDLE_WARN_S + 60)
    import os
    os.utime(dispatchable, (old, old))
    monkeypatch.setattr(server, "answer_blocked", lambda sid: None)
    r = server.summarize_session(SID, "", delta="auto", dry_run=True)
    assert r["ok"] and r["warning"] is None


def test_a_run_in_flight_warns_that_its_tail_is_not_captured(monkeypatch, scoped,
                                                             dispatchable):
    scoped()
    monkeypatch.setattr(server, "answer_blocked",
                        lambda sid: "console-spawned run still in flight")
    r = server.summarize_session(SID, "", delta="auto", dry_run=True)
    assert "still in flight" in r["warning"]


def test_an_unknown_delta_mode_falls_back_to_auto(monkeypatch, scoped,
                                                  dispatchable):
    scoped()
    r = server.summarize_session(SID, "", delta="sideways", dry_run=True)
    assert r["delta_mode"] == "delta"


def test_a_lost_claim_refuses_and_never_spawns(monkeypatch, scoped, dispatchable):
    """The one thing that DOES block: two passes digesting the same tail would
    write two entries. Everything else about the ledger degrades quietly."""
    scoped()
    monkeypatch.setattr(server, "_claim_pass",
                        lambda sid, n: {"conn": None, "note": None,
                                        "refused": "another summarize pass is "
                                                   "already in flight"})
    r = server.summarize_session(SID, "", delta="auto")   # spawn_claude would raise
    assert r["ok"] is False and "already in flight" in r["error"]
    assert r["pass"] == 2 and r["delta_mode"] == "delta"  # the scope still travels


def test_an_unrecorded_pass_is_a_note_not_a_refusal(monkeypatch):
    monkeypatch.setattr(server, "CSD_DSN", None)
    claim = server._claim_pass(SID, 2)
    assert claim["refused"] is None and "not recorded" in claim["note"]
