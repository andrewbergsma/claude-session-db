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
import base64
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
    # tool_use inputs are ROWS now, never a hidden bucket
    assert m["fixed_chars"] == 0 and m["residual_tokens"] == 0
    assert by["x:t1"]["kind"] == "tool_use" and by["x:t1"]["name"] == "Bash"


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
    assert s1["message"]["content"].startswith("[CR: injected command /session-summary — ")

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


# ---- honest accounting: every source is a row, and the rows close -----------
def _png_b64(w, h, pad=800):
    """Bytes carrying a valid PNG/IHDR header — enough for the dimension
    sniffer, with no codec dependency."""
    raw = (b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
           + w.to_bytes(4, "big") + h.to_bytes(4, "big")
           + b"\x08\x06\x00\x00\x00" + b"\x00" * pad)
    return base64.b64encode(raw).decode()


def _image_block(w=1200, h=900, pad=40000):
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/png",
                       "data": _png_b64(w, h, pad)}}


def _rich_transcript():
    """The three formerly-invisible populations: a fat tool_use input, pasted
    images, and a thinking block whose signature dwarfs its text."""
    return [
        _user("u1", "here is the screenshot"),
        {"type": "user", "uuid": "u2", "parentUuid": "u1", "sessionId": "s",
         "version": VER, "timestamp": "2026-08-14T10:00:00Z",
         "message": {"role": "user", "content": [
             _image_block(1200, 900),
             {"type": "text", "text": "what is wrong with it?"}]}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u2",
         "sessionId": "s", "version": VER,
         "timestamp": "2026-08-14T10:00:01Z",
         "message": {"role": "assistant", "content": [
             {"type": "thinking", "thinking": "short",
              "signature": "S" * 4000},
             {"type": "text", "text": "writing the file"},
             {"type": "tool_use", "id": "t9", "name": "Write",
              "input": {"file_path": "/tmp/big.py",
                        "content": "print('x')\n" * 500}}]}},
        {"type": "user", "uuid": "r10", "parentUuid": "a1", "sessionId": "s",
         "version": VER, "timestamp": "2026-08-14T10:00:03Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t9", "content": [
                 {"type": "text", "text": "screenshot attached"},
                 _image_block(800, 600)]}]}},
    ]


def test_totals_equal_sum_of_rows_exactly():
    """THE reconciliation invariant — no bucket left to hide in."""
    for recs in (_transcript(), _rich_transcript()):
        m = cr.build_manifest(recs)
        assert m["totals"]["est_tokens"] == sum(r["est_tokens"]
                                                for r in m["rows"])
        assert m["totals"]["chars"] == sum(r["chars"] for r in m["rows"])
        assert m["residual_tokens"] == 0 and m["fixed_tokens"] == 0
        # the group headers sum to the same number the BEFORE line shows
        assert sum(g["est_tokens"] for g in m["groups"].values()) \
            == m["totals"]["est_tokens"]
        assert cr.surface_tokens(recs) == m["totals"]["est_tokens"]


def test_tool_use_rows_are_priced_by_their_json_input():
    m = cr.build_manifest(_rich_transcript())
    by = {r["id"]: r for r in m["rows"]}
    row = by["x:t9"]
    assert row["kind"] == "tool_use" and row["name"] == "Write"
    assert row["hint"] == "/tmp/big.py"
    inp = {"file_path": "/tmp/big.py", "content": "print('x')\n" * 500}
    assert row["chars"] == len(json.dumps(inp, ensure_ascii=False))
    assert row["est_tokens"] == row["chars"] // 4
    assert row["default"] == "keep"          # recent turn


def test_images_are_priced_as_images_not_base64():
    m = cr.build_manifest(_rich_transcript())
    imgs = [r for r in m["rows"] if r["kind"] == "image"]
    assert len(imgs) == 2                # user paste + tool_result sub-block
    by_dims = {r["dims"]: r for r in imgs}
    assert by_dims["1200x900"]["est_tokens"] == 1200 * 900 // 750
    assert by_dims["800x600"]["est_tokens"] == 800 * 600 // 750
    for r in imgs:
        # the base64 payload is REPORTED (chars) but never billed as tokens
        assert r["chars"] > 40000
        assert r["est_tokens"] < r["chars"] // 4
    assert m["excluded"]["image_payload_chars"] == sum(r["chars"] for r in imgs)


