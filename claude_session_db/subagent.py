"""Subagent and external tool result discovery.

Discovers sidechain JSONL files and external tool result files
within session directories.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class SubagentInfo:
    """Discovered subagent file."""

    agent_id: str  # 7-char ID (e.g., "a582ac1")
    file_path: Path  # Full path to agent-{id}.jsonl


@dataclass
class ExternalToolResult:
    """Tool result stored in a separate file (overflow from JSONL)."""

    tool_use_id: str  # Matches content_blocks.tool_use_id
    file_path: Path
    content: Optional[str] = None  # Loaded lazily


def discover_subagents(session_dir: Path) -> list[SubagentInfo]:
    """Find all subagent JSONL files for a session.

    Subagent files live in {session-dir}/subagents/agent-{7-char-id}.jsonl.
    """
    subagent_dir = session_dir / "subagents"
    if not subagent_dir.exists():
        return []
    return [
        SubagentInfo(
            agent_id=f.stem.replace("agent-", ""),
            file_path=f,
        )
        for f in sorted(subagent_dir.glob("agent-*.jsonl"))
    ]


def discover_external_tool_results(session_dir: Path) -> list[ExternalToolResult]:
    """Find external tool result files for a session.

    When tool results exceed ~30K chars, Claude Code writes them to
    {session-dir}/tool-results/{hash}.txt. The hash is a truncated
    identifier that can be matched back to tool_use_id.
    """
    results_dir = session_dir / "tool-results"
    if not results_dir.exists():
        return []
    return [
        ExternalToolResult(
            tool_use_id=f.stem,  # Filename without extension
            file_path=f,
        )
        for f in sorted(results_dir.glob("*.txt"))
    ]


def load_external_tool_results(session_dir: Path) -> dict[str, str]:
    """Load all external tool results into a dict keyed by filename stem.

    Returns {stem: content} for use during sync to augment truncated
    tool results with full content from overflow files.
    """
    results = {}
    for ext_result in discover_external_tool_results(session_dir):
        try:
            results[ext_result.tool_use_id] = ext_result.file_path.read_text()
        except (OSError, UnicodeDecodeError):
            pass  # Skip unreadable files
    return results
