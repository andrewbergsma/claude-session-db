"""`csd digest` + `csd summary-scope` — the two commands the /session-summary
skill calls instead of hardcoding an absolute python path and a `find | head`
command substitution.

Semantics under test:
  - digest RESOLVES a session ref worktree-aware and with NO database at all
    (transcript glob), and prints byte-for-byte what session_digest.render
    prints — same `SESSION DIGEST · …` header, same `span:` / `delta span:`.
  - an unresolvable ref exits non-zero with `NO TRANSCRIPT FOUND for <ref>` on
    STDERR (the string the skill branches on), never a traceback.
  - the default scope is the WHOLE transcript (session_digest's own default),
    not the head/tail window `csd angles digest` applies.
  - summary-scope grades the next pass through the SAME summarize._delta_gate
    the console button and the launchd timer use, and NEVER raises: an
    unreachable archive degrades to `pass 1 / full` with the reason, exit 0.

Run:  uv run --extra dev pytest tests/test_cli_digest_scope.py -q
"""
import json

import pytest
from click.testing import CliRunner

from claude_session_db import cli
from claude_session_db import session_mgmt as mgmt
from claude_session_db import summarize as ph4
from claude_session_db.session_digest import render as render_digest

SID = "aaaaaaaa-1111-2222-3333-444444444444"
CHILD = f"{SID}:abc0123456789abcd"


def _rec(ts, role, text):
    # A user prompt carries plain string content in a real transcript; an
    # assistant turn carries content blocks.
    body = text if role == "user" else [{"type": "text", "text": text}]
    return {"type": role, "timestamp": ts, "message": {"content": body}}


@pytest.fixture
def transcript(tmp_path, monkeypatch):
    """A session JSONL under a fake ~/.claude/projects, and NO database."""
    proj = tmp_path / "-Users-andrew-GitHub-thing"
    proj.mkdir()
    p = proj / f"{SID}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in [
        _rec("2026-08-20T10:00:00.000Z", "user", "first prompt"),
        _rec("2026-08-20T10:00:05.000Z", "assistant", "first answer"),
        _rec("2026-08-21T15:00:00.000Z", "user", "second prompt"),
        _rec("2026-08-21T15:00:09.000Z", "assistant", "second answer"),
    ]) + "\n")
    monkeypatch.setattr(mgmt, "PROJECTS_DIR", tmp_path)
    return p


@pytest.fixture
def no_db(monkeypatch):
    """The archive is not configured at all — the group must still dispatch."""
    def boom(explicit=None):
        raise RuntimeError("No database DSN found.")
    monkeypatch.setattr(cli, "resolve_dsn", boom)


def run(*args):
    return CliRunner().invoke(cli.main, list(args))


# --------------------------------------------------------------- csd digest

def test_digest_runs_with_no_database_and_matches_the_renderer(transcript, no_db):
    """Drop-in for `python3 session_digest.py <path>` — same bytes, and the
    skill no longer needs to know where the file (or the interpreter) is."""
    res = run("digest", SID)
    assert res.exit_code == 0
    assert res.stdout == render_digest(transcript)
    assert res.stdout.startswith(f"SESSION DIGEST  ·  {SID}.jsonl\n")
    assert "span: 2026-08-20T10:00:00.000Z -> 2026-08-21T15:00:09.000Z" in res.output


def test_digest_accepts_a_unique_prefix(transcript, no_db):
    res = run("digest", SID[:8])
    assert res.exit_code == 0 and f"{SID}.jsonl" in res.output


def test_digest_default_scope_is_the_whole_transcript(transcript, no_db):
    """`csd angles digest` windows by default; this one must NOT — the skill
    summarizes from it, and a silently elided middle is a silently short
    summary. --full is accepted and means the same thing."""
    plain, full = run("digest", SID), run("digest", SID, "--full")
    assert plain.stdout == full.stdout
    assert "records elided" not in plain.output
    assert "first prompt" in plain.output and "second answer" in plain.output


def test_digest_since_renders_only_the_tail(transcript, no_db):
    res = run("digest", SID, "--since", "2026-08-21T00:00:00Z")
    assert res.exit_code == 0
    assert "delta span: 2026-08-21T15:00:00.000Z" in res.output
    assert "(2 of 4 records)" in res.output
    assert "first prompt" not in res.output and "second prompt" in res.output


def test_digest_since_rejects_an_unparseable_timestamp(transcript, no_db):
    res = run("digest", SID, "--since", "last tuesday")
    assert res.exit_code == 1
    assert "unparseable --since" in res.stderr


def test_digest_head_tail_windows_and_says_so(transcript, no_db):
    res = run("digest", SID, "--head", "1", "--tail", "1")
    assert res.exit_code == 0 and "records elided" in res.output


def test_an_unresolvable_ref_is_the_string_the_skill_branches_on(transcript, no_db):
    res = run("digest", "ffffffff-9999-9999-9999-999999999999")
    assert res.exit_code == 1
    assert res.stderr.startswith(
        "NO TRANSCRIPT FOUND for ffffffff-9999-9999-9999-999999999999")
    assert res.stdout == ""


def test_a_subagent_child_key_is_refused_and_points_at_the_parent(transcript, no_db):
    """Sidechain records live in the PARENT file and session_digest renders
    main-chain only — digesting the parent under a child's name would return
    work that is not the child's."""
    res = run("digest", CHILD)
    assert res.exit_code == 1
    assert f"NO TRANSCRIPT FOUND for {CHILD}" in res.stderr
    assert f"csd digest {SID}" in res.stderr


