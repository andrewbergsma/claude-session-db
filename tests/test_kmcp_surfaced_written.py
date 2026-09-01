"""The Context tab's two new kmcp ledgers: SURFACED and WRITTEN.

Before this, a tool_use whose kmcp base was neither a READ_TOOL nor a
SURFACE_TOOL fell through every branch of `build_session`'s extraction loop and
was DROPPED — the generic-tool branch is gated on `base is None`, which a
resolved kmcp base never is. Every `import_entries` / `patch_content` /
`create_relationship` a session ever made was invisible to the console.

These tests pin the two things that fixes:

  * `kind: "search"` results now yield REFS (both the JSON payload and the
    compact TEXT rendering kmcp returns at detail=minimal, which is not JSON
    and used to surface nothing at all), so the client can mark each one
    consumed ✓ / not.
  * `kind: "write"` events exist at all, carrying tool/op/app/path/refs/
    dry_run/is_error, with the result JSON's created-vs-updated as the
    authority and a refused write ({error, message}, is_error unset) read as
    an error rather than as a successful create.

Run:  uv run --extra dev pytest tests/test_kmcp_surfaced_written.py -q
"""
import json

import pytest

from claude_session_db.console import server as S


# ---- transcript builder -----------------------------------------------------
def _assistant(uuid, blocks):
    return {"type": "assistant", "uuid": uuid, "timestamp": "2026-09-01T10:00:00Z",
            "cwd": "/tmp/proj", "gitBranch": "main",
            "message": {"model": "claude-opus-5", "content": blocks,
                        "usage": {"input_tokens": 10}}}


def _result(uuid, tid, text, is_error=False):
    return {"type": "user", "uuid": uuid, "timestamp": "2026-09-01T10:00:01Z",
            "message": {"content": [{"type": "tool_result", "tool_use_id": tid,
                                     "is_error": is_error,
                                     "content": [{"type": "text", "text": text}]}]}}


