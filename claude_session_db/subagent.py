"""Subagent and external tool result discovery.

Discovers sidechain JSONL files, their meta.json sidecars, and external tool
result files within session directories.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class SubagentInfo:
    """Discovered subagent file."""

    agent_id: str  # 17-hex ID (e.g., "a0ae7c4c583360944")
    file_path: Path  # Full path to agent-{id}.jsonl


@dataclass
class ExternalToolResult:
    """Tool result stored in a separate file (overflow from JSONL)."""

    tool_use_id: str  # Matches content_blocks.tool_use_id
    file_path: Path
    content: Optional[str] = None  # Loaded lazily


def read_agent_meta(agent_jsonl: Path) -> dict:
    """Read the sidecar next to an agent-<id>.jsonl transcript.

    Each sidechain file has an adjacent `agent-<id>.meta.json` carrying
    {"agentType", "description", "toolUseId", "spawnDepth"[, "parentAgentId"]}.
    Returns {} when absent or unparsable (older sessions lack the sidecar).
    """
    meta_path = agent_jsonl.with_name(agent_jsonl.stem + ".meta.json")
    try:
        data = json.loads(meta_path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def discover_subagents(session_dir: Path) -> list[SubagentInfo]:
    """Find all subagent JSONL files for a session.

    Subagent files live at `{session-dir}/subagents/agent-{17-hex-id}.jsonl`,
    and workflow agents NEST at
    `{session-dir}/subagents/workflows/wf_*/agent-*.jsonl`.

    RECURSIVE, deliberately. This used to be a flat `glob("agent-*.jsonl")`,
    which silently returned zero of the nested workflow agents. It was a latent
    trap rather than a live bug — `SessionSync.enumerate_files` walks with
    `rglob` and finds them, so the archive never actually missed one — but the
    two discoverers disagreeing about what a session's subagents ARE is the
    kind of divergence that becomes a real bug the moment anything else calls
    this. Both now use the same rule.
    """
    subagent_dir = session_dir / "subagents"
    if not subagent_dir.exists():
        return []
    return [
        SubagentInfo(
            agent_id=f.stem.replace("agent-", ""),
            file_path=f,
        )
        for f in sorted(subagent_dir.rglob("agent-*.jsonl"))
    ]


# Overflow extensions we archive, keyed on the filename STEM (= tool_use_id).
#
#   .txt   the original plain-text overflow (851 files in a 30-day scan)
#   .json  structured overflow — a JSON array of content blocks, written when
#          the oversized result is BLOCK-shaped rather than a text blob (230
#          files, 226 of them `toolu_*`). Added by Claude Code in the
#          v2.1.161-258 window; csd globbed `*.txt` only and archived none of
#          them, so those 230 results existed in the archive as their inline
#          TRUNCATION with the verbatim copy sitting unread on disk.
#
# Deliberately NOT picked up:
#   *.pdf                    WebFetch downloads (`webfetch-<ts>-<rand>.pdf`).
#                            Binary, and the stem is not a tool_use_id, so
#                            there is nothing to key them to.
#   pdf-<uuid>/page-N.jpg    per-page RENDERS of a document, not a tool result.
#                            Binary; the tool result that produced them is
#                            already archived.
#   extracted/, data/        agent WORKING directories that happen to live
#                            under tool-results/. Inspected: their stems are
#                            content names (`standards_path-conventions.txt`,
#                            `data_fire-gas.txt`), not tool_use_ids, so
#                            hoovering them in would key arbitrary files onto
#                            whatever tool_use_id happened to collide. The
#                            discovery is therefore NON-recursive by design.
OVERFLOW_SUFFIXES = (".txt", ".json")


def discover_external_tool_results(session_dir: Path) -> list[ExternalToolResult]:
    """Find external tool result files for a session.

    When a tool result exceeds the inline cap, Claude Code writes it whole to
    `{session-dir}/tool-results/{tool_use_id}.{txt,json}`. The filename stem is
    the identifier matched back to `content_blocks.tool_use_id`.

    Top level only — see OVERFLOW_SUFFIXES for what is excluded and why.
    """
    results_dir = session_dir / "tool-results"
    if not results_dir.exists():
        return []
    return [
        ExternalToolResult(
            tool_use_id=f.stem,  # Filename without extension
            file_path=f,
        )
        for f in sorted(results_dir.iterdir())
        if f.is_file() and f.suffix in OVERFLOW_SUFFIXES
    ]


def load_external_tool_results(session_dir: Path) -> dict[str, str]:
    """Load all external tool results into a dict keyed by filename stem.

    Returns {stem: content} for use during sync to augment truncated
    tool results with full content from overflow files.

    Content is kept VERBATIM, including for `.json` overflow — a JSON array of
    content blocks is stored as the JSON text it is, not re-rendered. That is
    what "lossless" means here, and the caller only substitutes it when it is
    longer than the inline copy, so a `.json` file never shortens a result.

    A stem carried by BOTH extensions resolves to the longer content: the
    suffix ordering is an implementation detail, and taking the larger one is
    the same rule the sync path already applies.
    """
    results: dict[str, str] = {}
    for ext_result in discover_external_tool_results(session_dir):
        try:
            content = ext_result.file_path.read_text()
        except (OSError, UnicodeDecodeError):
            continue  # Skip unreadable files
        prev = results.get(ext_result.tool_use_id)
        if prev is None or len(content) > len(prev):
            results[ext_result.tool_use_id] = content
    return results
