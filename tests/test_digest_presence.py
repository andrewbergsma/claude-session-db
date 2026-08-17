"""Presence + ensure + title-proposal semantics (sidebar digests feature).

Under test:
  - session_timeline.presence: absent/stale/fresh/error/generating states,
    and the store-signature memo (an unchanged store is never re-read).
  - tldr.ensure / session_timeline.ensure: run-if-absent-or-stale — fresh is
    a no-op, a fresh negative-cache stub is respected (state "error", no
    retry), force regenerates.
  - tldr.generate persists title_proposal from the same single model call.
  - server._pending_proposal: a manual title is never auto-overwritten — the
    proposal is only ever a suggestion, suppressed once accepted or dismissed.

Run:  uv run --extra dev pytest tests/test_digest_presence.py -q
"""
import json
from pathlib import Path

import pytest

from claude_session_db import session_timeline as stl
from claude_session_db import tldr
from claude_session_db.console import server

SID = "cccccccc-1111-2222-3333-444444444444"


def _transcript(path: Path, n_turns=1):
    recs = []
    for i in range(n_turns):
        recs += [{"type": "user", "uuid": f"u-{i}",
                  "timestamp": f"2026-08-16T10:0{i}:00Z",
                  "message": {"content": f"do thing {i}"}},
                 {"type": "assistant", "timestamp": f"2026-08-16T10:0{i}:05Z",
                  "message": {"content": [{"type": "text", "text": "ok"}],
                              "stop_reason": "end_turn"}}]
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CSD_STATE_DIR", str(tmp_path / "state"))
    tldr._KEY_MEMO.clear()
    tldr._STORE_MEMO.clear()
    tldr.STATUS.clear()
    stl._PRES_MEMO.clear()
    stl.STATUS.clear()
    return _transcript(tmp_path / f"{SID}.jsonl")


# ---- timeline presence ------------------------------------------------------

def test_presence_absent_then_fresh_then_stale(env):
    assert stl.presence(SID, env)["state"] == "absent"
    key = tldr.turn_key(env)
    stl._persist(SID, {"session_id": SID, "turn_key": key,
                       "rows": [{"uuid": "u-0", "t": "", "summary": "did it"}],
                       "generated_at": "2026-08-16T10:01:00"})
    p = stl.presence(SID, env)
    assert p["state"] == "fresh" and p["rows"] == 1
    _transcript(env, n_turns=2)          # a new turn lands
    tldr._KEY_MEMO.clear()
    assert stl.presence(SID, env)["state"] == "stale"


def test_presence_error_and_partial(env):
    stl._persist(SID, {"session_id": SID, "turn_key": tldr.turn_key(env),
                       "error": "ValueError: boom"})
    assert stl.presence(SID, env)["state"] == "error"
    stl._persist(SID, {"session_id": SID, "turn_key": tldr.turn_key(env),
                       "rows": [{"uuid": "u-0", "t": "", "summary": "x"}],
                       "partial": True})
    assert stl.presence(SID, env)["state"] == "stale"   # orphaned partial


def test_presence_generating_overrides(env):
    stl.STATUS[SID] = "generating 3/9"
    assert stl.presence(SID, env)["state"] == "generating"
    stl.STATUS[SID] = "ok"
    assert stl.presence(SID, env)["state"] == "absent"


def test_presence_memo_skips_rereads(env, monkeypatch):
    stl._persist(SID, {"session_id": SID, "turn_key": tldr.turn_key(env),
                       "rows": [{"uuid": "u-0", "t": "", "summary": "x"}]})
    assert stl.presence(SID, env)["state"] == "fresh"
    # Unchanged store: presence must serve from the memo, never re-read.
    def boom(self, *a, **kw):
        raise AssertionError("store re-read on an unchanged poll")
    monkeypatch.setattr(Path, "read_text", boom)
    assert stl.presence(SID, env)["state"] == "fresh"


# ---- ensure (run-if-absent-or-stale, the automation seam) -------------------

