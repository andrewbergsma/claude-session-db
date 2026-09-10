"""`csd digest --cr` / `--row` — the context-reduced cut /session-summary hands
a fresh subagent (claude_session_db/digest_cr.py).

Semantics under test:
  - the header's FIRST line is `SESSION DIGEST  ·  <bare uuid>   [cr]` — the
    summarizer reads the session_id off it character-for-character.
  - every row in the window produces output: an elided row is a one-line
    breadcrumb carrying its row id, never an absence (regression for the
    curated-digest prior art, which iterated kept rows only).
  - kmcp writes are one line each (op + target), incl. import_entries'
    per-document refs and create_relationship's `src -> tgt (type)`, with
    ` ✗ <error>` when the write failed.
  - is_error results are KEPT (head), Agent results are KEPT and capped.
  - Edit/Write hints are the FULL file path (CR's input_hint truncates at 60).
  - --since windows the records exactly like the plain digest.
  - --row prints one manifest row's full body; an unknown id exits 2.

Run:  pytest tests/test_digest_cr.py -q
"""
import json

import pytest
from click.testing import CliRunner

from claude_session_db import cli
from claude_session_db import digest_cr
from claude_session_db import session_mgmt as mgmt

SID = "bbbbbbbb-1111-2222-3333-444444444444"
VER = "2.1.233"
LONG_PATH = ("/Users/andrew/GitHub/some-very-long-repository-name/src/"
             "deeply/nested/package/module_with_a_long_name.py")


def _user(uuid, text, ts="2026-08-20T10:00:00Z", **kw):
    return {"type": "user", "uuid": uuid, "sessionId": SID, "timestamp": ts,
            "version": VER, "message": {"role": "user", "content": text}, **kw}


def _assistant(uuid, text, tool_uses=(), ts="2026-08-20T10:00:01Z",
               thinking=None):
    content = []
    if thinking is not None:
        content.append({"type": "thinking", "thinking": thinking,
                        "signature": "sig"})
    if text:
        content.append({"type": "text", "text": text})
    for tid, name, inp in tool_uses:
        content.append({"type": "tool_use", "id": tid, "name": name,
                        "input": inp})
    return {"type": "assistant", "uuid": uuid, "sessionId": SID,
            "timestamp": ts, "version": VER,
            "message": {"role": "assistant", "content": content}}


def _result(uuid, tid, content, ts="2026-08-20T10:00:02Z", is_error=False):
    b = {"type": "tool_result", "tool_use_id": tid, "content": content}
    if is_error:
        b["is_error"] = True
    return {"type": "user", "uuid": uuid, "sessionId": SID, "timestamp": ts,
            "version": VER, "message": {"role": "user", "content": [b]},
            "toolUseResult": content}


K = "mcp__claude_ai_kmcp__"
IMPORT_YAML = ("application: knowledge_mcp\npath: design/alpha\n"
               "entity_type: design\ntitle: A\n---\n"
               "application: knowledge_mcp_code\npath: task/beta\n"
               "entity_type: task\ntitle: B\n")
AGENT_REPORT = "R" * 5000