def test_huge_image_is_capped_and_unreadable_one_falls_back():
    big = {"type": "image", "source": {"type": "base64",
                                       "media_type": "image/png",
                                       "data": _png_b64(6000, 6000)}}
    assert cr.image_tokens(big)[0] == cr.IMAGE_MAX_TOKENS
    junk = {"type": "image", "source": {"type": "base64",
                                        "media_type": "image/heic",
                                        "data": "Z" * 400}}
    est, label, b64 = cr.image_tokens(junk)
    assert est == cr.IMAGE_FLAT_TOKENS and "unreadable" in label and b64 == 400


def test_thinking_signature_is_excluded_entirely():
    recs = _rich_transcript()
    m = cr.build_manifest(recs)
    by = {r["id"]: r for r in m["rows"]}
    assert by["th:a1"]["est_tokens"] == len("short") // 4
    assert m["excluded"]["signature_chars"] == 4000
    # lengthening the signature must not move a single token
    for r in recs:
        if r.get("uuid") == "a1":
            r["message"]["content"][0]["signature"] = "S" * 40000
    assert cr.build_manifest(recs)["totals"]["est_tokens"] \
        == m["totals"]["est_tokens"]


def test_other_blocks_are_a_visible_locked_row():
    recs = [_user("u1", "hi"),
            {"type": "assistant", "uuid": "a1", "sessionId": "s",
             "version": VER, "timestamp": "2026-08-14T10:00:01Z",
             "message": {"role": "assistant", "content": [
                 {"type": "server_tool_use", "id": "w1", "blob": "z" * 100}]}}]
    m = cr.build_manifest(recs)
    other = [r for r in m["rows"] if r["kind"] == "other"]
    assert len(other) == 1 and other[0]["locked"]
    assert m["totals"]["est_tokens"] == sum(r["est_tokens"] for r in m["rows"])


# ---- stubbing the new kinds -------------------------------------------------
def test_stub_tool_use_keeps_pairing_and_swaps_input():
    recs = _rich_transcript()
    m = cr.build_manifest(recs)
    res = cr.apply_stubs(recs, m, ["x:t9"])
    assert res["stubbed"] == ["x:t9"] and res["saved_chars"] > 0
    a1 = next(r for r in recs if r.get("uuid") == "a1")
    blk = a1["message"]["content"][2]
    assert set(blk) == {"type", "id", "name", "input"}   # pair still matches
    assert blk["id"] == "t9" and blk["name"] == "Write"
    assert blk["input"]["_cr_elided"].startswith("[CR: Write input /tmp/big.py")


def test_stub_image_becomes_a_text_block():
    recs = _rich_transcript()
    m = cr.build_manifest(recs)
    ids = [r["id"] for r in m["rows"] if r["kind"] == "image"]
    res = cr.apply_stubs(recs, m, ids)
    assert set(res["stubbed"]) == set(ids)
    u2 = next(r for r in recs if r.get("uuid") == "u2")
    assert u2["message"]["content"][0] == {
        "type": "text",
        "text": "[image removed by CR: 1200x900, ~1440 tokens]"}
    r10 = next(r for r in recs if r.get("uuid") == "r10")
    sub = r10["message"]["content"][0]["content"][1]
    assert sub["type"] == "text" and "800x600" in sub["text"]
    # no image block survives, and the count collapses accordingly
    assert not [r for r in cr.build_manifest(recs)["rows"]
                if r["kind"] == "image"]
    assert cr.surface_tokens(recs) < m["totals"]["est_tokens"]


def test_stub_image_inside_an_already_stubbed_result_is_not_ignored():
    recs = _rich_transcript()
    m = cr.build_manifest(recs)
    ids = [r["id"] for r in m["rows"]
           if r["kind"] == "image" or r["id"] == "t:t9"]
    assert cr.apply_stubs(recs, m, ids)["ignored"] == []