def _use(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


SEARCH_JSON = json.dumps({
    "query": "envelope", "total": 42,
    "type_counts": {"knowledge": 2, "lesson": 1},
    "results": [
        {"application": "csd", "path": "design/envelope", "title": "Envelope",
         "entity_type": "knowledge", "score": 24.0},
        {"application": "csd", "path": "lesson/never-block", "title": "Never block",
         "entity_type": "lesson", "score": 12.0},
    ]})

# The compact rendering kmcp returns at detail=minimal — NOT json.
SEARCH_TEXT = (
    "agent delivery loop worktree PR ship merge · 1,935 hits · app=csd\n"
    "types: 845 event · 447 task\n"
    "\n"
    "  process  process/agent-delivery                    Agent Delivery Loop — …\n"
    "  lesson   other_app:lesson/git/squash-merge         Squash-merged branches …\n"
    "\n"
    "  +1930 more — mostly event(845)/task(447).\n")

IMPORT_DOC = json.dumps({
    "application": "csd", "path": "design/envelope", "entity_type": "knowledge",
    "title": "Envelope", "description": "d", "content": {"content": "body"}})

IMPORT_DRY_RESULT = json.dumps({
    "created": [{"entry": 1, "path": "design/envelope", "entity_type": "knowledge",
                 "would_create": True, "would_update": False}],
    "skipped": [], "errors": [], "dry_run": True,
    "summary": {"total": 1, "created": 1, "skipped": 0, "errors": 0}})

IMPORT_REAL_RESULT = json.dumps({
    "created": [{"entry": 1, "path": "design/envelope", "entity_type": "knowledge",
                 "id": "abc", "updated": False}],
    "skipped": [], "errors": [], "dry_run": False,
    "summary": {"total": 1, "created": 1, "skipped": 0, "errors": 0}})

CLI_WRITE = ("KNOWLEDGE_ALLOW_UNAUTH_LOCAL=1 knowledge-cli call patch_content "
             "'{\"application\":\"csd\",\"path\":\"task/open-items\","
             "\"section\":\"status\",\"operation\":\"set\",\"value\":\"done\"}'")


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A synthetic transcript exercising every new extraction branch."""
    recs = [
        {"type": "user", "uuid": "u0", "timestamp": "2026-09-01T09:59:00Z",
         "cwd": "/tmp/proj",
         "message": {"content": [{"type": "text", "text": "do the thing"}]}},
        _assistant("a1", [_use("t1", "mcp__knowledge__hybrid_search",
                               {"query": "envelope", "application": "csd"})]),
        _result("r1", "t1", SEARCH_JSON),
        _assistant("a2", [_use("t2", "mcp__knowledge__search",
                               {"query": "agent delivery loop"})]),
        _result("r2", "t2", SEARCH_TEXT),
        # consumed: one of the refs the first search surfaced
        _assistant("a3", [_use("t3", "mcp__knowledge__get_entry",
                               {"application": "csd", "path": "design/envelope"})]),
        _result("r3", "t3", "body"),
        # dry_run, then the real write of the same document
        _assistant("a4", [_use("t4", "mcp__knowledge__import_entries",
                               {"content": IMPORT_DOC, "dry_run": True})]),
        _result("r4", "t4", IMPORT_DRY_RESULT),
        _assistant("a5", [_use("t5", "mcp__knowledge__import_entries",
                               {"content": IMPORT_DOC})]),
        _result("r5", "t5", IMPORT_REAL_RESULT),
        # a patch
        _assistant("a6", [_use("t6", "mcp__knowledge__patch_content",
                               {"application": "csd", "path": "task/open-items",
                                "section": "status", "operation": "set",
                                "change_summary": "close the item"})]),
        _result("r6", "t6", json.dumps({"updated": True})),
        # an update_entry that FAILED
        _assistant("a7", [_use("t7", "mcp__knowledge__update_entry",
                               {"application": "csd", "path": "design/envelope",
                                "change_summary": "retitle"})]),
        _result("r7", "t7", "version conflict: entry changed since snapshot",
                is_error=True),
        # the knowledge-cli Bash shim, counted like the MCP surface
        _assistant("a8", [_use("t8", "Bash", {"command": CLI_WRITE})]),
        _result("r8", "t8", json.dumps({"updated": True})),
        # a malformed import: unreadable document, still gets a row
        _assistant("a9", [_use("t9", "mcp__knowledge__import_entries",
                               {"file_path": "/nope/entries.json"})]),
        _result("r9", "t9", json.dumps({"error": "Import path not allowed",
                                        "message": "outside the staging dir"})),
    ]
    proj = tmp_path / "-tmp-proj"
    proj.mkdir()
    sid = "11111111-2222-3333-4444-555555555555"
    (proj / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in recs) + "\n")
    monkeypatch.setattr(S, "PROJECTS", tmp_path)
    return S.build_session(sid)


def _by_kind(payload, kind):
    return [e for e in payload["events"] if e["kind"] == kind]


# ---- counts + payload compatibility -----------------------------------------
def test_counts_carry_writes(session):
    assert session["counts"]["reads"] == 1
    assert session["counts"]["searches"] == 2
    assert session["counts"]["writes"] == 6

def test_existing_event_kinds_survive(session):
    kinds = {e["kind"] for e in session["events"]}
    assert {"user", "read", "search", "write"} <= kinds
    # the assistant turns here are tool-only, so no assistant text events —
    # what matters is that no existing kind lost its shape
    for e in _by_kind(session, "read"):
        assert {"tool", "app", "path", "mode", "chars", "tid"} <= set(e)


# ---- surfaced ---------------------------------------------------------------
def test_json_search_yields_refs(session):
    e = _by_kind(session, "search")[0]
    hits = e["result"]["hits"]
    assert e["result"]["total"] == 42
    assert [(h["app"], h["path"], h["etype"]) for h in hits] == [
        ("csd", "design/envelope", "knowledge"),
        ("csd", "lesson/never-block", "lesson")]

def test_text_search_yields_refs(session):
    """The compact TEXT rendering is not JSON — it used to surface nothing."""
    e = _by_kind(session, "search")[1]
    r = e["result"]
    assert r["total"] == 1935
    assert r["type_counts"] == {"event": 845, "task": 447}
    assert [(h["app"], h["path"]) for h in r["hits"]] == [
        ("csd", "process/agent-delivery"),
        ("other_app", "lesson/git/squash-merge")]
    assert all(not h["path"].startswith("+") for h in r["hits"])

def test_text_search_titles_and_types(session):
    h = _by_kind(session, "search")[1]["result"]["hits"][0]
    assert h["etype"] == "process"
    assert h["title"].startswith("Agent Delivery Loop")

def test_search_result_shapes_that_are_not_search_results():
    assert S._parse_search_result(None) is None
    assert S._parse_search_result("Claude requested permissions…") is None
    assert S._parse_search_result("{not json") is None
    assert S._parse_search_result(json.dumps({"ok": True}))["hits"] == []

def test_bare_list_result_is_accepted():
    r = S._parse_search_result(json.dumps(
        [{"application": "csd", "path": "a/b", "entity_type": "task"}]))
    assert r["hits"] == [{"app": "csd", "path": "a/b", "title": None,
                          "score": None, "etype": "task"}]

def test_relationship_result_uses_the_far_end():
    r = S._parse_search_result(json.dumps({"relationships": [
        {"source_path": "a/b", "target_path": "c/d",
         "target_application": "other", "weight": 0.5}]}))
    assert r["hits"][0]["app"] == "other"
    assert r["hits"][0]["path"] == "c/d"


# ---- written ----------------------------------------------------------------
def test_dry_run_then_real_write_are_distinct(session):
    dry, real = _by_kind(session, "write")[0], _by_kind(session, "write")[1]
    assert dry["tool"] == real["tool"] == "import_entries"
    assert dry["dry_run"] is True and real["dry_run"] is False
    for e in (dry, real):
        assert e["op"] == "created"
        assert e["refs"] == [{"app": "csd", "path": "design/envelope",
                              "etype": "knowledge", "op": "created"}]
        assert e["is_error"] is False

def test_patch_carries_section_and_change_summary(session):
    e = _by_kind(session, "write")[2]
    assert (e["tool"], e["op"]) == ("patch_content", "patched")
    assert (e["app"], e["path"]) == ("csd", "task/open-items")
    assert e["note"] == "close the item"

def test_failed_write_is_marked(session):
    e = _by_kind(session, "write")[3]
    assert e["tool"] == "update_entry" and e["op"] == "updated"
    assert e["is_error"] is True
    assert "version conflict" in e["error"]

def test_cli_shim_write_counts_as_a_write(session):
    e = _by_kind(session, "write")[4]
    assert (e["tool"], e["via"], e["op"]) == ("patch_content", "cli", "patched")
    assert (e["app"], e["path"]) == ("csd", "task/open-items")

def test_refused_write_reads_as_an_error_not_a_create(session):
    """{error, message} with is_error UNSET is a refusal, not a write."""
    e = _by_kind(session, "write")[5]
    assert e["is_error"] is True
    assert "Import path not allowed" in e["error"]
    # a staged-file import declares no entry ref — the row must not link a
    # filesystem path into /browse as if it were an entry path
    assert e["refs"] == [{"app": None, "path": None, "etype": None,
                          "op": "created"}]
    assert e["note"] == "file: /nope/entries.json"


# ---- unit: the parsers degrade, they never raise ----------------------------
def test_import_docs_reads_json_yaml_and_lists():
    assert S._import_docs({"content": IMPORT_DOC})[0]["path"] == "design/envelope"
    # `entries` passed as a JSON *string* (a real-world caller shape)
    multi = S._import_docs({"entries": json.dumps([
        {"application": "a", "path": "p/1"}, {"application": "a", "path": "p/2"}])})
    assert [d["path"] for d in multi] == ["p/1", "p/2"]
    # YAML, scraped without a parser
    y = S._import_docs({"content": "application: csd\npath: lesson/x\n"
                                   "entity_type: lesson\ncontent: |\n  body\n"})
    assert y == [{"app": "csd", "path": "lesson/x", "etype": "lesson",
                  "title": None}]

def test_import_docs_never_raises_on_junk():
    for junk in ({}, {"content": 7}, {"content": "«»"}, {"entries": [1, 2]},
                 {"content": "[]"}, None):
        assert S._import_docs(junk) == []

def test_parse_write_result_tolerates_scalar_keys():
    """An update_entry result carries `updated: true` — a bool, not a list."""
    r = S._parse_write_result(json.dumps({"updated": True}))
    assert r["created"] == [] and r["updated"] == [] and r["errors"] == []

def test_parse_write_result_maps_updates():
    r = S._parse_write_result(json.dumps({
        "created": [{"path": "a/b", "updated": True}], "dry_run": False}))
    assert r["created"][0]["updated"] is True

def test_write_meta_relationship_and_rename():
    op, refs, note = S._write_meta("create_relationship", {
        "application": "csd", "source_path": "a/b", "target_path": "c/d",
        "relationship_type": "see_also"}, None)
    assert op == "related" and refs[0]["path"] == "a/b"
    assert note == "see_also → csd:c/d"
    op, refs, note = S._write_meta("move_entry", {
        "old_application": "csd", "old_path": "a/b",
        "new_application": "other", "new_path": "c/d"}, None)
    assert op == "moved" and refs[0]["app"] == "csd"
    assert note == "→ other:c/d"

def test_write_meta_always_returns_a_row():
    for base in sorted(S.WRITE_TOOLS):
        op, refs, _ = S._write_meta(base, {}, None)
        assert op and len(refs) == 1        # a malformed input still gets a row

def test_write_tools_are_seeded_from_the_angles_extractor():
    from claude_session_db.angles import _WRITE_TOOLS
    assert set(_WRITE_TOOLS) <= S.WRITE_TOOLS
    assert not (S.WRITE_TOOLS & S.READ_TOOLS)
    assert not (S.WRITE_TOOLS & S.SURFACE_TOOLS)

def test_bash_shim_admits_writes():
    base, inp = S._bash_kmcp({"command": CLI_WRITE})
    assert base == "patch_content" and inp["path"] == "task/open-items"
    assert S._bash_kmcp({"command": "knowledge-cli call get_statistics"}) is None
    base, inp = S._bash_kmcp(
        {"command": "knowledge-cli call import_entries --application csd "
                    "--path design/x --dry-run"})
    assert base == "import_entries" and inp["dry_run"] is True