def _records():
    return [
        _user("u1", "please fix the bug and record it", ts="2026-08-20T10:00:00Z"),
        _user("s1", "Base directory for this skill: /Users/x/.claude/skills/"
                    "session-summary\n\n" + "skill body " * 300,
              ts="2026-08-20T10:00:00Z", isMeta=True),
        _assistant("a1", "on it", thinking="", tool_uses=[
            ("tb", "Bash", {"command": "git status"}),
            ("tk", K + "get_entry", {"application": "claudecode",
                                     "path": "lesson/x"}),
        ], ts="2026-08-20T10:00:01Z"),
        _result("r1", "tb", "o" * 2300, ts="2026-08-20T10:00:02Z"),
        _result("r2", "tk", "entry body " * 100, ts="2026-08-20T10:00:02Z"),
        _assistant("a2", "", tool_uses=[
            ("te", "Edit", {"file_path": LONG_PATH, "old_string": "a",
                            "new_string": "b"}),
            ("tw", "Write", {"file_path": "/tmp/new.py", "content": "x" * 900}),
        ], ts="2026-08-20T10:00:03Z"),
        _result("r3", "te", "ok", ts="2026-08-20T10:00:04Z"),
        _result("r4", "tw", "ok", ts="2026-08-20T10:00:04Z"),
        _assistant("a3", "", tool_uses=[
            ("tf", "Bash", {"command": "pytest -q"})], ts="2026-08-20T10:00:05Z"),
        _result("r5", "tf", "E" * 600 + " FAILED", ts="2026-08-20T10:00:06Z",
                is_error=True),
        # ── turn 2, after the --since cut ──
        _user("u2", "now record it in kmcp", ts="2026-08-21T15:00:00Z"),
        _assistant("a4", "writing", tool_uses=[
            ("ti", K + "import_entries", {"content": IMPORT_YAML}),
            ("tr", K + "create_relationship", {
                "application": "knowledge_mcp", "source_path": "design/alpha",
                "target_path": "task/beta", "target_application":
                    "knowledge_mcp_code", "relationship_type": "see_also"}),
            ("tp", K + "patch_content", {"application": "knowledge_mcp",
                                         "path": "design/alpha"}),
        ], ts="2026-08-21T15:00:01Z"),
        _result("r6", "ti", json.dumps({"created": ["design/alpha",
                                                    "task/beta"]}),
                ts="2026-08-21T15:00:02Z"),
        _result("r7", "tr", "ok", ts="2026-08-21T15:00:02Z"),
        _result("r8", "tp", "Content validation failed after patch",
                ts="2026-08-21T15:00:02Z", is_error=True),
        _assistant("a5", "", tool_uses=[
            ("ta", "Agent", {"subagent_type": "general-purpose",
                             "description": "survey the repo",
                             "prompt": "go look"})], ts="2026-08-21T15:00:03Z"),
        _result("r9", "ta", [{"type": "text", "text": AGENT_REPORT}],
                ts="2026-08-21T15:00:04Z"),
        {"type": "file-history-snapshot", "messageId": "m1"},
        # sidechain + compaction carrier: both dropped
        _user("sc", "SIDECHAIN SECRET", ts="2026-08-21T15:00:05Z",
              isSidechain=True),
        _user("cc", "This session is being continued from a previous "
                    "conversation. RESTATED HISTORY",
              ts="2026-08-21T15:00:06Z", isCompactSummary=True),
    ]


@pytest.fixture
def transcript(tmp_path, monkeypatch):
    proj = tmp_path / "-Users-andrew-GitHub-thing"
    proj.mkdir()
    p = proj / f"{SID}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in _records()) + "\n")
    monkeypatch.setattr(mgmt, "PROJECTS_DIR", tmp_path)
    return p


@pytest.fixture
def no_db(monkeypatch):
    def boom(explicit=None):
        raise RuntimeError("No database DSN found.")
    monkeypatch.setattr(cli, "resolve_dsn", boom)


def _render(p, **kw):
    text, stats = digest_cr.render_cr(p, session_id=SID, **kw)
    return text, stats


def _line(text, needle):
    hits = [ln for ln in text.splitlines() if needle in ln]
    assert hits, f"no line containing {needle!r}"
    return hits[0]


# ---- header -----------------------------------------------------------------
def test_header_first_line_is_bare_uuid_with_cr_tag(transcript):
    text, _ = _render(transcript)
    assert text.splitlines()[0] == f"SESSION DIGEST  ·  {SID}   [cr]"
    assert ".jsonl" not in text.splitlines()[0]
    assert "NOTE FOR THE SUMMARIZER" in text
    assert f"csd digest {SID} --row" in text
    assert "thinking: 1 blocks present, 0 with stored text" in text