def test_forge_fork_with_tool_use_and_image_stays_well_formed(tmp_path):
    recs = _rich_transcript()
    src = tmp_path / "src-sid.jsonl"
    src.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    m = cr.build_manifest(recs)
    stub = [r["id"] for r in m["rows"]
            if r["kind"] in ("image", "tool_use", "result")]
    res = cr.forge_fork(src, stub)
    out = [json.loads(ln) for ln in
           (tmp_path / f"{res['new_session']}.jsonl").read_text().splitlines()
           if ln.strip()]

    uses, results = {}, set()
    for r in out:
        for b in ((r.get("message") or {}).get("content") or []):
            if not isinstance(b, dict):
                continue
            assert b.get("type") != "image", "no image block may survive"
            if b.get("type") == "tool_use":
                assert b.get("id") and b.get("name")
                uses[b["id"]] = b
            if b.get("type") == "tool_result":
                results.add(b.get("tool_use_id"))
    assert set(uses) == results               # every pair still matches
    assert list(uses["t9"]["input"]) == ["_cr_elided"]
    assert res["before_tokens"] > res["after_tokens"] > 0
    assert res["after_tokens"] == cr.surface_tokens(out)


# ---- injection labels -------------------------------------------------------
def test_injection_label_by_kind():
    L = cr.injection_label
    assert L("Base directory for this skill: /Users/x/.claude/skills/session-summary\n\n# Body") \
        == ("skill", "/session-summary")
    assert L("<task-notification>\n<task-id>abc</task-id>\n<status>completed</status>\n"
             "<summary>Agent \"Read up docingest\" finished</summary>\n<result>…</result>") \
        == ("task-notification", "completed · Agent \"Read up docingest\" finished")
    assert L("Another Claude session sent a message:\n<cross-session-message from=\"uds:/x\" "
             "from-name=\"docingest-b2\" from-mode=\"bypass\">\nTerritory note: hands off\nmore") \
        == ("cross-session", "from docingest-b2 · Territory note: hands off")
    assert L("<local-command-caveat>Caveat: …</local-command-caveat>") == ("local-command", "caveat")
    assert L("<local-command-stdout>\x1b[2mCompacted (ctrl+o)\x1b[0m\nmore</local-command-stdout>") \
        == ("local-command", "stdout · Compacted (ctrl+o)")
    assert L("<command-name>/compact</command-name>\n<command-message>compact</command-message>\n"
             "<command-args>focus on x</command-args>") == ("command", "/compact focus on x")
    assert L("<command-message>session-summary</command-message><command-args></command-args>") \
        == ("command", "/session-summary")
    assert L("<bash-input>csd angles</bash-input>") == ("bash", "! csd angles")
    assert L("<system-reminder>\nThe file was modified.\n</system-reminder>") \
        == ("system-reminder", "The file was modified.")
    assert L("[Image: source: /a.png][Image: source: /b.png]") == ("image-caption", "2 pasted images")
    assert L("something else entirely\nline 2") == (None, "something else entirely")


def test_injection_row_carries_label_and_breadcrumb():
    m = cr.build_manifest(_transcript())
    by = {r["id"]: r for r in m["rows"]}
    assert by["s:s1"]["name"] == "command"
    assert by["s:s1"]["hint"] == "/session-summary"
    assert cr._breadcrumb(by["s:s1"]).startswith("[CR: injected command /session-summary —")


# ---- billed context (usage) — the floor and the thinking the file cannot see
def _assistant_usage(uuid, mid, parent, inp, cr, cc, out, text="ok"):
    r = _assistant(uuid, text, parent=parent)
    r["message"]["id"] = mid
    r["message"]["usage"] = {"input_tokens": inp, "cache_read_input_tokens": cr,
                             "cache_creation_input_tokens": cc,
                             "output_tokens": out}
    return r


