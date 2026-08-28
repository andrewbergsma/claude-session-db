"""Client-side placeholder safety for every parameterized SQL string we ship.

The failure mode this guards (regression from 5a2d462, fixed here): psycopg
interpolates placeholders CLIENT-SIDE, and its parser scans the ENTIRE SQL
string — SQL comments included — whenever params are passed. A lone `%` in an
explanatory `--` comment ("~18% of sessions") therefore raises

    psycopg.ProgrammingError: incomplete placeholder: '%'

on EVERY call, before the query ever reaches Postgres. `mark_summarized` shipped
that way because nothing in the suite executed the statement: the SQL was only
ever asserted against as source text.

Two tests, both running the REAL psycopg parser (never a hand-rolled regex):
  - the mark_summarized path is actually executed end-to-end against a fake
    connection whose cursor parses SQL+params exactly as psycopg would;
  - a repo-wide AST sweep parses every literal SQL string handed to
    execute/executemany WITH parameters, so the next such comment fails here
    instead of in the phase-4 watermark stamp.

Run:  uv run --extra dev pytest tests/test_sql_placeholders.py -q
"""
import ast
import pathlib

import pytest
from psycopg import adapt
from psycopg._queries import PostgresQuery

from claude_session_db import reconcile

PKG = pathlib.Path(reconcile.__file__).parent


try:                                   # psycopg's own placeholder splitter
    from psycopg._queries import _split_query

    def _check_placeholders(sql: str) -> None:
        """Raise exactly as cursor.execute(sql, params) would on a lone percent."""
        _split_query(sql.encode())
except ImportError:                    # pragma: no cover - psycopg moved it

    def _check_placeholders(sql: str) -> None:
        """Same rule by hand: every % must be followed by s, b, t or another %."""
        for i, ch in enumerate(sql):
            if ch == "%" and sql[i + 1:i + 2] not in ("s", "b", "t", "%"):
                raise ValueError(f"lone percent: …{sql[max(0, i - 40):i + 10]}…")


def _parse(sql: str, params) -> bytes:
    """Run psycopg's client-side placeholder pass over `sql` + `params`.

    This is the exact code path `cursor.execute(sql, params)` takes before any
    server round-trip, so it raises the same ProgrammingError the bug did.
    """
    q = PostgresQuery(adapt.Transformer())
    q.convert(sql, params)
    return q.query


# ---------------------------------------------------------------------------
# The path that broke: mark_summarized, actually executed
# ---------------------------------------------------------------------------

class ParsingCursor:
    """A cursor that really parses what it is given, then returns a canned row."""

    def __init__(self, row):
        self._row = row
        self.parsed: list[bytes] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.parsed.append(_parse(sql, params) if params is not None else sql)
        return self

    def fetchone(self):
        return self._row


class ParsingConn:
    def __init__(self, row):
        self.cur = ParsingCursor(row)
        self.commits = 0

    def cursor(self, row_factory=None):
        return self.cur

    def commit(self):
        self.commits += 1


def test_mark_summarized_sql_survives_the_client_side_placeholder_pass():
    row = {"session_id": "sid-1", "state": "summarized",
           "message_count_at_summary": 42, "leaf_uuid_at_summary": "m-42"}
    conn = ParsingConn(row)

    got = reconcile.mark_summarized(conn, "sid-1", "claudecode", "session/x")

    assert got == row
    assert conn.commits == 1
    # Placeholders really were substituted (three $n, none left as %s).
    sql = conn.cur.parsed[0]
    assert b"$1" in sql and b"$2" in sql and b"$3" in sql
    assert b"%s" not in sql
    # The comment's substance survives the fix — it documents why the true
    # message tail is used instead of last_prompt_leaf_uuid.
    assert b"last_prompt_leaf_uuid" in sql and b"261/1432" in sql


def test_mark_summarized_raises_for_an_unknown_session():
    conn = ParsingConn(None)
    with pytest.raises(ValueError, match="not found in archive"):
        reconcile.mark_summarized(conn, "nope", "claudecode", "session/x")


# ---------------------------------------------------------------------------
# Repo-wide: every literal SQL string passed WITH params must parse
# ---------------------------------------------------------------------------

def _module_str_consts(tree: ast.Module) -> dict[str, str]:
    out = {}
    for st in tree.body:
        if (isinstance(st, ast.Assign) and len(st.targets) == 1
                and isinstance(st.targets[0], ast.Name)
                and isinstance(st.value, ast.Constant)
                and isinstance(st.value.value, str)):
            out[st.targets[0].id] = st.value.value
    return out


def _sql_literals(node: ast.AST, consts: dict[str, str]):
    """Best-effort: the string constants making up an execute()'s first arg."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    elif isinstance(node, ast.Name) and node.id in consts:
        yield consts[node.id]
    elif isinstance(node, ast.JoinedStr):        # f-string: check its fixed parts
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                yield v.value
    elif isinstance(node, ast.BinOp):            # "…" + "…"
        yield from _sql_literals(node.left, consts)
        yield from _sql_literals(node.right, consts)


def _parameterized_sql():
    """(file, lineno, sql) for every execute/executemany call given params."""
    for py in sorted(PKG.rglob("*.py")):
        tree = ast.parse(py.read_text(), str(py))
        consts = _module_str_consts(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name not in ("execute", "executemany") or len(node.args) < 2:
                continue                          # no params => % is literal, fine
            for sql in _sql_literals(node.args[0], consts):
                yield py, node.lineno, sql


def test_every_parameterized_sql_literal_has_no_lone_percent():
    """psycopg scans comments too, so this is a whole-string property."""
    found = list(_parameterized_sql())
    assert len(found) > 20, "AST sweep found nothing — it stopped working"

    bad = []
    for py, lineno, sql in found:
        try:
            _check_placeholders(sql)
        except Exception as exc:
            bad.append((py.name, lineno, str(exc)))

    assert not bad, "SQL passed to execute() with params fails psycopg's " \
                    f"client-side parse: {bad}"
