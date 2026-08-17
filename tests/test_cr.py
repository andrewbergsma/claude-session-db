"""Unit tests for the CR (context-reduction) engine (claude_session_db/cr.py).

Semantics under test:
  - manifest grouping + deterministic defaults (recency keeps, dedup wins,
    kmcp reads default to → ref, thinking is locked)
  - stub-both-copies correctness (message.content block AND toolUseResult
    mirror), and NO extra keys inside content blocks (the API rejects them:
    "tool_result._cr: Extra inputs are not permitted" — found by smoke test)
  - version guard: unknown record versions refuse the forge
  - preamble record shape (synthetic user record at the fork tip,
    parentUuid = last kept main-chain uuid)
  - refs degrade to plain pointers when kmcp is down (never block)
  - forge writes a NEW file, never mutates the source; sidechains dropped

Run:  uv run --extra dev pytest tests/test_cr.py -q
"""
import json

import pytest

from claude_session_db import cr

VER = "2.1.233"


def _user(uuid, text, parent=None, **kw):
    return {"type": "user", "uuid": uuid, "parentUuid": parent,
            "sessionId": "src-sid", "timestamp": "2026-08-14T10:00:00Z",
            "version": VER, "cwd": "/tmp/proj", "gitBranch": "main",
            "message": {"role": "user", "content": text}, **kw}


def _result(uuid, tool_id, content, parent=None, mirror=True, **kw):
    r = {"type": "user", "uuid": uuid, "parentUuid": parent,
         "sessionId": "src-sid", "timestamp": "2026-08-14T10:00:02Z",
         "version": VER,
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": tool_id,
              "content": content}]}, **kw}
    if mirror:
        r["toolUseResult"] = content
    return r


def _assistant(uuid, text, tool_uses=(), parent=None, thinking=None):
    content = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking,
                        "signature": "sig-must-survive"})
    if text:
        content.append({"type": "text", "text": text})
    for tid, name, inp in tool_uses:
        content.append({"type": "tool_use", "id": tid, "name": name,
                        "input": inp})
    return {"type": "assistant", "uuid": uuid, "parentUuid": parent,
            "sessionId": "src-sid", "timestamp": "2026-08-14T10:00:01Z",
            "version": VER,
            "message": {"role": "assistant", "content": content}}


def _transcript():
    """2 turns: turn 1 has a big Bash result + a kmcp read + an injection,
    turn 2 (recent) has a duplicate of the big result."""
    big = "x" * 9000
    return [
        _user("u1", "please fix the bug"),
        _user("s1", "<command-name>/session-summary</command-name> blah",
              parent="u1"),
        _assistant("a1", "on it", parent="s1", thinking="let me think",
                   tool_uses=[
                       ("t1", "Bash", {"command": "pytest -q"}),
                       ("t2", "mcp__knowledge__get_entry",
                        {"application": "claudecode", "path": "lesson/x"}),
                   ]),
        _result("r1", "t1", big, parent="a1"),
        _result("r2", "t2", "entry body " * 50, parent="r1"),
        _user("u2", "now deploy it", parent="r2"),
        _assistant("a2", "deploying", parent="u2",
                   tool_uses=[("t3", "Bash", {"command": "make deploy"})]),
        _result("r3", "t3", big, parent="a2"),   # sha1-duplicate of r1
        {"type": "file-history-snapshot", "messageId": "m1"},
    ]


# ---- manifest ---------------------------------------------------------------
def test_manifest_grouping_and_defaults():
    m = cr.build_manifest(_transcript())
    assert m["version_ok"] and m["unsupported_versions"] == []
    by = {r["id"]: r for r in m["rows"]}

    assert by["u:u1"]["kind"] == "prompt" and by["u:u1"]["default"] == "keep"
    assert by["s:s1"]["kind"] == "injection"
    assert by["th:a1"]["locked"] and by["th:a1"]["default"] == "keep"
    assert by["a:a1"]["kind"] == "narration" and by["a:a1"]["default"] == "keep"
    assert by["t:t1"]["kind"] == "result" and by["t:t1"]["name"] == "Bash"
    assert by["t:t1"]["hint"] == "pytest -q"
    assert by["t:t2"]["kind"] == "kmcp"
    assert by["t:t2"]["refs"] == ["claudecode:lesson/x"]

    # 2 turns, both within the last-6 window → recency keeps everything
    # except the sha1-duplicate (dedup wins regardless of recency).
    assert by["t:t1"]["default"] == "keep"
    assert by["t:t2"]["default"] == "keep"
    assert by["t:t3"]["dup"] and by["t:t3"]["dup_of"] == "t:t1"
    assert by["t:t3"]["default"] == "stub"

    g = m["groups"]
    assert g["result"]["count"] == 2 and g["kmcp"]["count"] == 1
    assert g["prompt"]["count"] == 2 and g["injection"]["count"] == 1
    assert m["turns"] == 2
    assert m["floor"]["low"] == 70_000        # honest-AFTER scaffolding band
    # tool_use inputs are counted as fixed (non-redactable) overhead
    assert m["fixed_chars"] > 0