# ---- elided rows still produce breadcrumbs (curated-digest regression) ------
def test_every_elided_row_leaves_a_breadcrumb(transcript):
    text, stats = _render(transcript)
    # the injected skill body is elided to ONE line naming it and its row id
    crumb = _line(text, "[s:s1 injected skill /session-summary")
    assert "elided]" in crumb and "skill body" not in text
    # a successful tool call + its result collapse into ONE line
    bash = _line(text, "[t:tb Bash git status")
    assert "in 0.0K / out 2.3K elided]" in bash
    assert "o" * 50 not in text
    # every tool id surfaces somewhere, nothing vanishes
    for tid in ("tb", "tk", "te", "tw", "tf", "ti", "tr", "tp", "ta"):
        assert f":{tid}" in text, tid
    # prose is verbatim
    assert "[USER] please fix the bug and record it" in text
    assert "[ASSISTANT] on it" in text
    # sidechain + compaction carriers are dropped
    assert "SIDECHAIN SECRET" not in text and "RESTATED HISTORY" not in text
    assert stats["rows"] > stats["kept"] > 0


def test_kmcp_read_is_a_ref_line_never_a_body(transcript):
    text, _ = _render(transcript)
    assert "  KMCP READ t:tk: claudecode:lesson/x" in text
    assert "entry body" not in text


# ---- kmcp writes --------------------------------------------------------------
def test_kmcp_write_lines(transcript):
    text, _ = _render(transcript)
    imp = _line(text, "KMCP WRITE x:ti:")
    assert imp.strip() == ("KMCP WRITE x:ti: import_entries "
                           "knowledge_mcp:design/alpha, knowledge_mcp_code:task/beta")
    rel = _line(text, "KMCP WRITE x:tr:")
    assert rel.strip() == ("KMCP WRITE x:tr: create_relationship "
                           "knowledge_mcp:design/alpha -> "
                           "knowledge_mcp_code:task/beta (see_also)")
    bad = _line(text, "KMCP WRITE x:tp:")
    assert bad.strip() == ("KMCP WRITE x:tp: patch_content knowledge_mcp:design/alpha"
                           " ✗ Content validation failed after patch")


def test_kmcp_write_refs_shapes():
    assert digest_cr.kmcp_write_refs(
        "create_relationship", {"application": "a", "source_path": "p/s",
                                "target_path": "p/t",
                                "relationship_type": "depends_on"}) \
        == "a:p/s -> p/t (depends_on)"
    assert digest_cr.kmcp_write_refs(
        "move_entry", {"application": "a", "old_path": "p/old",
                       "new_path": "p/new"}) == "a:p/old -> p/new"
    assert digest_cr.kmcp_write_refs(
        "import_entries", {"entries": [{"application": "a", "path": "p/1"},
                                       {"application": "b", "path": "p/2"}]}) \
        == "a:p/1, b:p/2"


def test_kmcp_write_via_knowledge_cli_shim(tmp_path):
    recs = [
        _user("u1", "write it"),
        _assistant("a1", "", tool_uses=[("tc", "Bash", {
            "command": "knowledge-cli call create_entry "
                       "'{\"application\":\"app\",\"path\":\"lesson/y\"}'"})]),
        _result("r1", "tc", "{\"created\": true}"),
    ]
    p = tmp_path / f"{SID}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    text, _ = _render(p)
    assert "KMCP WRITE x:tc: create_entry app:lesson/y (via knowledge-cli)" in text


# ---- kept exceptions ------------------------------------------------------------
def test_error_result_is_kept_with_head(transcript):
    text, _ = _render(transcript)
    err = _line(text, "TOOL ERROR t:tf")
    assert err.startswith("  TOOL ERROR t:tf (Bash pytest -q): EEEE")
    assert "E" * 400 in err and "E" * 401 not in err
    assert err.endswith("[--row t:tf]")
    # the failed call's input is still stubbed at the call site
    assert "[x:tf Bash pytest -q — in" in text


def test_agent_result_kept_and_capped(transcript):
    text, _ = _render(transcript)
    assert "  AGENT x:ta: general-purpose — survey the repo" in text
    assert "AGENT RESULT t:ta (general-purpose — survey the repo):" in text
    assert "R" * 2000 in text and "R" * 2001 not in text
    assert "… [+3.0K elided, --row t:ta]" in text


