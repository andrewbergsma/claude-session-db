"""Unit tests for the console's CR endpoints (console/server.py cr_* funcs).

Under test:
  - cr_manifest_payload: manifest over a discovered transcript, bash-shim
    kmcp reads classified via _bash_kmcp
  - cr_apply two-phase: preview writes NOTHING; confirm forges a new file in
    the same project dir under a server-minted uuid; original untouched
  - refs degrade when kmcp is down (cr_hydrate/cr_search never raise) — the
    preview/fork still succeed with plain-pointer preambles and a visible
    hydrate_error
  - cr_search surfaces {error} instead of blocking

Run:  uv run --extra dev pytest tests/test_cr_endpoints.py -q
"""
import json

import pytest

from claude_session_db import cr
from claude_session_db.console import server

SID = "cccccccc-1111-2222-3333-444444444444"
VER = "2.1.233"


def _recs():
    big = "y" * 8000
    return [
        {"type": "user", "uuid": "u1", "sessionId": SID, "version": VER,
         "cwd": "/tmp/proj", "timestamp": "2026-08-14T10:00:00Z",
         "message": {"role": "user", "content": "dig into the bug"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
         "sessionId": SID, "version": VER, "timestamp": "2026-08-14T10:00:01Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "reading the entry"},
             {"type": "tool_use", "id": "t1",
              "name": "mcp__knowledge__get_entry",
              "input": {"application": "claudecode", "path": "lesson/x"}},
             {"type": "tool_use", "id": "t2", "name": "Bash",
              "input": {"command": "pytest -q"}}]}},
        {"type": "user", "uuid": "r1", "parentUuid": "a1", "sessionId": SID,
         "version": VER, "timestamp": "2026-08-14T10:00:02Z",
         "toolUseResult": big,
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1",
              "content": "entry body " * 40}]}},
        {"type": "user", "uuid": "r2", "parentUuid": "r1", "sessionId": SID,
         "version": VER, "timestamp": "2026-08-14T10:00:03Z",
         "toolUseResult": big,
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t2", "content": big}]}},
    ]


@pytest.fixture
def env(tmp_path, monkeypatch):
    proj = tmp_path / "projects" / "-tmp-proj"
    proj.mkdir(parents=True)
    f = proj / f"{SID}.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in _recs()) + "\n")
    monkeypatch.setattr(server, "PROJECTS", tmp_path / "projects")
    return f


def _kmcp_down(tool, args):
    raise server.KmcpError(f"{tool}: knowledge-cli not found")


# ---- manifest ---------------------------------------------------------------
def test_manifest_payload(env):
    payload, code = server.cr_manifest_payload(SID)
    assert code == 200 and payload["session_id"] == SID
    by = {r["id"]: r for r in payload["rows"]}
    assert by["t:t1"]["kind"] == "kmcp"
    assert by["t:t1"]["refs"] == ["claudecode:lesson/x"]
    assert by["t:t2"]["kind"] == "result" and by["t:t2"]["name"] == "Bash"
    assert payload["floor"]["est"] == 85_000


def test_manifest_payload_reconciles_and_ships_the_new_kinds(env):
    """What the rail renders must add up to what its BEFORE line says."""
    payload, code = server.cr_manifest_payload(SID)
    assert code == 200
    assert payload["totals"]["est_tokens"] == sum(r["est_tokens"]
                                                  for r in payload["rows"])
    assert sum(g["est_tokens"] for g in payload["groups"].values()) \
        == payload["totals"]["est_tokens"]
    assert payload["residual_tokens"] == 0
    # additive fields the client needs; old names still parse
    assert set(payload["excluded"]) == {"signature_chars",
                                        "image_payload_chars",
                                        "json_envelope_chars"}
    assert payload["fixed_tokens"] == 0 and payload["surface_chars"] > 0
    by = {r["id"]: r for r in payload["rows"]}
    assert by["x:t1"]["kind"] == "tool_use"        # the kmcp read's INPUT
    assert by["x:t2"]["hint"] == "pytest -q"
    for r in payload["rows"]:                      # per-source addressability
        assert r["turn"] >= 1 and "bidx" in r


def test_preview_tokens_use_the_manifest_measure(env, monkeypatch):
    monkeypatch.setattr(server, "_kmcp_call", _kmcp_down)
    man, _ = server.cr_manifest_payload(SID)
    payload, code = server.cr_apply(SID, [], [], confirm=False)
    assert code == 200
    assert payload["before_tokens"] == man["totals"]["est_tokens"]
    assert payload["after_tokens"] == payload["before_tokens"]   # nothing stubbed


def test_manifest_missing_session():
    _, code = server.cr_manifest_payload("00000000-dead-beef-0000-000000000000")
    assert code == 404


