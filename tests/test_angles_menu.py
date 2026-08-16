"""Unit tests for the mine-angles menu plumbing.

Two pieces carry the feature:
  - angle_catalog() (console/server.py) — the menu's data source, enumerated
    from ANGLE_SPECS with per-session mined counts off the store on disk.
  - run_angles(angles=[...]) subset merge (angles.py) — mining ONE angle
    carries the other angles' items forward when the turn is unchanged, and
    never mixes turns. Run:

    uv run --extra dev pytest tests/test_angles_menu.py -q
"""
import json

import pytest

from claude_session_db import angles as A
from claude_session_db.console import server


SID = "aaaaaaaa-1111-2222-3333-444444444444"


def _rec_user(text, ts):
    return {"type": "user", "timestamp": ts, "cwd": "/tmp/proj",
            "gitBranch": "main", "message": {"content": text}}


def _rec_assistant(ts, tool_uses):
    content = [{"type": "text", "text": "working"}]
    for i, (name, inp) in enumerate(tool_uses):
        content.append({"type": "tool_use", "name": name, "input": inp,
                        "id": f"t{i}"})
    return {"type": "assistant", "timestamp": ts, "version": "9.9.9",
            "message": {"model": "test-model", "content": content,
                        "usage": {"input_tokens": 5, "output_tokens": 2}}}


@pytest.fixture
def transcript(tmp_path, monkeypatch):
    """A fake projects dir with one two-tool turn, plus an isolated state dir."""
    proj = tmp_path / "projects" / "-tmp-proj"
    proj.mkdir(parents=True)
    f = proj / f"{SID}.jsonl"
    recs = [
        _rec_user("please fix the bug", "2026-08-14T10:00:00Z"),
        _rec_assistant("2026-08-14T10:00:05Z", [
            ("Edit", {"file_path": "/tmp/proj/a.py", "old_string": "x",
                      "new_string": "y"}),
            ("Bash", {"command": "pytest -q", "description": "Run tests"}),
        ]),
    ]
    f.write_text("\n".join(json.dumps(r) for r in recs))
    monkeypatch.setattr(A, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setenv("CSD_STATE_DIR", str(tmp_path / "state"))
    return f


def _store():
    return json.loads((A._state_dir() / f"{SID}.json").read_text())


def _angles_of(store):
    return {i["angle"] for i in store["items"].values()}


# ---- catalog ----------------------------------------------------------------
def test_catalog_enumerates_registry():
    cat = server.angle_catalog("")
    ids = [a["id"] for a in cat["angles"]]
    assert ids == list(A.ANGLE_SPECS)          # every angle, registry order
    for a in cat["angles"]:
        assert a["label"] and a["prefix"] and a["kind"]
        assert a["mined"] == 0                 # no session named


def test_catalog_counts_mined(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ANGLES_DIR", tmp_path)
    (tmp_path / f"{SID}.json").write_text(json.dumps({
        "generated_at": "2026-08-14T10:00:10-0700",
        "items": {"F1": {"angle": "files", "headline": "a.py edit×1"},
                  "F2": {"angle": "files", "headline": "b.py write×1"},
                  "X1": {"angle": "commands", "headline": "Run tests"}}}))
    by_id = {a["id"]: a for a in server.angle_catalog(SID)["angles"]}
    assert by_id["files"]["mined"] == 2
    assert by_id["commands"]["mined"] == 1
    assert by_id["direction"]["mined"] == 0


# ---- subset merge -----------------------------------------------------------
def test_subset_mine_keeps_other_angles_same_turn(transcript):
    A.run_angles(cwd="", session_id=SID, no_probes=True)   # full det mine
    full = _angles_of(_store())
    assert {"files", "commands"} <= full

    A.run_angles(cwd="", session_id=SID, angles=["commands"], no_probes=True)
    after = _store()
    assert _angles_of(after) == full           # nothing wiped
    assert after["items"]["X1"]["detail"]["command"] == "pytest -q"


def test_subset_mine_drops_stale_turn(transcript):
    A.run_angles(cwd="", session_id=SID, no_probes=True)
    # A new turn lands (no Edit this time) — the store must not mix turns.
    with transcript.open("a") as f:
        f.write("\n" + json.dumps(_rec_user("now deploy it",
                                            "2026-08-14T11:00:00Z")))
        f.write("\n" + json.dumps(_rec_assistant("2026-08-14T11:00:05Z", [
            ("Bash", {"command": "make deploy", "description": "Deploy"})])))
    A.run_angles(cwd="", session_id=SID, angles=["commands"], no_probes=True)
    after = _store()
    assert _angles_of(after) == {"commands"}   # old turn's files NOT carried
    assert after["items"]["X1"]["headline"] == "Deploy"