def test_manifest_recency_window_expires():
    """With >6 turns, turn-1 heavy blocks default to stub / kmcp to ref."""
    recs = _transcript()[:-1]
    for i in range(3, 10):     # add 7 trivial turns → turn 1 leaves the window
        recs.append(_user(f"u{i}", f"turn {i}"))
        recs.append(_assistant(f"a{i}", "ack"))
    m = cr.build_manifest(recs)
    by = {r["id"]: r for r in m["rows"]}
    assert by["t:t1"]["default"] == "stub"
    assert by["t:t2"]["default"] == "ref"     # kmcp read → travel as a ref
    assert by["s:s1"]["default"] == "stub"
    assert by["u:u1"]["default"] == "keep"    # prompts always pre-keep


def test_manifest_bash_kmcp_hook():
    """A knowledge-cli Bash shim classifies as a kmcp row via the hook."""
    def shim(inp):
        if "knowledge-cli" in (inp.get("command") or ""):
            return ("get_entry", {"application": "claudecode",
                                  "path": "lesson/shim"})
        return None
    recs = [
        _user("u1", "hi"),
        _assistant("a1", "reading", parent="u1", tool_uses=[
            ("t1", "Bash", {"command": "knowledge-cli call get_entry -"})]),
        _result("r1", "t1", "shim body", parent="a1"),
    ]
    by = {r["id"]: r for r in cr.build_manifest(recs, bash_kmcp=shim)["rows"]}
    assert by["t:t1"]["kind"] == "kmcp"
    assert by["t:t1"]["refs"] == ["claudecode:lesson/shim"]


# ---- stubbing ---------------------------------------------------------------
def test_stub_both_copies_no_extra_block_keys():
    recs = _transcript()
    m = cr.build_manifest(recs)
    res = cr.apply_stubs(recs, m, ["t:t1", "s:s1", "a:a1"])
    assert set(res["stubbed"]) == {"t:t1", "s:s1", "a:a1"}
    assert res["saved_chars"] > 0

    r1 = next(r for r in recs if r.get("uuid") == "r1")
    block = r1["message"]["content"][0]
    assert block["content"].startswith("[CR: Bash pytest -q — ")
    assert block["content"].endswith("elided]")
    assert r1["toolUseResult"] == block["content"]      # both copies
    # NO extra keys inside the block — the API rejects them outright.
    assert set(block) == {"type", "tool_use_id", "content"}

    s1 = next(r for r in recs if r.get("uuid") == "s1")
    assert s1["message"]["content"].startswith("[CR: injected context — ")

    a1 = next(r for r in recs if r.get("uuid") == "a1")
    blocks = a1["message"]["content"]
    # thinking untouched (signature intact), text swapped, block count same
    assert blocks[0]["thinking"] == "let me think"
    assert blocks[0]["signature"] == "sig-must-survive"
    assert blocks[1]["text"].startswith("[CR: assistant narration — ")
    assert set(blocks[1]) == {"type", "text"}
    assert len(blocks) == 4


def test_stub_locked_and_unknown_ignored():
    recs = _transcript()
    m = cr.build_manifest(recs)
    res = cr.apply_stubs(recs, m, ["th:a1", "nope:zz"])
    assert res["stubbed"] == []
    assert set(res["ignored"]) == {"th:a1", "nope:zz"}
    a1 = next(r for r in recs if r.get("uuid") == "a1")
    assert a1["message"]["content"][0]["thinking"] == "let me think"


def test_structure_preserved_after_stub():
    recs = _transcript()
    before = [(r.get("uuid"), r.get("parentUuid"), r.get("type"))
              for r in recs]
    m = cr.build_manifest(recs)
    cr.apply_stubs(recs, m, [r["id"] for r in m["rows"] if not r["locked"]])
    after = [(r.get("uuid"), r.get("parentUuid"), r.get("type"))
             for r in recs]
    assert before == after      # uuid/parentUuid DAG + record order intact