def test_tldr_ensure_states(env, monkeypatch):
    queued = []
    monkeypatch.setattr(tldr, "enqueue", lambda s, p: queued.append(s) or True)
    key = tldr.turn_key(env)
    assert tldr.ensure(SID, env)["state"] == "queued"        # absent -> run
    tldr._persist(SID, {"session_id": SID, "turn_key": key, "headline": "h"})
    r = tldr.ensure(SID, env)
    assert r["state"] == "fresh" and not r["queued"]          # fresh -> no-op
    tldr._persist(SID, {"session_id": SID, "turn_key": "old:done",
                        "headline": "h"})
    assert tldr.ensure(SID, env)["state"] == "queued"         # stale -> run
    tldr._persist(SID, {"session_id": SID, "turn_key": key, "error": "nope"})
    r = tldr.ensure(SID, env)
    assert r["state"] == "error" and not r["queued"]          # stub respected
    assert tldr.ensure(SID, env, force=True)["state"] == "queued"
    assert queued == [SID, SID, SID]


def test_timeline_ensure_states(env, monkeypatch):
    queued = []
    monkeypatch.setattr(stl, "_enqueue", lambda s, p: queued.append(s))
    assert stl.ensure(SID, env)["state"] == "queued"          # absent -> run
    stl._persist(SID, {"session_id": SID, "turn_key": tldr.turn_key(env),
                       "rows": [{"uuid": "u-0", "t": "", "summary": "x"}]})
    assert stl.ensure(SID, env)["state"] == "fresh"           # fresh -> no-op
    stl._persist(SID, {"session_id": SID, "turn_key": tldr.turn_key(env),
                       "error": "boom"})
    assert stl.ensure(SID, env)["state"] == "error"           # no retry loop
    assert stl.ensure(SID, env, force=True)["state"] == "queued"
    assert queued == [SID, SID]


def test_ensure_no_turns(tmp_path, monkeypatch):
    monkeypatch.setenv("CSD_STATE_DIR", str(tmp_path / "state"))
    tldr._KEY_MEMO.clear()
    p = tmp_path / f"{SID}.jsonl"
    p.write_text(json.dumps({"type": "summary", "summary": "x"}) + "\n")
    assert tldr.ensure(SID, p) == {"ok": False, "state": "no-turns",
                                   "queued": False}
    assert stl.ensure(SID, p)["state"] == "no-turns"


# ---- title proposal ---------------------------------------------------------

def test_generate_persists_title_proposal(env, monkeypatch):
    monkeypatch.setitem(tldr.BACKENDS, "ollama", lambda prompt, **kw: {
        "about": "console feature work", "doing": "editing server.py",
        "title": "Sidebar digest chips", "detail": "d"})
    store = tldr.generate(SID, env)
    assert store["title_proposal"] == "Sidebar digest chips"
    assert tldr.get_cached(SID)["title_proposal"] == "Sidebar digest chips"
    # peek surfaces it without enqueuing anything
    assert tldr.peek(SID, env)["title_proposal"] == "Sidebar digest chips"


def test_pending_proposal_guards():
    tl = {"title_proposal": "Sidebar digest chips"}
    # no manual title -> offered
    assert server._pending_proposal(tl, None, None) == "Sidebar digest chips"
    # manual title present but different -> still only a SUGGESTION (never
    # auto-applied; there is no code path that writes it without /api/title)
    assert server._pending_proposal(tl, "My own name", None) \
        == "Sidebar digest chips"
    # accepted (title == proposal, case-insensitive) -> nothing pending
    assert server._pending_proposal(tl, "sidebar digest chips", None) is None
    # dismissed exactly this proposal -> nothing pending
    assert server._pending_proposal(tl, None, "Sidebar digest chips") is None
    # a dismissal of an OLDER proposal does not mute a new one
    assert server._pending_proposal(tl, None, "old proposal") \
        == "Sidebar digest chips"
    assert server._pending_proposal(None, None, None) is None
    assert server._pending_proposal({"error": "x"}, None, None) is None


def test_dismiss_title_proposal_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "META_FILE", tmp_path / "meta.json")
    monkeypatch.setattr(server, "CONSOLE_STATE", tmp_path)
    r = server.dismiss_title_proposal(SID, "Sidebar digest chips")
    assert r["ok"]
    m = server._meta_of(server._read_meta_overlay(), SID)
    assert m["tp_dismissed"] == "Sidebar digest chips"
    # a manual title set later coexists; dismissal survives
    server.set_title(SID, "Hand-picked")
    m = server._meta_of(server._read_meta_overlay(), SID)
    assert m["title"] == "Hand-picked"
    assert m["tp_dismissed"] == "Sidebar digest chips"
    assert server.dismiss_title_proposal(SID, "")["ok"] is False