def test_background_agent_report_kept_from_task_notification(tmp_path):
    recs = [
        _user("u1", "delegate it"),
        _assistant("a1", "", tool_uses=[("tg", "Agent", {
            "subagent_type": "general-purpose", "description": "bg job",
            "run_in_background": True})]),
        _result("r1", "tg", [{"type": "text", "text":
                              "Async agent launched successfully. agentId: x"}]),
        _user("n1", "<task-notification>\n<task-id>x</task-id>\n"
                    "<tool-use-id>tg</tool-use-id>\n<status>completed</status>\n"
                    "<summary>Agent \"bg job\" finished</summary>\n"
                    "<result>THE REPORT</result>\n</task-notification>",
              isMeta=True),
    ]
    p = tmp_path / f"{SID}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    text, _ = _render(p)
    assert "AGENT x:tg: general-purpose — bg job (background" in text
    assert "Async agent launched" not in text
    assert "AGENT REPORT s:n1 (completed · x:tg bg job):\nTHE REPORT" in text


def test_edit_hint_is_the_full_path(transcript):
    text, _ = _render(transcript)
    assert len(LONG_PATH) > 60
    assert f"[t:te Edit {LONG_PATH} — in" in text
    assert "[t:tw Write (create/overwrite) /tmp/new.py — in 0.9K" in text


# ---- --since --------------------------------------------------------------------
def test_since_window(transcript):
    from datetime import datetime, timezone
    text, stats = _render(transcript, since=datetime(2026, 8, 21, 0, 0,
                                                     tzinfo=timezone.utc))
    assert text.splitlines()[0] == f"SESSION DIGEST  ·  {SID}   [cr]"
    assert "delta span: records after 2026-08-21T00:00:00+00:00" in text
    assert "now record it in kmcp" in text
    assert "please fix the bug" not in text and ":tb" not in text
    assert "KMCP WRITE x:ti:" in text
    # turn numbers come from the WHOLE session, not the window
    assert "── turn 2 " in text and "── turn 1 " not in text


# ---- CLI: --cr, --out, --row ------------------------------------------------------
def test_cli_cr_stdout_and_accounting(transcript, no_db):
    res = CliRunner().invoke(cli.main, ["digest", SID[:8], "--cr"])
    assert res.exit_code == 0, res.output
    assert res.stdout.splitlines()[0] == f"SESSION DIGEST  ·  {SID}   [cr]"
    assert res.stderr.startswith("kept ")
    assert " rows · ~" in res.stderr and "est tokens (" in res.stderr


def test_cli_cr_out_prints_only_the_path(transcript, no_db, tmp_path):
    dest = tmp_path / "d.txt"
    res = CliRunner().invoke(cli.main, ["digest", SID, "--cr", "--out",
                                        str(dest), "--since",
                                        "2026-08-21T00:00:00Z"])
    assert res.exit_code == 0, res.output
    assert res.stdout == f"{dest}\n"
    assert dest.read_text().startswith(f"SESSION DIGEST  ·  {SID}   [cr]\n")


def test_cli_row_round_trip(transcript, no_db):
    text, _ = _render(transcript)
    assert "[t:tb " in text                     # the id the stub carries
    res = CliRunner().invoke(cli.main, ["digest", SID, "--row", "t:tb"])
    assert res.exit_code == 0, res.output
    assert res.stdout == "o" * 2300 + "\n"
    res = CliRunner().invoke(cli.main, ["digest", SID, "--row", "x:te"])
    assert res.exit_code == 0 and LONG_PATH in res.stdout
    res = CliRunner().invoke(cli.main, ["digest", SID, "--row", "s:s1"])
    assert res.exit_code == 0 and "skill body" in res.stdout


def test_cli_row_unknown_id_exits_2(transcript, no_db):
    res = CliRunner().invoke(cli.main, ["digest", SID, "--row", "t:nope"])
    assert res.exit_code == 2
    assert "t:nope" in res.stderr and res.stdout == ""


def test_cli_cr_rejects_plain_window_flags(transcript, no_db):
    res = CliRunner().invoke(cli.main, ["digest", SID, "--cr", "--head", "5"])
    assert res.exit_code == 2 and "--head" in res.output


def test_plain_digest_unchanged(transcript, no_db):
    res = CliRunner().invoke(cli.main, ["digest", SID])
    assert res.exit_code == 0
    assert res.stdout.splitlines()[0] == f"SESSION DIGEST  ·  {SID}.jsonl"
