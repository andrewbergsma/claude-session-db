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


# ---- fork fidelity: the stamp, the resume command, the composer's spawn ------
@pytest.fixture
def meta(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CONSOLE_STATE", tmp_path / "console")
    monkeypatch.setattr(server, "META_FILE", tmp_path / "console" / "meta.json")
    return tmp_path / "console" / "meta.json"


def test_confirm_stamps_fork_meta_and_prints_resume_cmd(env, meta, monkeypatch):
    monkeypatch.setattr(server, "_kmcp_call", _kmcp_down)
    payload, code = server.cr_apply(SID, ["t:t2"], [], confirm=True)
    assert code == 200 and payload["spawned"] is False
    new_id = payload["new_session"]
    assert payload["cwd"] == "/tmp/proj"
    assert payload["resume_cmd"] == f"cd /tmp/proj && claude --resume {new_id}"
    assert payload["resume_tokens"] == payload["floor"]["est"] + payload["after_tokens"]
    m = server._meta_of(server._read_meta_overlay(), new_id)
    assert m["cr_source"] == SID
    assert m["cr_after"] == payload["after_tokens"]
    assert m["cr_before"] == payload["before_tokens"]
    assert m["cr_floor"] == payload["floor"]["est"]
    assert m["cr_at"].endswith("Z")


def test_confirm_with_text_spawns_into_the_fork(env, meta, monkeypatch):
    monkeypatch.setattr(server, "_kmcp_call", _kmcp_down)
    calls = []
    monkeypatch.setattr(server, "spawn_claude",
                        lambda args, cwd, sid=None, **kw: calls.append((args, cwd, sid)))
    src_bytes = env.read_bytes()
    payload, code = server.cr_apply(SID, [], [], confirm=True,
                                    text="continue from here", cwd="/elsewhere")
    assert code == 200 and payload["spawned"] and payload["action"] == "cr-fork-answer"
    new_id = payload["new_session"]
    (args, cwd, sid), = calls
    assert args == ["-p", "--resume", new_id, "continue from here"]
    assert cwd == "/tmp/proj"          # the transcript's cwd wins over the body's
    assert sid == new_id               # registered under the FORK, so Stop aims right
    assert env.read_bytes() == src_bytes


def test_confirm_spawn_failure_keeps_the_fork(env, meta, monkeypatch):
    monkeypatch.setattr(server, "_kmcp_call", _kmcp_down)
    def boom(*a, **k):
        raise FileNotFoundError("`claude` binary not found")
    monkeypatch.setattr(server, "spawn_claude", boom)
    payload, code = server.cr_apply(SID, [], [], confirm=True, text="go")
    assert code == 200 and payload["spawned"] is False
    assert "not found" in payload["spawn_error"]
    assert (env.parent / f"{payload['new_session']}.jsonl").exists()


def test_confirm_without_text_never_spawns(env, meta, monkeypatch):
    monkeypatch.setattr(server, "_kmcp_call", _kmcp_down)
    monkeypatch.setattr(server, "spawn_claude",
                        lambda *a, **k: pytest.fail("spawned without text"))
    payload, _ = server.cr_apply(SID, [], [], confirm=True, text="   ")
    assert payload["spawned"] is False


def test_preview_carries_floor_billed_and_cwd(env, monkeypatch):
    monkeypatch.setattr(server, "_kmcp_call", _kmcp_down)
    payload, _ = server.cr_apply(SID, [], [], confirm=False)
    assert payload["floor"]["source"] == "band"     # fixture has no usage
    assert payload["billed"] is None
    assert payload["cwd"] == "/tmp/proj"
    assert payload["resume_tokens"] == payload["floor"]["est"] + payload["after_tokens"]


def test_cr_overlay_estimates_until_a_post_fork_turn_is_billed():
    m = {"cr_source": SID, "cr_before": 98_000, "cr_after": 44_000,
         "cr_floor": 32_000, "cr_billed": 230_000,
         "cr_at": "2026-09-08T10:00:00.000Z"}
    # fresh fork: the copied usage block predates the stamp → estimate
    s = {"ctx_tokens": 230_000}
    server._cr_overlay(s, m, "2026-09-07T22:00:00.000Z")
    assert s["ctx_tokens"] == 76_000 and s["ctx_est"] == "cr"
    assert s["cr"]["measured"] is False and s["cr"]["source"] == SID
    # no usage at all → still the estimate
    s = {"ctx_tokens": None}
    server._cr_overlay(s, m, None)
    assert s["ctx_tokens"] == 76_000
    # first own turn billed after the stamp → measured wins, estimate gone
    s = {"ctx_tokens": 81_500}
    server._cr_overlay(s, m, "2026-09-08T10:05:00.000Z")
    assert s["ctx_tokens"] == 81_500 and "ctx_est" not in s
    assert s["cr"]["measured"] is True
    # not a CR fork → untouched
    s = {"ctx_tokens": 5}
    server._cr_overlay(s, {}, "2026-09-08T10:05:00.000Z")
    assert s == {"ctx_tokens": 5}


def test_resume_cmd_quotes_an_awkward_cwd():
    cmd = server.cr_resume_cmd("/Users/andrew/My Repo", "abc")
    assert cmd == "cd '/Users/andrew/My Repo' && claude --resume abc"
    assert server.cr_resume_cmd(None, "abc").startswith("claude --resume abc")


# ---- row bodies (GET /api/cr/row) -------------------------------------------
def test_row_payload_returns_the_full_content(env):
    server._CR_MEMO.clear()
    body, code = server.cr_row_payload(SID, "t:t2")
    assert code == 200 and body["kind"] == "result"
    assert body["text"] == "y" * 8000
    body, code = server.cr_row_payload(SID, "u:u1")
    assert code == 200 and body["text"] == "dig into the bug"
    body, code = server.cr_row_payload(SID, "x:t1")
    assert code == 200 and json.loads(body["text"])["path"] == "lesson/x"


def test_row_payload_404_for_unknown_row_or_session(env):
    server._CR_MEMO.clear()
    assert server.cr_row_payload(SID, "t:nope")[1] == 404
    assert server.cr_row_payload("00000000-0000-0000-0000-000000000000",
                                 "u:u1")[1] == 404


def test_manifest_rows_ship_heads_and_memo_follows_the_file(env):
    server._CR_MEMO.clear()
    payload, _ = server.cr_manifest_payload(SID)
    by = {r["id"]: r for r in payload["rows"]}
    assert by["u:u1"]["head"] == "dig into the bug"
    assert by["a:a1"]["head"] == "reading the entry"
    assert len(server._CR_MEMO) == 1
    # the memo is keyed on (mtime_ns, size): a changed transcript rebuilds
    recs = _recs()
    recs[0]["message"]["content"] = "dig into the OTHER bug"
    env.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    payload, _ = server.cr_manifest_payload(SID)
    assert {r["id"]: r for r in payload["rows"]}["u:u1"]["head"] \
        == "dig into the OTHER bug"
