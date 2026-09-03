"""Overflow tool-result discovery: `.txt` AND `.json`, top level only.

`discover_external_tool_results` globbed `*.txt`. Claude Code v2.1.161-258
started writing STRUCTURED overflow — a JSON array of content blocks — to
`tool-results/<tool_use_id>.json`, and a 30-day scan found 230 such files (226
with `toolu_*` stems) beside 851 `.txt` ones. Every one of those results was in
the archive as its inline TRUNCATION while the verbatim copy sat unread on
disk, which is a direct breach of the "no truncation" invariant.

The other half of this file is what must NOT be picked up. `tool-results/` also
accumulates WebFetch `.pdf` downloads, `pdf-<uuid>/page-N.jpg` document
renders, and agent working directories (`extracted/`, `data/`) whose filenames
are content names, not tool_use_ids. Hoovering those in would key arbitrary
files onto whatever tool_use_id collided with their stem.
"""
from __future__ import annotations

import json

from claude_session_db.subagent import (
    OVERFLOW_SUFFIXES,
    discover_external_tool_results,
    load_external_tool_results,
)


def _session(tmp_path):
    d = tmp_path / "sess"
    (d / "tool-results").mkdir(parents=True)
    return d


def test_json_overflow_is_discovered(tmp_path):
    """The regression this file exists for."""
    d = _session(tmp_path)
    body = json.dumps([{"type": "text", "text": "x" * 5000}])
    (d / "tool-results" / "toolu_01PvgHdTmUFce94XsMhNqLFd.json").write_text(body)
    found = {r.tool_use_id: r for r in discover_external_tool_results(d)}
    assert "toolu_01PvgHdTmUFce94XsMhNqLFd" in found
    assert load_external_tool_results(d)["toolu_01PvgHdTmUFce94XsMhNqLFd"] == body


def test_txt_overflow_still_works(tmp_path):
    """851 of the 1,081 overflow files are .txt — widening must not narrow."""
    d = _session(tmp_path)
    (d / "tool-results" / "mcp-claude-ai-kmcp-search-abc.txt").write_text("hello")
    assert load_external_tool_results(d) == {"mcp-claude-ai-kmcp-search-abc": "hello"}


def test_json_content_is_verbatim_not_rerendered(tmp_path):
    """Lossless means the JSON text, not a flattening of it."""
    d = _session(tmp_path)
    blocks = [{"type": "text", "text": "alpha"}, {"type": "text", "text": "beta"}]
    raw = json.dumps(blocks, indent=2)
    (d / "tool-results" / "toolu_x.json").write_text(raw)
    assert load_external_tool_results(d)["toolu_x"] == raw


def test_pdf_downloads_are_not_picked_up(tmp_path):
    """Binary, and `webfetch-<ts>-<rand>` is not a tool_use_id."""
    d = _session(tmp_path)
    (d / "tool-results" / "webfetch-1786977653876-g4p3c7.pdf").write_bytes(b"%PDF-1.4")
    assert discover_external_tool_results(d) == []


def test_pdf_page_renders_are_not_picked_up(tmp_path):
    """`pdf-<uuid>/page-N.jpg` are RENDERS of a document, not tool results."""
    d = _session(tmp_path)
    pages = d / "tool-results" / "pdf-b39ff81c-7f7c-47ed-b8c9-5eff2732d5c0"
    pages.mkdir()
    (pages / "page-1.jpg").write_bytes(b"\xff\xd8\xff")
    (pages / "page-2.jpg").write_bytes(b"\xff\xd8\xff")
    assert discover_external_tool_results(d) == []


def test_agent_working_dirs_are_not_picked_up(tmp_path):
    """`extracted/` and `data/` hold content-named files an agent wrote. Their
    stems are not tool_use_ids, so recursing would bind arbitrary files to
    whatever tool_use_id shares their name. Discovery is non-recursive."""
    d = _session(tmp_path)
    for sub, name in (("extracted", "standards_path-conventions.txt"),
                      ("data", "notes.json")):
        (d / "tool-results" / sub).mkdir()
        (d / "tool-results" / sub / name).write_text("not an overflow result")
    assert discover_external_tool_results(d) == []


def test_both_extensions_for_one_stem_take_the_longer(tmp_path):
    d = _session(tmp_path)
    (d / "tool-results" / "toolu_dup.txt").write_text("short")
    (d / "tool-results" / "toolu_dup.json").write_text("a much longer body indeed")
    assert load_external_tool_results(d)["toolu_dup"] == "a much longer body indeed"


def test_missing_dir_is_not_an_error(tmp_path):
    assert discover_external_tool_results(tmp_path / "nope") == []
    assert load_external_tool_results(tmp_path / "nope") == {}


def test_unreadable_file_is_skipped_not_fatal(tmp_path):
    """A binary blob that sneaks past the suffix filter must not kill a sync."""
    d = _session(tmp_path)
    (d / "tool-results" / "toolu_bad.txt").write_bytes(b"\xff\xfe\x00\x00binary")
    (d / "tool-results" / "toolu_good.txt").write_text("fine")
    loaded = load_external_tool_results(d)
    assert loaded.get("toolu_good") == "fine"


def test_suffix_list_is_explicit():
    assert OVERFLOW_SUFFIXES == (".txt", ".json")
