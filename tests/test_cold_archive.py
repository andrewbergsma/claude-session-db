"""The cold archive (3.30.0): incremental aggregates, the idle gate, the
live-pid exclusion and the gated schema DDL.

Before 3.30.0 every sweep (every five minutes) ran the full
`recompute_session_aggregates()` whether or not a file had changed: four seq
scans of `messages`, two sorts spilling to temp and every `sessions` row
rewritten. It also re-ingested live transcripts on every tick and re-ran
SCHEMA_SQL, whose unguarded `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` took
ACCESS EXCLUSIVE each time. All DB-free: a fake cursor records the SQL.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest

from claude_session_db import postgres
from claude_session_db.postgres import SessionArchive
from claude_session_db.sync import (
    DEFAULT_MIN_IDLE_S,
    SessionSync,
    live_session_ids,
    parse_duration,
)


# --- fakes -------------------------------------------------------------------

class _Cur:
    def __init__(self, log, fetch=None):
        self.log, self._fetch = log, fetch or (lambda sql: None)
        self._last = None

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        self._last = sql

    def fetchone(self):
        return self._fetch(self._last)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self, fetch=None):
        self.executed: list = []
        self.commits = 0
        self.closed = False
        self._fetch = fetch

    def cursor(self):
        return _Cur(self.executed, self._fetch)

    def commit(self):
        self.commits += 1


def _archive(conn) -> SessionArchive:
    a = SessionArchive("postgresql://unused/claude_sessions")
    a.conn = conn
    return a


# --- recompute_session_aggregates scoping -----------------------------------

def test_empty_scope_touches_nothing():
    conn = _Conn()
    _archive(conn).recompute_session_aggregates(set())
    assert conn.executed == []
    assert conn.commits == 0


def test_none_ids_only_set_is_treated_as_empty():
    conn = _Conn()
    _archive(conn).recompute_session_aggregates({None, ""})
    assert conn.executed == []


def test_scoped_recompute_binds_only_the_changed_sessions():
    conn = _Conn()
    _archive(conn).recompute_session_aggregates({"s2", "s1", "s1"})
    assert len(conn.executed) == 2                    # main pass + child pass
    for sql, params in conn.executed:
        assert params == {"ids": ["s1", "s2"]}        # deduped, sorted
        assert "= ANY(%(ids)s)" in sql
    main, child = (sql for sql, _ in conn.executed)

    # Every CTE of the main pass is scoped, so no input table is scanned whole.
    assert re.search(r"FROM messages WHERE session_id = ANY\(%\(ids\)s\)\s+GROUP BY", main)
    assert re.search(r"FROM system_events WHERE session_id = ANY\(%\(ids\)s\)", main)
    # content_blocks / tool_results have no session_id index: they are reached
    # through an InitPlan array of the scoped sessions' message uuids, which
    # becomes an Index Cond on idx_cb_message / idx_tr_message.
    uuids = "ANY(ARRAY(SELECT uuid FROM messages WHERE session_id = ANY(%(ids)s)))"
    assert f"AND cb.session_id = ANY(%(ids)s) AND cb.message_uuid = {uuids}" in main
    assert f"AND tr.session_id = ANY(%(ids)s) AND tr.message_uuid = {uuids}" in main
    # the sessions side is a PK lookup, not a scan of every row
    assert "WHERE s.session_id = agg.session_id AND s.session_id = ANY(%(ids)s)" in main

    # Child pass: scoped by the parent key in every CTE and in the UPDATE.
    assert "WHERE agent_id IS NOT NULL AND session_id = ANY(%(ids)s)" in child
    assert child.count("AND m.session_id = ANY(%(ids)s)") == 2
    child_uuids = ("ANY(ARRAY(SELECT uuid FROM messages WHERE session_id = "
                   "ANY(%(ids)s) AND agent_id IS NOT NULL))")
    assert f"AND cb.message_uuid = {child_uuids}" in child
    assert f"AND tr.message_uuid = {child_uuids}" in child
    assert "AND s.parent_session_id = ANY(%(ids)s)" in child
    assert conn.commits == 1


def test_large_scope_is_chunked_one_commit_per_chunk():
    n = postgres.AGGREGATE_CHUNK_SESSIONS
    ids = {f"s{i:04d}" for i in range(2 * n + 3)}
    conn = _Conn()
    _archive(conn).recompute_session_aggregates(ids)
    assert len(conn.executed) == 2 * 3                # 3 chunks x (main, child)
    assert conn.commits == 3
    seen = [p["ids"] for _, p in conn.executed[::2]]
    assert [len(c) for c in seen] == [n, n, 3]
    assert sorted(sum(seen, [])) == sorted(ids)       # every id exactly once


def test_full_recompute_is_unscoped():
    conn = _Conn()
    _archive(conn).recompute_session_aggregates()
    assert len(conn.executed) == 2
    for sql, params in conn.executed:
        assert params is None
        assert "ANY(" not in sql
        assert "%(" not in sql                       # no stray placeholder
        assert "{scope" not in sql                   # every fragment rendered


# --- the fake archive for sync_all ------------------------------------------

class _FakeArchive:
    def __init__(self, synced=None, previous=None):
        self.synced = dict(synced or {})          # source_file -> mtime_ns
        self.previous = previous or {}            # source_file -> {session ids}
        self.recompute_calls: list = []
        self.cleared: list = []
        self.init_kwargs = None
        self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def initialize(self, **kw):
        self.init_kwargs = kw

    def drop_all(self):
        pass

    def needs_sync(self, f, m):
        return self.synced.get(f) != m

    def clear_file_data(self, f):
        self.cleared.append(f)
        return set(self.previous.get(f, ()))

    def message_uuids_owned_elsewhere(self, uuids, source_file):
        return set()

    def get_or_create_project(self, *a, **k):
        return 1

    def update_sync_state(self, f, m, *a):
        self.synced[f] = m

    def get_task_output_mtimes(self, sid):
        return {}

    def recompute_session_aggregates(self, session_ids=None):
        self.recompute_calls.append(
            None if session_ids is None else set(session_ids))

    def commit(self):
        pass

    def __getattr__(self, name):
        if name.startswith(("insert_", "upsert_")):
            return lambda *a, **k: None
        raise AttributeError(name)


def _line(sid, uuid, text="hi"):
    return json.dumps({
        "type": "user", "uuid": uuid, "sessionId": sid,
        "timestamp": "2026-11-20T00:00:00.000Z", "cwd": "/tmp/x",
        "message": {"role": "user", "content": text},
    })


def _write(path: Path, sid: str, age_s: float, now: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_line(sid, f"u-{path.stem}") + "\n")
    os.utime(path, (now - age_s, now - age_s))
    return path


NOW = 1_800_000_000.0


@pytest.fixture
def claude_dir(tmp_path):
    d = tmp_path / ".claude"
    (d / "projects" / "-tmp-x").mkdir(parents=True)
    (d / "sessions").mkdir()
    return d


def _sync(claude_dir, archive):
    s = SessionSync(dsn="postgresql://unused/claude_sessions", claude_dir=claude_dir)
    s.archive = archive
    return s


def test_nothing_changed_skips_the_recompute(claude_dir):
    proj = claude_dir / "projects" / "-tmp-x"
    f = _write(proj / "aaaa.jsonl", "aaaa", 10 * 3600, NOW)
    arch = _FakeArchive(synced={str(f): f.stat().st_mtime_ns})
    stats = _sync(claude_dir, arch).sync_all(now=NOW)
    assert stats.files_skipped == 1 and stats.files_synced == 0
    assert stats.changed_sessions == set()
    assert arch.recompute_calls == []
    assert arch.init_kwargs["force_ddl"] is False


def test_recompute_scoped_to_changed_sessions_only(claude_dir):
    proj = claude_dir / "projects" / "-tmp-x"
    changed = _write(proj / "aaaa.jsonl", "aaaa", 10 * 3600, NOW)
    same = _write(proj / "bbbb.jsonl", "bbbb", 10 * 3600, NOW)
    sub = _write(proj / "cccc" / "subagents" / "agent-a1.jsonl", "cccc",
                 10 * 3600, NOW)
    arch = _FakeArchive(
        synced={str(same): same.stat().st_mtime_ns},
        # the re-synced file used to own rows under another session id too
        previous={str(changed): {"aaaa", "old-sid"}},
    )
    stats = _sync(claude_dir, arch).sync_all(now=NOW)
    assert stats.files_synced == 2 and stats.files_skipped == 1
    # the subagent scopes to its PARENT (sidechain rows carry its session_id)
    assert arch.recompute_calls == [{"aaaa", "old-sid", "cccc"}]
    assert "bbbb" not in arch.recompute_calls[0]


def test_force_recomputes_everything_and_forces_ddl(claude_dir):
    proj = claude_dir / "projects" / "-tmp-x"
    f = _write(proj / "aaaa.jsonl", "aaaa", 10 * 3600, NOW)
    arch = _FakeArchive(synced={str(f): f.stat().st_mtime_ns})
    _sync(claude_dir, arch).sync_all(force=True, now=NOW)
    assert arch.recompute_calls == [None]
    assert arch.init_kwargs["force_ddl"] is True


# --- the idle gate ----------------------------------------------------------

def test_default_gate_is_six_hours():
    assert DEFAULT_MIN_IDLE_S == 6 * 3600


def test_warm_files_are_deferred_not_written(claude_dir):
    proj = claude_dir / "projects" / "-tmp-x"
    warm = _write(proj / "warm.jsonl", "warm", 2 * 3600, NOW)          # 2 h old
    cold = _write(proj / "cold.jsonl", "cold", 7 * 3600, NOW)          # 7 h old
    warm_sub = _write(proj / "cold" / "subagents" / "agent-z.jsonl", "cold",
                      60, NOW)                                          # 1 min old
    arch = _FakeArchive()
    stats = _sync(claude_dir, arch).sync_all(now=NOW)
    assert stats.files_synced == 1
    assert stats.files_deferred == 2
    assert arch.cleared == [str(cold)]                # nothing else deleted
    assert str(warm) not in arch.synced               # sync_state still lags...
    assert str(warm_sub) not in arch.synced
    assert arch.recompute_calls == [{"cold"}]

    # ...so a later run, once the file has gone cold, picks it up.
    stats2 = _sync(claude_dir, arch).sync_all(now=NOW + 6 * 3600 + 120)
    assert stats2.files_synced == 2 and stats2.files_deferred == 0


def test_min_idle_zero_restores_the_old_behaviour(claude_dir):
    proj = claude_dir / "projects" / "-tmp-x"
    _write(proj / "warm.jsonl", "warm", 5, NOW)
    (claude_dir / "sessions" / f"{os.getpid()}.json").write_text(
        json.dumps({"pid": os.getpid(), "sessionId": "warm"}))
    arch = _FakeArchive()
    stats = _sync(claude_dir, arch).sync_all(min_idle_s=0, now=NOW)
    assert stats.files_synced == 1
    assert stats.files_deferred == stats.files_live == 0


def test_live_pid_session_is_skipped_however_cold(claude_dir):
    proj = claude_dir / "projects" / "-tmp-x"
    _write(proj / "live.jsonl", "live", 30 * 3600, NOW)                # idle a day+
    _write(proj / "live" / "subagents" / "agent-q.jsonl", "live", 30 * 3600, NOW)
    done = _write(proj / "done.jsonl", "done", 30 * 3600, NOW)
    (claude_dir / "sessions" / f"{os.getpid()}.json").write_text(
        json.dumps({"pid": os.getpid(), "sessionId": "live"}))
    arch = _FakeArchive()
    stats = _sync(claude_dir, arch).sync_all(now=NOW)
    assert stats.files_live == 2                      # main + its sidechain
    assert stats.files_synced == 1
    assert arch.cleared == [str(done)]
    assert arch.recompute_calls == [{"done"}]


def test_live_session_ids_reads_the_registry(tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    (d / "1.json").write_text(json.dumps({"pid": 1, "sessionId": "alive"}))
    (d / "2.json").write_text(json.dumps({"pid": 2, "sessionId": "dead"}))
    (d / "3.json").write_text("{not json")
    (d / "4.json").write_text(json.dumps({"pid": "x", "sessionId": "badpid"}))
    (d / "5.json").write_text(json.dumps({"pid": 5}))
    (d / "1.abc.key").write_text("secret")            # not a registry file
    got = live_session_ids(tmp_path, pid_alive=lambda pid: pid == 1)
    assert got == {"alive"}


def test_live_session_ids_real_pid_check(tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    (d / "me.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": "me"}))
    (d / "gone.json").write_text(json.dumps({"pid": 2 ** 31 - 2, "sessionId": "gone"}))
    assert live_session_ids(tmp_path) == {"me"}


def test_missing_registry_is_empty(tmp_path):
    assert live_session_ids(tmp_path / "nope") == set()


@pytest.mark.parametrize("raw,secs", [
    ("6h", 21600), ("90m", 5400), ("45s", 45), ("0", 0), ("30", 1800),
    ("1.5h", 5400), ("2d", 172800), (" 6H ", 21600),
])
def test_parse_duration(raw, secs):
    assert parse_duration(raw) == secs


@pytest.mark.parametrize("raw", ["", "h", "-1h", "soon"])
def test_parse_duration_rejects(raw):
    with pytest.raises(ValueError):
        parse_duration(raw)


def test_cli_exposes_min_idle_on_ingest_and_sweep():
    from claude_session_db.cli import ingest, sweep
    for cmd in (ingest, sweep):
        opt = next(p for p in cmd.params if p.name == "min_idle_s")
        assert "--min-idle" in opt.opts
        assert opt.default == DEFAULT_MIN_IDLE_S


# --- the schema DDL gate ------------------------------------------------------

def test_error_class_alter_is_catalog_guarded():
    sql = postgres.SCHEMA_SQL
    assert "ADD COLUMN IF NOT EXISTS error_class" not in sql
    # every ALTER TABLE ... ADD COLUMN sits inside a guarded DO block
    for m in re.finditer(r"ALTER TABLE \w+\s+ADD COLUMN", sql):
        before = sql[:m.start()]
        assert before.rfind("DO $$") > before.rfind("END $$;"), sql[m.start():m.start() + 60]


def _fetch_for(stored_sha, has_metadata=True, views_version=None):
    def fetch(sql):
        if "to_regclass('public.metadata')" in sql:
            return (has_metadata,)
        if "schema_ddl_sha" in sql:
            return (stored_sha,) if stored_sha else None
        if "views_version" in sql:
            return (views_version or str(postgres.SCHEMA_VERSION),)
        return None
    return fetch


def _ran_schema(conn):
    return any(sql is postgres.SCHEMA_SQL for sql, _ in conn.executed)


def _no_backfills(monkeypatch):
    monkeypatch.setattr(SessionArchive, "run_backfills", lambda self, **k: {})


def test_initialize_skips_ddl_when_fingerprint_matches(monkeypatch):
    _no_backfills(monkeypatch)
    conn = _Conn(_fetch_for(postgres.schema_fingerprint()))
    _archive(conn).initialize()
    assert not _ran_schema(conn)
    assert not any("ALTER" in sql or "CREATE" in sql for sql, _ in conn.executed)


def test_initialize_runs_ddl_when_schema_changed(monkeypatch):
    _no_backfills(monkeypatch)
    conn = _Conn(_fetch_for("stale-sha"))
    _archive(conn).initialize()
    assert _ran_schema(conn)
    stored = [p for sql, p in conn.executed if "'schema_ddl_sha'" in sql and "INSERT" in sql]
    assert stored == [(postgres.schema_fingerprint(),)]


def test_initialize_runs_ddl_on_a_fresh_database(monkeypatch):
    _no_backfills(monkeypatch)
    conn = _Conn(_fetch_for(None, has_metadata=False))
    _archive(conn).initialize()
    assert _ran_schema(conn)


def test_initialize_force_ddl(monkeypatch):
    _no_backfills(monkeypatch)
    conn = _Conn(_fetch_for(postgres.schema_fingerprint()))
    _archive(conn).initialize(force_ddl=True)
    assert _ran_schema(conn)


def test_sweep_interval_matches_hourly_plist():
    from claude_session_db import cli
    plist = (Path(__file__).resolve().parent.parent / "launchd"
             / "com.claude-session-db.sweep.plist").read_text()
    assert cli.SWEEP_INTERVAL_S == 3600
    assert "<key>StartCalendarInterval</key>" in plist
    assert re.search(r"<key>Minute</key>\s*<integer>17</integer>", plist)
    assert "<key>StartInterval</key>" not in plist
    assert "--min-idle" in plist
