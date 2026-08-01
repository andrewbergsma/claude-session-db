"""Unit tests for the /api/files & /api/file confinement gate.

The gate (`_confine`) is the single path check both endpoints run every
caller-supplied relative path through; these tests pin its refusals
(traversal, absolute, NUL, .git, symlink escapes) and the file-serving
floor (regular files only). Run:

    uv run --extra dev pytest tests/test_confine.py -q
"""
import os

import pytest

from claude_session_db.console.server import (
    ConfineError, _confine, _file_json, _list_dir)


@pytest.fixture
def tree(tmp_path):
    """root/ with a file, a subdir, .git/, dotfile, symlinks; outside/ beside it."""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "inner.txt").write_text("inner")
    (root / "a.txt").write_text("hello")
    (root / ".hidden").write_text("h")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("secret")
    outside.mkdir()
    (outside / "secret.txt").write_text("outside secret")
    os.symlink(outside / "secret.txt", root / "escape.txt")     # escapes root
    os.symlink(root / "a.txt", root / "ok.txt")                 # within root
    os.symlink(root / ".git" / "config", root / "sneaky.txt")   # into .git
    os.symlink(root / "gone.txt", root / "broken.txt")          # dangling
    return root


# ---- refusals ---------------------------------------------------------------
def test_dotdot_rejected(tree):
    with pytest.raises(ConfineError):
        _confine(str(tree), "../outside/secret.txt")
    with pytest.raises(ConfineError):
        _confine(str(tree), "sub/../../outside/secret.txt")
    with pytest.raises(ConfineError):
        _confine(str(tree), "..")


def test_absolute_rejected(tree):
    with pytest.raises(ConfineError):
        _confine(str(tree), "/etc/passwd")
    with pytest.raises(ConfineError):
        _confine(str(tree), "\\windows\\style")


def test_nul_rejected(tree):
    with pytest.raises(ConfineError):
        _confine(str(tree), "a.txt\x00.png")


def test_git_rejected(tree):
    with pytest.raises(ConfineError):
        _confine(str(tree), ".git/config")
    with pytest.raises(ConfineError):
        _confine(str(tree), ".git")
    # a symlink that resolves INTO .git can't launder the exclusion
    with pytest.raises(ConfineError):
        _confine(str(tree), "sneaky.txt")


def test_symlink_escape_rejected(tree):
    with pytest.raises(ConfineError):
        _confine(str(tree), "escape.txt")


# ---- allowed paths ----------------------------------------------------------
def test_root_and_plain_file(tree):
    assert _confine(str(tree), "") == os.path.realpath(str(tree))
    assert _confine(str(tree), "a.txt") == \
        os.path.realpath(str(tree / "a.txt"))
    assert _confine(str(tree), "sub/inner.txt") == \
        os.path.realpath(str(tree / "sub" / "inner.txt"))


def test_symlink_within_root_ok(tree):
    # resolves to the real target, still inside the root
    assert _confine(str(tree), "ok.txt") == \
        os.path.realpath(str(tree / "a.txt"))
    payload, code = _file_json(str(tree), "ok.txt")
    assert code == 200 and payload["content"] == "hello"


# ---- file serving floor -----------------------------------------------------
def test_non_regular_file_refused(tree):
    os.mkfifo(tree / "pipe")            # a FIFO would hang the handler thread
    payload, code = _file_json(str(tree), "pipe")
    assert code == 403 and "regular" in payload["error"]


def test_escaping_symlink_403_via_file(tree):
    payload, code = _file_json(str(tree), "escape.txt")
    assert code == 403


# ---- listing behaviour ------------------------------------------------------
def test_list_dir_marks_and_excludes(tree):
    d = _list_dir(str(tree), "", hidden=False)
    names = {e["name"]: e for e in d["entries"]}
    assert ".git" not in names                      # always excluded
    assert ".hidden" not in names                   # hidden by default...
    assert d["hidden_count"] == 1                   # ...but counted
    assert names["escape.txt"].get("escapes") is True   # marked, not followed
    assert names["broken.txt"].get("broken") is True
    assert names["ok.txt"]["type"] == "symlink"
    assert names["ok.txt"]["target"] == "file"
    assert names["sub"]["type"] == "dir"
    # dirs-first ordering
    assert d["entries"][0]["name"] == "sub"
    assert d["truncated"] is False


def test_list_dir_hidden_toggle(tree):
    d = _list_dir(str(tree), "", hidden=True)
    names = {e["name"] for e in d["entries"]}
    assert ".hidden" in names
    assert ".git" not in names                      # excluded even with hidden=1
    assert d["hidden_count"] == 0