def _usage_transcript():
    return [
        _user("u1", "first question"),
        # turn 1: one API response split across two records (same message.id)
        _assistant_usage("a1", "m1", "u1", 100, 14_536, 17_632, 500),
        _assistant_usage("a1b", "m1", "a1", 100, 14_536, 17_632, 500),
        _user("u2", "second question", parent="a1b"),
        # turn 2: three calls; the last one is the billed context
        _assistant_usage("a2", "m2", "u2", 50, 40_000, 3_000, 9_000),
        _assistant_usage("a3", "m3", "a2", 50, 43_000, 12_000, 30_000),
        _assistant_usage("a4", "m4", "a3", 60, 55_000, 5_049, 800),
    ]


def test_billed_context_reads_usage_once_per_message_id():
    b = cr.billed_context(_usage_transcript())
    assert b["floor"] == 100 + 14_536 + 17_632           # turn-1 context
    assert b["ctx"] == 60 + 55_000 + 5_049                # the last call
    # last turn's earlier calls only (the final call's output is not in
    # anyone's input yet); turn 1 is not counted
    assert b["last_turn_output"] == 9_000 + 30_000
    assert b["calls"] == 4                                # m1 counted once


def test_billed_context_without_usage_is_empty():
    b = cr.billed_context(_transcript())
    assert b == {"ctx": None, "floor": None, "last_turn_output": 0, "calls": 0}


def test_manifest_floor_is_measured_when_usage_exists_else_band():
    m = cr.build_manifest(_usage_transcript())
    assert m["floor"]["source"] == "usage"
    assert m["floor"]["low"] == m["floor"]["high"] == m["floor"]["est"] == 32_268
    b = m["billed"]
    assert b["ctx"] == 60_109 and b["floor"] == 32_268
    assert b["reducible"] == m["totals"]["est_tokens"]
    assert b["last_turn_output"] == 39_000
    # the decomposition is exact by construction — the remainder is shown
    assert b["floor"] + b["reducible"] + b["last_turn_output"] + b["unexplained"] == b["ctx"]

    m0 = cr.build_manifest(_transcript())
    assert m0["floor"]["source"] == "band" and m0["floor"]["low"] == 70_000
    assert m0["billed"] is None


def test_forge_fork_reports_floor_cwd_and_resume_estimate(tmp_path):
    src = tmp_path / "src-sid.jsonl"
    cr.dump(_usage_transcript(), src)
    res = cr.forge_fork(src, [], new_id="fork-1")
    assert res["cwd"] == "/tmp/proj"
    assert res["floor"]["source"] == "usage" and res["floor"]["est"] == 32_268
    assert res["billed_before"] == 60_109
    assert res["resume_tokens"] == (res["floor"]["est"] + res["after_tokens"]
                                    - res["dropped_on_resume"])


# ─── Thinking: stored text vs signature-only, and what a resume bills ───────
def _thinking_transcript():
    recs = _usage_transcript()
    recs.append({"type": "assistant", "uuid": "th-a", "parentUuid": recs[-1]["uuid"],
                 "sessionId": "src-sid", "timestamp": "2026-09-08T00:10:00.000Z",
                 "cwd": "/tmp/proj",
                 "message": {"role": "assistant", "id": "msg-th",
                             "content": [
                                 {"type": "thinking", "thinking": "Plan: read the file first.",
                                  "signature": "s" * 400},
                                 {"type": "thinking", "thinking": "", "signature": "s" * 600},
                                 {"type": "redacted_thinking", "data": "r" * 300},
                                 {"type": "text", "text": "Reading."}]}})
    return recs


def test_thinking_rows_say_how_they_are_stored():
    m = cr.build_manifest(_thinking_transcript())
    th = [r for r in m["rows"] if r["kind"] == "thinking"]
    stored = {r["stored"] for r in th}
    assert stored == {"text", "signature", "redacted"}
    by = {r["stored"]: r for r in th}
    assert by["text"]["head"].startswith("Plan: read") and by["text"]["est_tokens"] > 0
    assert by["signature"]["est_tokens"] == 0 and "signature only" in by["signature"]["head"]
    assert by["redacted"]["est_tokens"] == 0 and "redacted" in by["redacted"]["head"]
    assert all(r["locked"] for r in th)
    # a redacted block is no longer an "other" row with its payload counted
    assert not [r for r in m["rows"] if r["kind"] == "other"]
    assert m["excluded"]["redacted_thinking_chars"] == 300
    assert m["excluded"]["signature_chars"] >= 1000


