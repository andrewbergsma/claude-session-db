"""Three latent traps found in the v2.1.161-258 impact review.

None of them was a live bug when found. All three are the shape that becomes
one silently: two code paths disagreeing about the same fact, or a derivation
that produces a confident wrong answer with no way to tell.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from claude_session_db import usage
from claude_session_db.subagent import discover_subagents
from claude_session_db.sync import (
    SyncStats,
    decode_project_path,
    encode_project_path,
    project_path_is_decodable,
)


# --- trap 1: two subagent discoverers, two answers -------------------------

def test_nested_workflow_agents_are_discovered(tmp_path):
    """`discover_subagents` globbed flat while `SessionSync.enumerate_files`
    walks with rglob, so the two disagreed about what a session's subagents
    are. Not live (the archive uses the rglob path) — but a divergence that
    becomes a real bug the moment anything else calls this."""
    sd = tmp_path / "sess"
    flat = sd / "subagents"
    nested = flat / "workflows" / "wf_123"
    nested.mkdir(parents=True)
    (flat / "agent-a0ae7c4c583360944.jsonl").write_text("")
    (nested / "agent-b1bf8d5d694471055.jsonl").write_text("")

    found = {s.agent_id for s in discover_subagents(sd)}
    assert found == {"a0ae7c4c583360944", "b1bf8d5d694471055"}


def test_subagent_discovery_ignores_non_agent_files(tmp_path):
    sd = tmp_path / "sess"
    (sd / "subagents" / "workflows" / "wf_1").mkdir(parents=True)
    (sd / "subagents" / "journal.jsonl").write_text("")
    (sd / "subagents" / "agent-a1.jsonl").write_text("")
    assert [s.agent_id for s in discover_subagents(sd)] == ["a1"]


def test_missing_subagent_dir_is_empty(tmp_path):
    assert discover_subagents(tmp_path / "nope") == []


# --- trap 2: a non-invertible encoding decoded with confidence -------------

def test_encoding_maps_both_slash_and_dot():
    """The root of the problem: `-` is three different characters."""
    assert encode_project_path("/Users/me/.claude") == "-Users-me--claude"
    assert encode_project_path("/Users/me/claude-session-db") == \
        "-Users-me-claude-session-db"


def test_plain_path_still_decodes():
    assert decode_project_path("-Users-me-GitHub-x") == "/Users/me/GitHub/x"


def test_dot_directory_is_flagged_not_silently_wrong():
    """`-Users-andrew--claude` naively decodes to `/Users/andrew//claude`,
    which normalises to `/Users/andrew/claude` — a real-looking path that is
    NOT the session's directory. Every worktree project is in this family."""
    enc = "-Users-andrew--claude"
    dec = decode_project_path(enc)
    assert not project_path_is_decodable(enc, dec)


def test_cwd_hint_is_the_reliable_inversion():
    """The transcript carries its own cwd. When it encodes to this exact
    directory name it IS the answer — no guessing."""
    real = "/Users/andrew/.claude"
    enc = encode_project_path(real)
    assert decode_project_path(enc, cwd_hint=real) == real
    assert project_path_is_decodable(enc, real) is (Path(real).is_dir())


def test_a_wrong_cwd_hint_is_ignored():
    """A hint that does not encode to this directory belongs to another
    project and must not be trusted."""
    enc = "-Users-me-GitHub-x"
    assert decode_project_path(enc, cwd_hint="/somewhere/else") == "/Users/me/GitHub/x"


def test_operator_named_project_dir_is_flagged():
    """CLAUDE_CODE_PROJECT_DIR_NAME is an arbitrary name with no path
    relationship at all."""
    enc = "my-custom-project-name"
    assert not project_path_is_decodable(enc, decode_project_path(enc))


def test_undecodable_projects_are_reported():
    s = SyncStats()
    s.note_undecodable_project("-Users-andrew--claude")
    s.note_undecodable_project("-Users-andrew--claude")     # a set, not a count
    assert len(s.undecodable_projects) == 1
    assert "undecodable project dirs" in s.oneline()
    assert "UNDECODABLE project dirs: 1" in str(s)
    # and it says what is still trustworthy
    assert "encoded_path is the key and is exact" in str(s)


def test_clean_sync_says_nothing_about_projects():
    assert "undecodable" not in SyncStats().oneline()


# --- trap 3: a pinned client version, 56 releases stale --------------------

def test_user_agent_drift_is_documented():
    """`USER_AGENT` pins 2.1.202 on a 2.1.258+ machine. It works, which is why
    it needs a comment: a rejected user agent fails as a 401/403 and looks
    exactly like an auth problem. Deliberately not exercised — the OAuth path
    spends a real token rotation."""
    import inspect
    src = inspect.getsource(usage)
    assert "WATCH ITEM" in src
    assert "2.1.202" in usage.USER_AGENT
    assert "re-extract" in src
