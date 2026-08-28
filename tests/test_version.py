"""Unit tests for version resolution + the running-vs-disk staleness verdict.

Pins the three things the console's version chip depends on: one canonical
version (the package attribute, which pyproject also builds from), a running
snapshot that is captured once and never silently re-read, and a `stale`
verdict that only fires on a KNOWN difference (never on a missing sha). Run:

    uv run --extra dev pytest tests/test_version.py -q
"""
import re

import pytest

import claude_session_db
from claude_session_db import version as V


# ---------------------------------------------------------------- canonical
def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:[-+].+)?", V.VERSION)


def test_version_is_the_package_attribute():
    """One source of truth — the module must not carry its own copy."""
    assert V.VERSION == claude_session_db.__version__


def test_file_version_matches_the_imported_one():
    """The on-disk parse and the import agree in a clean checkout."""
    assert V.file_version() == claude_session_db.__version__


def test_cli_exposes_the_same_version():
    from click.testing import CliRunner

    from claude_session_db.cli import main
    res = CliRunner().invoke(main, ["--version"])
    assert res.exit_code == 0
    assert claude_session_db.__version__ in res.output


# ------------------------------------------------------------------ running
def test_capture_running_is_idempotent(monkeypatch):
    monkeypatch.setattr(V, "RUNNING", {})
    first = dict(V.capture_running())
    monkeypatch.setattr(V, "_snapshot", lambda from_disk=False: {
        "version": "9.9.9", "sha": "ffffff", "dirty": False})
    second = V.capture_running()
    assert second["version"] == first["version"] and second["sha"] == first["sha"]


def test_running_has_a_start_time():
    assert V.capture_running()["started_at"] > 0


# -------------------------------------------------------------- staleness
def _report(monkeypatch, run, disk):
    monkeypatch.setattr(V, "RUNNING", dict(run, started_at=1.0))
    monkeypatch.setattr(V, "disk_state", lambda max_age_s=0: disk)
    return V.version_report()


def test_stale_when_shas_differ(monkeypatch):
    r = _report(monkeypatch,
                {"version": "3.9.0", "sha": "aaaaaaa", "dirty": False},
                {"version": "3.9.1", "sha": "bbbbbbb", "dirty": False})
    assert r["stale"] is True
    assert (r["sha"], r["disk_sha"]) == ("aaaaaaa", "bbbbbbb")
    assert (r["version"], r["disk_version"]) == ("3.9.0", "3.9.1")


def test_not_stale_when_shas_match(monkeypatch):
    r = _report(monkeypatch,
                {"version": "3.9.0", "sha": "aaaaaaa", "dirty": False},
                {"version": "3.9.0", "sha": "aaaaaaa", "dirty": False})
    assert r["stale"] is False


@pytest.mark.parametrize("run_sha,disk_sha", [(None, "bbbbbbb"), ("aaaaaaa", None),
                                              (None, None)])
def test_unknown_sha_never_claims_staleness(monkeypatch, run_sha, disk_sha):
    """No git / not a checkout must degrade to "unknown", never a false alarm."""
    r = _report(monkeypatch,
                {"version": "3.9.0", "sha": run_sha, "dirty": None},
                {"version": "3.9.0", "sha": disk_sha, "dirty": None})
    assert r["stale"] is False


def test_git_failure_degrades_to_none(monkeypatch):
    monkeypatch.setattr(V, "_git", lambda *a: None)
    assert V.head_sha() is None and V.head_dirty() is None
    snap = V._snapshot()
    assert snap["sha"] is None and snap["version"] == V.VERSION


def test_disk_state_is_cached(monkeypatch):
    calls = []

    def fake(from_disk=False):
        calls.append(from_disk)
        return {"version": "3.9.0", "sha": "aaaaaaa", "dirty": False}

    monkeypatch.setattr(V, "_snapshot", fake)
    monkeypatch.setattr(V, "_disk_cache", {"at": 0.0, "val": None})
    V.disk_state()
    V.disk_state()
    assert calls == [True]          # second call served from cache


# ------------------------------------------------------------------ changelog
def test_changelog_is_present_and_starts_with_the_current_version():
    txt = V.changelog_text()
    assert txt and txt.lstrip().startswith("# Changelog")
    assert f"## [{V.VERSION}]" in txt, "bump the version -> add its changelog entry"


def test_changelog_text_is_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(V, "CHANGELOG", tmp_path / "nope.md")
    assert V.changelog_text() is None