def test_manifest_thinking_census_and_resume_drops_text():
    m = cr.build_manifest(_thinking_transcript())
    th = m["thinking"]
    assert (th["blocks"], th["with_text"], th["signature_only"], th["redacted"]) == (3, 1, 1, 1)
    assert th["text_tokens"] == cr.est_tokens(len("Plan: read the file first."))
    r = cr.resume_estimate(32_000, m)
    assert r["dropped_on_resume"] == th["text_tokens"]
    assert r["resume_tokens"] == 32_000 + m["totals"]["est_tokens"] - th["text_tokens"]


def test_forge_fork_resume_drops_thinking_text(tmp_path):
    src = tmp_path / "src-sid.jsonl"
    cr.dump(_thinking_transcript(), src)
    res = cr.forge_fork(src, [], new_id="fork-th")
    assert res["dropped_on_resume"] > 0
    assert res["resume_tokens"] == (res["floor"]["est"] + res["after_tokens"]
                                    - res["dropped_on_resume"])


# ─── Row heads and bodies — what a keep/stub decision is ABOUT ──────────────
def test_every_row_carries_its_opening_words():
    m = cr.build_manifest(_rich_transcript())
    by = {r["id"]: r for r in m["rows"]}
    assert by["u:u1"]["head"] == "here is the screenshot"
    assert by["u:u2"]["head"] == "what is wrong with it?"
    assert by["a:a1"]["head"] == "writing the file"
    assert by["th:a1"]["head"] == "short"
    assert by["x:t9"]["head"].startswith('{"file_path": "/tmp/big.py"')
    assert by["t:t9"]["head"].startswith("screenshot attached")
    assert by["i:u2#0"]["head"] == "1200x900"
    assert all("head" in r for r in m["rows"])
    assert all(len(r["head"]) <= cr.HEAD_CHARS for r in m["rows"])


def test_head_collapses_whitespace_and_ansi():
    assert cr._head("  a\n\n  b\t c ") == "a b c"
    assert cr._head("\x1b[31mred\x1b[0m") == "red"
    assert cr._head("x" * 500).endswith("…")
    recs = _rich_transcript()
    recs[2]["message"]["content"][0]["thinking"] = ""
    th = next(r for r in cr.build_manifest(recs)["rows"] if r["id"] == "th:a1")
    assert th["head"].startswith("(signature only")
    assert len(cr._head("x" * 500)) == cr.HEAD_CHARS


def test_row_body_every_kind():
    recs = _rich_transcript()
    m = cr.build_manifest(recs)
    by = {r["id"]: r for r in m["rows"]}
    assert cr.row_body(recs, by["u:u1"])["text"] == "here is the screenshot"
    assert cr.row_body(recs, by["a:a1"])["text"] == "writing the file"
    assert cr.row_body(recs, by["th:a1"])["text"] == "short"
    tu = cr.row_body(recs, by["x:t9"])
    assert json.loads(tu["text"])["file_path"] == "/tmp/big.py"
    res = cr.row_body(recs, by["t:t9"])
    assert res["text"].startswith("screenshot attached")
    assert "[image" in res["text"]                # the sub-image is its own row
    img = cr.row_body(recs, by["i:u2#0"])
    assert img["image"]["media_type"] == "image/png"
    assert img["image"]["data"] == recs[1]["message"]["content"][0]["source"]["data"]
    sub = cr.row_body(recs, by["i:r10#0.1"])      # image INSIDE a tool_result
    assert sub["image"]["data"] == recs[3]["message"]["content"][0]["content"][1]["source"]["data"]


def test_row_body_reports_missing_not_guessed():
    recs = _rich_transcript()
    m = cr.build_manifest(recs)
    row = dict(next(r for r in m["rows"] if r["id"] == "t:t9"))
    row["tid"] = "nope"
    assert cr.row_body(recs, row) is None
    row = dict(next(r for r in m["rows"] if r["id"] == "u:u1"), uuid="ghost")
    assert cr.row_body(recs, row) is None
    assert cr.row_body(recs, None) is None
