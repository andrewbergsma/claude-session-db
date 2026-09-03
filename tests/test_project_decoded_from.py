"""`projects.decoded_path` no longer freezes at the first insert (schema v10).

Claude Code's project-dir encoding maps both `/` and `.` to `-` and is not
invertible, so the naive decode is a guess — wrong for every dot-directory and
every worktree project. The transcript's own `cwd` IS ground truth when it
re-encodes to the directory name, and `decode_project_path` already used it.

The bug was downstream: `get_or_create_project`'s conflict path updated only
`last_seen_at`. A project first seen without a usable cwd hint therefore kept
its guess forever, even after a later transcript supplied the truth. And the
per-run cache stored the first resolution, so the upgrade could not even happen
within one sync.

`decoded_from` ('cwd' | 'encoded' | NULL = pre-v10) records the provenance,
which is what makes the upgrade safe: it is strictly one-way.
"""
from __future__ import annotations

import inspect

from claude_session_db import postgres
from claude_session_db.sync import SessionSync, encode_project_path


# --- schema ----------------------------------------------------------------

def test_column_is_declared_guarded_and_additive():
    sql = postgres.SCHEMA_SQL
    assert "ALTER TABLE projects ADD COLUMN decoded_from TEXT" in sql
    assert "column_name = 'decoded_from'" in sql, "the ALTER must be catalog-guarded"
    assert "DROP COLUMN" not in sql


def test_the_conflict_path_upgrades_the_path_and_the_name():
    src = inspect.getsource(postgres.SessionArchive.get_or_create_project)
    for col in ("decoded_path", "project_name", "decoded_from"):
        assert f"{col} = CASE WHEN EXCLUDED.decoded_from = 'cwd'" in src
    assert "last_seen_at = now()" in src, "the old behaviour is kept, not replaced"


def test_a_guess_never_overwrites_anything():
    """One-way: 'cwd' wins, 'encoded' loses. It can never downgrade."""
    src = inspect.getsource(postgres.SessionArchive.get_or_create_project)
    assert "ELSE projects.decoded_path END" in src
    assert "ELSE projects.project_name END" in src
    assert "ELSE projects.decoded_from END" in src


def test_the_encoded_path_is_still_the_key():
    src = inspect.getsource(postgres.SessionArchive.get_or_create_project)
    assert "ON CONFLICT (encoded_path)" in src
    assert "encoded_path =" not in src, "the unique key must never be rewritten"


def test_the_default_provenance_is_the_guess():
    sig = inspect.signature(postgres.SessionArchive.get_or_create_project)
    assert sig.parameters["decoded_from"].default == "encoded"


# --- the sync-side resolution ----------------------------------------------

class _Arch:
    def __init__(self):
        self.calls = []

    def get_or_create_project(self, encoded, decoded, decoded_from="encoded"):
        self.calls.append((encoded, decoded, decoded_from))
        return 7


def _sync(arch):
    s = SessionSync.__new__(SessionSync)
    s.archive = arch
    s._project_cache = {}
    return s


REAL = "/Users/andrew/.claude"
ENC = encode_project_path(REAL)          # dot-directory: the naive decode is wrong


def test_a_matching_cwd_hint_is_recorded_as_ground_truth():
    arch = _Arch()
    _sync(arch)._project_id_for(ENC, cwd_hint=REAL)
    assert arch.calls == [(ENC, REAL, "cwd")]


def test_no_hint_is_recorded_as_a_guess():
    arch = _Arch()
    _sync(arch)._project_id_for(ENC)
    enc, decoded, how = arch.calls[0]
    assert how == "encoded"
    assert decoded != REAL, "the naive decode of a dot-directory IS wrong"


def test_a_non_matching_cwd_hint_is_still_a_guess():
    """A cwd that does not re-encode to this directory name is not evidence
    about this directory — CLAUDE_CODE_PROJECT_DIR_NAME, the v2.1.224 long-path
    scheme, a scratchpad project."""
    arch = _Arch()
    _sync(arch)._project_id_for(ENC, cwd_hint="/somewhere/else")
    assert arch.calls[0][2] == "encoded"


def test_a_later_file_with_a_real_hint_upgrades_within_the_same_run():
    """The per-run cache used to freeze the first (guessed) resolution, so the
    upgrade could not happen even when a later file had the answer."""
    arch = _Arch()
    s = _sync(arch)
    s._project_id_for(ENC)                     # first file: no hint
    s._project_id_for(ENC, cwd_hint=REAL)      # later file: ground truth
    assert [c[2] for c in arch.calls] == ["encoded", "cwd"]


def test_once_resolved_from_cwd_the_cache_stops_asking():
    arch = _Arch()
    s = _sync(arch)
    s._project_id_for(ENC, cwd_hint=REAL)
    s._project_id_for(ENC, cwd_hint=REAL)
    s._project_id_for(ENC)                     # a guess must not re-resolve
    assert len(arch.calls) == 1


def test_repeated_guesses_do_not_re_resolve():
    arch = _Arch()
    s = _sync(arch)
    s._project_id_for(ENC)
    s._project_id_for(ENC)
    assert len(arch.calls) == 1


def test_the_project_id_is_returned_either_way():
    arch = _Arch()
    s = _sync(arch)
    assert s._project_id_for(ENC) == 7
    assert s._project_id_for(ENC, cwd_hint=REAL) == 7
