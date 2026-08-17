"""Unit tests for the tldr batch action (console/server.py).

Semantics under test:
  - create_batch accepts "tldr" and propagates options.force onto tldr items
    (and only tldr items).
  - _run_batch_item skip-if-fresh: a cached store whose turn_key matches the
    live transcript is marked done+skipped with NO generation.
  - stale cache -> generation goes through tldr.enqueue (the single worker
    lane), item resolves off tldr.STATUS.
  - a fresh negative-cache stub fails the item with the cached error, without
    re-running the model; force re-runs it.
  - a transcript with no real prompt fails its item; the batch survives.

Run:  uv run --extra dev pytest tests/test_batch_tldr.py -q
"""
import json

import pytest

from claude_session_db import tldr
from claude_session_db.console import server

SID = "bbbbbbbb-1111-2222-3333-444444444444"


def _write_transcript(path, with_prompt=True):
    recs = []
    if with_prompt:
        recs = [{"type": "user", "uuid": "u-1",
                 "timestamp": "2026-08-16T10:00:00Z",
                 "message": {"content": "do the thing"}},
                {"type": "assistant", "timestamp": "2026-08-16T10:00:05Z",
                 "message": {"content": [{"type": "text", "text": "ok"}],
                             "stop_reason": "end_turn"}}]
    else:
        recs = [{"type": "summary", "summary": "x"}]
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated state dir + batch registry + transcript; no worker threads."""
    monkeypatch.setenv("CSD_STATE_DIR", str(tmp_path / "state"))
    tldr._KEY_MEMO.clear()
    tldr.STATUS.clear()
    monkeypatch.setattr(server, "BATCH_FILE", tmp_path / "batch.json")
    monkeypatch.setattr(server, "_BATCHES", {})
    monkeypatch.setattr(server, "_ensure_batch_worker", lambda: None)
    path = _write_transcript(tmp_path / f"{SID}.jsonl")
    monkeypatch.setattr(server, "find_session",
                        lambda sid: path if sid == SID else None)
    generated = []

    def fake_enqueue(sid, p):
        generated.append(sid)
        tldr._persist(sid, {"session_id": sid, "turn_key": tldr.turn_key(p),
                            "headline": "fresh mock"})
        tldr.STATUS[sid] = "ok"
        return True

    monkeypatch.setattr(tldr, "enqueue", fake_enqueue)
    return {"path": path, "generated": generated}


def _make(force=False):
    payload, code = server.create_batch(
        ["tldr"], [SID], options={"force": True} if force else None)
    assert code == 200, payload
    return payload["batch"]


def _run(batch):
    server._run_batch_item(batch["id"], 0)
    return server._BATCHES[batch["id"]]["items"][0]


def test_create_batch_accepts_tldr_and_propagates_force(env):
    b = _make(force=True)
    assert b["items"][0]["action"] == "tldr"
    assert b["items"][0]["force"] is True
    assert _make(force=False)["items"][0]["force"] is False
    # non-tldr items never grow a force key
    p, code = server.create_batch(["angles"], [SID], options={"force": True})
    assert code == 200 and "force" not in p["batch"]["items"][0]


def test_stale_cache_generates_through_the_tldr_lane(env):
    tldr._persist(SID, {"session_id": SID, "turn_key": "old:done",
                        "headline": "stale"})
    it = _run(_make())
    assert it["status"] == "done" and not it.get("skipped")
    assert env["generated"] == [SID]
    assert tldr.get_cached(SID)["headline"] == "fresh mock"


def test_fresh_cache_is_skipped_without_generation(env):
    key = tldr.turn_key(env["path"])
    tldr._persist(SID, {"session_id": SID, "turn_key": key,
                        "headline": "already fresh"})
    it = _run(_make())
    assert it["status"] == "done" and it.get("skipped") is True
    assert env["generated"] == []                      # no model call
    assert tldr.get_cached(SID)["headline"] == "already fresh"


def test_fresh_error_stub_fails_without_rerun_but_force_reruns(env):
    key = tldr.turn_key(env["path"])
    stub = {"session_id": SID, "turn_key": key,
            "error": "ValueError: no complete turns"}
    tldr._persist(SID, stub)
    it = _run(_make())
    assert it["status"] == "failed"
    assert "no complete turns" in it["error"] and "cached" in it["error"]
    assert env["generated"] == []
    tldr._persist(SID, stub)                           # reset, then force
    it = _run(_make(force=True))
    assert it["status"] == "done" and env["generated"] == [SID]


def test_force_regenerates_a_fresh_store(env):
    tldr._persist(SID, {"session_id": SID, "turn_key": tldr.turn_key(env["path"]),
                        "headline": "already fresh"})
    it = _run(_make(force=True))
    assert it["status"] == "done" and not it.get("skipped")
    assert env["generated"] == [SID]


def test_no_prompt_transcript_fails_item_not_batch(env):
    _write_transcript(env["path"], with_prompt=False)
    tldr._KEY_MEMO.clear()
    it = _run(_make())
    assert it["status"] == "failed"
    assert "no user prompt" in it["error"]
    assert server._BATCHES  # registry intact; batch marked done, not crashed
    assert next(iter(server._BATCHES.values()))["done"] is True
