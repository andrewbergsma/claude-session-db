"""Model-pricing seed: coverage, precedence, and the rates themselves.

Why this file exists
--------------------
`v_message_cost` joins each assistant message to `model_pricing` with

    WHERE m.model LIKE mp.model_pattern || '%'
    ORDER BY length(mp.model_pattern) DESC LIMIT 1

so a model with no matching pattern is `unpriced` — it does not error, it
silently drops out of every cost rollup. A 30-day scan found 234K assistant
messages on the Claude 5 family in exactly that state. Nothing in the suite
would have caught it, because the seed is data, not code.

These tests are DB-free: the seed rows are parsed out of `SCHEMA_SQL` and the
view's own resolution rule (longest matching prefix wins) is re-implemented
here in four lines. That keeps them honest about what the view does while
staying runnable with no Postgres.
"""
from __future__ import annotations

import re

import pytest

from claude_session_db import postgres


# Models observed in the live corpus (30-day scan, 2026-09-02). Every one of
# these MUST resolve to a pricing row.
LIVE_MODELS = [
    "claude-opus-5",
    "claude-fable-5",
    "claude-sonnet-5",
    "claude-fable-5-1",
    "claude-opus-4-8",
    "claude-haiku-4-5-20251001",
    "claude-opus-4-7",
    # the long-context variant as it appears in cost-state modelUsage
    "claude-opus-5[1m]",
]


def _seeded_patterns() -> dict[str, dict]:
    """{model_pattern: {input, output, cache_read_mult}} parsed from SCHEMA_SQL.

    The seed is written as literal INSERT tuples; two forms ship (with and
    without an explicit cache_read_mult column), so both are matched and the
    table default (0.10) is applied where the column is absent.
    """
    sql = postgres.SCHEMA_SQL
    out: dict[str, dict] = {}
    # ('pattern', in, out, [read_mult,] 'date', 'notes')
    row_re = re.compile(
        r"\(\s*'(?P<pat>claude-[^']+)'\s*,\s*"
        r"(?P<inp>[\d.]+)\s*,\s*(?P<outp>[\d.]+)\s*,\s*"
        r"(?:(?P<read>[\d.]+)\s*,\s*)?"
        r"'(?P<date>\d{4}-\d{2}-\d{2})'"
    )
    for m in row_re.finditer(sql):
        out[m.group("pat")] = {
            "input": float(m.group("inp")),
            "output": float(m.group("outp")),
            "cache_read_mult": float(m.group("read")) if m.group("read") else 0.10,
        }
    return out


def _resolve(model: str, patterns: dict[str, dict]) -> tuple[str, dict] | None:
    """The view's rule: LIKE prefix match, longest pattern wins."""
    hits = [p for p in patterns if model.startswith(p)]
    if not hits:
        return None
    best = max(hits, key=len)
    return best, patterns[best]


def test_seed_parses():
    pats = _seeded_patterns()
    assert len(pats) >= 14, f"seed parse looks wrong: {sorted(pats)}"


@pytest.mark.parametrize("model", LIVE_MODELS)
def test_every_live_model_is_priced(model):
    """No model in the live corpus may fall through to `unpriced`."""
    hit = _resolve(model, _seeded_patterns())
    assert hit is not None, f"{model} matches NO model_pricing pattern -> unpriced"


def test_fable_5_1_is_not_shadowed_by_fable_5():
    """The length ordering must pick the MORE specific pattern.

    `claude-fable-5-1` is matched by both `claude-fable-5` (14 chars) and
    `claude-fable-5-1` (16). If the shorter one won, 5.1's measured 0.025x
    cache-read multiplier would silently become 0.10x — a ~4x over-report on
    the dominant token bucket of an agentic transcript.
    """
    pats = _seeded_patterns()
    pat, row = _resolve("claude-fable-5-1", pats)
    assert pat == "claude-fable-5-1"
    assert row["cache_read_mult"] == 0.025
    # ...and plain fable-5 must NOT inherit 5.1's cheaper cache reads.
    pat5, row5 = _resolve("claude-fable-5", pats)
    assert pat5 == "claude-fable-5"
    assert row5["cache_read_mult"] == 0.10


def test_opus_4_8_is_not_shadowed_by_opus_4():
    """Opus 4.6/4.7/4.8 bill at 5/25, not the 15/75 of the generic Opus 4.x row."""
    pats = _seeded_patterns()
    pat, row = _resolve("claude-opus-4-8", pats)
    assert pat == "claude-opus-4-8"
    assert (row["input"], row["output"]) == (5.0, 25.0)
    # the generic row is still there for Opus 4.0-4.5
    assert (pats["claude-opus-4"]["input"], pats["claude-opus-4"]["output"]) == (15.0, 75.0)


@pytest.mark.parametrize("model,expected", [
    ("claude-opus-5", (5.0, 25.0, 0.10)),
    ("claude-opus-5[1m]", (5.0, 25.0, 0.10)),
    ("claude-sonnet-5", (2.0, 10.0, 0.10)),
    ("claude-fable-5", (10.0, 50.0, 0.10)),
    ("claude-fable-5-1", (10.0, 50.0, 0.025)),
])
def test_claude_5_rates_match_the_cost_state_solve(model, expected):
    """Rates solved from Claude Code's own `cost-state` ledger.

    Least squares over per-model {input, output, cacheRead, cacheCreation,
    costUSD} observations reproduced sonnet-5 (16/16) and fable-5-1 (7/7)
    exactly; opus-5 checks out on a zero-cache-read row by hand
    (705*5 + 73*25 + 90544*6.25 = 571250 micro-USD = $0.571250, the recorded value).
    """
    _, row = _resolve(model, _seeded_patterns())
    assert (row["input"], row["output"], row["cache_read_mult"]) == expected


def test_view_resolves_by_longest_pattern():
    """Guard the ordering clause itself — the test above is only true because
    of it."""
    assert "ORDER BY length(mp.model_pattern) DESC" in postgres.VIEWS_SQL


def test_seed_is_idempotent():
    """Re-running initialize() must never clobber a manually edited rate."""
    # NB: notes text may contain ';', so match through to the conflict clause
    # rather than to the first semicolon.
    starts = postgres.SCHEMA_SQL.count("INSERT INTO model_pricing")
    guarded = len(re.findall(
        r"INSERT INTO model_pricing.*?ON CONFLICT \(model_pattern\) DO NOTHING;",
        postgres.SCHEMA_SQL, re.S))
    assert starts > 0, "no model_pricing seed found"
    assert guarded == starts, (
        f"{starts} model_pricing INSERTs but only {guarded} are ON CONFLICT DO NOTHING")