def test_since_and_delta_are_mutually_exclusive():
    with pytest.raises(ValueError, match="mutually exclusive"):
        mgmt.digest_for(SID, dsn=None, since="2026-08-21T00:00:00Z", delta=True)


# -------------------------------------------------------- csd summary-scope

class _Gate:
    def __init__(self, **kw):
        d = {"mode": "delta", "pass_no": 2, "watermark": object(),
             "source": "leaf", "report": None, "end_ts": None,
             "prev_ref": "claude_session_db:session/2026-08-21/xyz", "skip": None}
        d.update(kw)
        self.__dict__.update(d)


class _Report:
    def __init__(self, records):
        self.records = records


WM_ISO = "2026-08-21T14:02:11Z"


@pytest.fixture
def graded(monkeypatch, transcript, no_db):
    """Wire the grader's two collaborators — no archive, no transcript reads."""
    monkeypatch.setattr(ph4, "_iso", lambda dt: WM_ISO if dt is not None else None)

    def install(row=None, gate=None):
        monkeypatch.setattr(ph4, "prior_capture",
                            lambda sid, dsn: row if row is not None
                            else {"session_id": sid, "prev_pass": 1})
        monkeypatch.setattr(ph4, "_delta_gate",
                            lambda r, dsn, kdsn, **kw: gate or _Gate())
    return install


def test_scope_delta_reports_the_window_and_the_exact_digest_command(graded):
    graded(gate=_Gate(report=_Report(84)))
    res = run("summary-scope", SID)
    assert res.exit_code == 0
    assert f"session: {SID}   pass: 2   scope: delta" in res.output
    assert f"since:   {WM_ISO}   (watermark source: leaf_uuid)" in res.output
    assert "prior:   claude_session_db:session/2026-08-21/xyz" in res.output
    assert f"digest:  csd digest {SID} --since {WM_ISO}" in res.output


def test_json_carries_the_same_facts(graded):
    graded(gate=_Gate(report=_Report(84)))
    out = json.loads(run("summary-scope", SID, "--json").output)
    assert out["scope"] == "delta" and out["pass"] == 2
    assert out["since"] == WM_ISO and out["source"] == "leaf"
    assert out["records"] == 84 and out["summarized"] is True


def test_a_never_summarized_session_is_pass_1_full(graded):
    graded(row={"session_id": SID, "prev_pass": 0},
           gate=_Gate(mode="full", pass_no=1, watermark=None, source="none",
                      prev_ref=None))
    res = run("summary-scope", SID)
    assert "pass: 1   scope: full   (no prior summary found)" in res.output
    assert f"digest:  csd digest {SID}" in res.output and "--since" not in res.output


def test_an_unsubstantive_tail_is_scope_none_with_the_reason(graded):
    """Captured already and nothing worth writing since — the skill must be
    told 'none', not handed a full scope that would restate pass 1."""
    graded(gate=_Gate(skip="delta below floor (4 < 20 records)",
                      report=_Report(4)))
    res = run("summary-scope", SID)
    assert res.exit_code == 0
    assert "scope: none — delta below floor (4 < 20 records)" in res.output
    assert f"since:   {WM_ISO}" in res.output          # still addressable
    assert "`--mode force` windows" in res.output


def test_force_windows_a_tail_auto_would_have_skipped(graded):
    graded(gate=_Gate(skip="delta below floor (4 < 20 records)", report=_Report(4)))
    res = run("summary-scope", SID, "--mode", "force")
    assert "scope: delta" in res.output and "dispatched anyway" in res.output


def test_a_capture_with_no_resolvable_watermark_is_full_not_delta(graded):
    """No window => no honest continuation: full scope, said out loud."""
    graded(gate=_Gate(mode="full", watermark=None, source="none"))
    res = run("summary-scope", SID)
    assert "scope: full" in res.output and "no summary watermark" in res.output


def test_an_unreachable_archive_degrades_to_pass_1_full_exit_0(monkeypatch,
                                                               transcript, no_db):
    """Doctrine: the grader can never block its caller."""
    def boom(sid, dsn):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(ph4, "prior_capture", boom)
    res = run("summary-scope", SID)
    assert res.exit_code == 0
    assert "pass: 1   scope: full" in res.output
    assert "connection refused" in res.output


def test_an_unresolvable_ref_still_reports_instead_of_raising(monkeypatch, no_db,
                                                             tmp_path):
    monkeypatch.setattr(mgmt, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(ph4, "prior_capture",
                        lambda sid, dsn: (_ for _ in ()).throw(
                            RuntimeError("no archive DSN configured")))
    res = run("summary-scope", "ffffffff-0000-0000-0000-000000000000")
    assert res.exit_code == 0 and "scope: full" in res.output


# ------------------------------------------------- one grader, two surfaces

def test_the_console_delegates_to_the_same_grader(monkeypatch):
    """The console's resolve_summary_scope is a binding of its DSNs to
    summarize.resolve_summary_scope — not a second implementation."""
    from claude_session_db.console import server
    seen = {}

    def spy(sid, dsn, kmcp_dsn=None, mode="auto", prior_capture_fn=None):
        seen.update(sid=sid, dsn=dsn, kmcp_dsn=kmcp_dsn, mode=mode)
        return {"delta": False}

    monkeypatch.setattr(server, "CSD_DSN", "csd-dsn")
    monkeypatch.setattr(server, "KMCP_DSN", "kmcp-dsn")
    monkeypatch.setattr(ph4, "resolve_summary_scope", spy)
    assert server.resolve_summary_scope(SID, "force") == {"delta": False}
    assert seen == {"sid": SID, "dsn": "csd-dsn", "kmcp_dsn": "kmcp-dsn",
                    "mode": "force"}