# ---- two-phase --------------------------------------------------------------
def test_preview_writes_nothing(env, monkeypatch):
    monkeypatch.setattr(server, "_kmcp_call", _kmcp_down)
    before_files = set(env.parent.iterdir())
    payload, code = server.cr_apply(SID, ["t:t2"], ["claudecode:lesson/x"],
                                    confirm=False)
    assert code == 200 and payload["phase"] == "preview"
    assert payload["before_tokens"] > payload["after_tokens"]
    assert payload["stubbed"] == 1
    # kmcp down → preamble degrades to plain pointers, error visible
    assert payload["hydrated"] is False
    assert "knowledge-cli not found" in payload["hydrate_error"]
    assert "- claudecode:lesson/x" in payload["preamble"]
    assert set(env.parent.iterdir()) == before_files       # NOTHING written


def test_confirm_forges_new_file(env, monkeypatch):
    monkeypatch.setattr(server, "_kmcp_call", _kmcp_down)
    src_bytes = env.read_bytes()
    payload, code = server.cr_apply(SID, ["t:t2"], ["claudecode:lesson/x"],
                                    confirm=True)
    assert code == 200 and payload["phase"] == "forked"
    new_id = payload["new_session"]
    assert new_id != SID
    dst = env.parent / f"{new_id}.jsonl"
    assert dst.exists()
    assert env.read_bytes() == src_bytes                   # original untouched

    out = [json.loads(l) for l in dst.read_text().splitlines() if l.strip()]
    r2 = next(r for r in out if r.get("uuid") == "r2")
    assert r2["message"]["content"][0]["content"].startswith("[CR:")
    assert r2["toolUseResult"] == r2["message"]["content"][0]["content"]
    # preamble user record present with the degraded pointer
    pre = out[-2]
    assert pre["type"] == "user"
    txt = pre["message"]["content"][0]["text"]
    assert "- claudecode:lesson/x" in txt


def test_confirm_no_refs_no_preamble(env, monkeypatch):
    monkeypatch.setattr(server, "_kmcp_call", _kmcp_down)
    payload, _ = server.cr_apply(SID, [], [], confirm=True)
    out = [json.loads(l) for l in
           (env.parent / f"{payload['new_session']}.jsonl")
           .read_text().splitlines() if l.strip()]
    assert out[-1]["type"] == "custom-title"
    assert out[-2].get("uuid") == "r2"          # no synthetic preamble record


def test_version_guard_refuses(env):
    recs = _recs()
    recs[0]["version"] = "7.0.0"
    env.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    payload, code = server.cr_apply(SID, [], [], confirm=True)
    assert code == 409 and "7.0.0" in payload["error"]
    assert list(env.parent.glob("*.jsonl")) == [env]       # nothing forged


# ---- kmcp degrade -----------------------------------------------------------
def test_cr_search_degrades(monkeypatch):
    monkeypatch.setattr(server, "_kmcp_call", _kmcp_down)
    r = server.cr_search("context reduction", None)
    assert r["results"] == [] and "knowledge-cli not found" in r["error"]


def test_cr_search_normalizes(monkeypatch):
    monkeypatch.setattr(server, "_kmcp_call", lambda tool, args: {
        "results": [{"application": "claudecode", "path": "lesson/x",
                     "title": "X", "description": "d", "score": 0.9}]})
    r = server.cr_search("x", "claudecode")
    assert r["results"][0]["path"] == "lesson/x"
    assert "error" not in r


def test_cr_hydrate_batches_once(monkeypatch):
    calls = []

    def fake(tool, args):
        calls.append((tool, args))
        return {"entries": [{"title": "A"}, {"title": "B"}]}
    monkeypatch.setattr(server, "_kmcp_call", fake)
    ents, err = server.cr_hydrate(["app:lesson/a", "app:design/b"])
    assert err is None and [e["title"] for e in ents] == ["A", "B"]
    assert len(calls) == 1                       # ONE get_entries for the cart
    assert calls[0][0] == "get_entries"
    assert calls[0][1]["entries"] == [
        {"application": "app", "path": "lesson/a"},
        {"application": "app", "path": "design/b"}]


def test_cr_compile_document(monkeypatch):
    monkeypatch.setattr(server, "_kmcp_call", lambda t, a: {
        "entries": [{"title": "Playbook", "description": "how",
                     "content": "steps"}]})
    r = server.cr_compile(["claudecode:performance/context-reduction-playbook"])
    assert r["ok"] and r["hydrated"]
    assert "## claudecode:performance/context-reduction-playbook" in r["document"]
    assert "Playbook — how" in r["document"]