# ---- version guard ----------------------------------------------------------
def test_version_guard_refuses_unknown(tmp_path):
    recs = _transcript()
    recs[0]["version"] = "3.0.1"
    src = tmp_path / "src-sid.jsonl"
    src.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    assert cr.unsupported_versions(recs) == ["3.0.1"]
    with pytest.raises(cr.CRUnsupported):
        cr.forge_fork(src, [])
    assert list(tmp_path.glob("*.jsonl")) == [src]      # nothing forged


def test_version_none_is_fine():
    assert cr.unsupported_versions([{"type": "mode"}, {"version": "1.0.44"},
                                    {"version": "2.1.233"}]) == []


# ---- preamble ---------------------------------------------------------------
def test_preamble_refs_degrade_when_kmcp_down():
    txt = cr.render_preamble(["app:lesson/a", "app:design/b"],
                             entries=None, error="kmcp unreachable")
    assert "kmcp unreachable" in txt
    assert "- app:lesson/a" in txt and "- app:design/b" in txt
    assert txt.startswith("[CR context preamble")


def test_preamble_hydrated_entries():
    entries = [
        {"title": "Lesson A", "description": "why A", "content": "body A"},
        {"error": "not found"},
    ]
    txt = cr.render_preamble(["app:lesson/a", "app:lesson/b"], entries=entries)
    assert "## app:lesson/a" in txt
    assert "Lesson A — why A" in txt and "body A" in txt
    assert "- app:lesson/b  (unresolved: not found)" in txt


def test_preamble_big_body_falls_back_to_summary():
    entries = [{"title": "Big", "summary": "the gist",
                "content": "z" * (cr.BODY_INLINE_MAX + 10)}]
    txt = cr.render_preamble(["app:design/big"], entries=entries)
    assert "the gist" in txt
    assert "z" * 200 not in txt


# ---- forge ------------------------------------------------------------------
def test_forge_fork_shape(tmp_path):
    recs = _transcript()
    recs.insert(3, {"type": "user", "uuid": "sc1", "isSidechain": True,
                    "sessionId": "src-sid",
                    "message": {"role": "user", "content": "sidechain seed"}})
    src = tmp_path / "src-sid.jsonl"
    src.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    src_bytes = src.read_bytes()

    res = cr.forge_fork(src, ["t:t1"], preamble_text="PREAMBLE TEXT",
                        new_id="cafebabe-0000-0000-0000-000000000001")
    assert res["new_session"] == "cafebabe-0000-0000-0000-000000000001"
    dst = tmp_path / f"{res['new_session']}.jsonl"
    assert dst.exists()
    assert src.read_bytes() == src_bytes            # source never mutated

    out = [json.loads(l) for l in dst.read_text().splitlines() if l.strip()]
    assert all(r.get("sessionId") in (None, res["new_session"]) for r in out)
    assert not any(r.get("isSidechain") for r in out
                   if r.get("type") in ("user", "assistant")
                   and r.get("uuid") == "sc1")      # sidechain dropped
    # tip: synthetic preamble user record, then the custom-title
    pre, title = out[-2], out[-1]
    assert pre["type"] == "user"
    assert pre["parentUuid"] == "r3"                # last kept main-chain uuid
    assert pre["message"]["content"] == [
        {"type": "text", "text": "PREAMBLE TEXT"}]
    assert pre["uuid"] and pre["sessionId"] == res["new_session"]
    assert title["type"] == "custom-title"
    assert title["customTitle"].startswith("CR fork of src-sid"[:16])
    # the stub landed in the copy
    r1 = next(r for r in out if r.get("uuid") == "r1")
    assert r1["message"]["content"][0]["content"].startswith("[CR:")
    assert res["before_tokens"] > res["after_tokens"]


def test_forge_no_preamble_no_synthetic_user(tmp_path):
    recs = _transcript()
    src = tmp_path / "src-sid.jsonl"
    src.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    res = cr.forge_fork(src, [])
    out = [json.loads(l) for l in
           (tmp_path / f"{res['new_session']}.jsonl").read_text().splitlines()
           if l.strip()]
    assert out[-1]["type"] == "custom-title"
    # no synthetic user record: the tip below the title is the source's own
    # last record (the file-history-snapshot), not a CR-appended user turn
    assert out[-2]["type"] == "file-history-snapshot"
