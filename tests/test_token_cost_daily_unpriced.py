"""`v_token_cost_daily` says how much of the day it could not price.

An unpriced row — a model with no `model_pricing` pattern (a non-Anthropic
model, or a new Claude family before its rates are seeded) — yields NULL cost
terms, and `sum()` silently skips NULLs. So the daily view read as a complete
total while quietly omitting spend. `v_token_cost_by_model` and
`v_session_cost_drift` both already carried `unpriced_messages`; the daily
rollup, which is what `csd usage` reports, did not.

No cost arithmetic changes here — only the counts.
"""
from __future__ import annotations

import re

from claude_session_db import postgres


def _daily():
    v = postgres.VIEWS_SQL
    body = v.split("CREATE VIEW v_token_cost_daily AS", 1)[1]
    return body.split(";", 1)[0]


def test_the_view_reports_priced_and_unpriced_counts():
    d = _daily()
    assert "count(*) FILTER (WHERE unpriced) AS unpriced_messages" in d
    assert "count(*) FILTER (WHERE NOT unpriced) AS priced_messages" in d
    assert "count(*) AS messages" in d


def test_it_matches_the_by_model_view():
    """The two rollups must agree about what an unpriced row is."""
    by_model = postgres.VIEWS_SQL.split("CREATE OR REPLACE VIEW v_token_cost_by_model AS", 1)[1]
    assert "count(*) FILTER (WHERE unpriced) AS unpriced_messages" in by_model.split(";", 1)[0]


def test_the_view_is_dropped_before_it_is_recreated():
    """CREATE OR REPLACE cannot add columns to an existing view definition."""
    v = postgres.VIEWS_SQL
    assert "DROP VIEW IF EXISTS v_token_cost_daily;" in v
    assert v.index("DROP VIEW IF EXISTS v_token_cost_daily;") < \
        v.index("CREATE VIEW v_token_cost_daily AS")
    assert "CREATE OR REPLACE VIEW v_token_cost_daily" not in v


def test_no_cost_arithmetic_changed():
    d = _daily()
    assert "round(sum(input_cost), 4)" in d
    assert "round(sum(cache_write_5m_cost + cache_write_1h_cost), 4)" in d
    assert "round(sum(cache_read_cost), 4)" in d
    assert "round(sum(output_cost), 4)" in d
    assert "round(sum(total_cost), 4)" in d


def test_the_existing_columns_are_untouched():
    d = _daily()
    for col in ("day", "sessions", "input_cost", "cache_write_cost",
                "cache_read_cost", "output_cost", "total_cost"):
        assert re.search(rf"\bAS {col}\b", d) or f" {col} " in d or col in d


def test_the_unpriced_flag_still_comes_from_v_message_cost():
    assert "(input_per_mtok IS NULL) AS unpriced" in postgres.VIEWS_SQL


def test_write_untiered_tokens_is_named_in_the_message_cost_comment():
    """The lump-cache_creation term had a name in the SQL and none in the
    comment that explained it, so the comment could not be matched to a column."""
    header = postgres.VIEWS_SQL.split("CREATE OR REPLACE VIEW v_message_cost AS", 1)[0]
    tail = header.rsplit("v_message_cost is the reusable per-message base", 1)[-1]
    assert "`write_untiered_tokens`" in tail
    assert "at the 5m rate" in tail
