#!/usr/bin/env python3
"""
Analyze Claude Code JSONL transcripts for failure patterns and context waste.

Extracts four machine-detectable signals from ~/.claude/projects/<project>/*.jsonl:
  1. Bash failures     – exit codes and command patterns
  2. Tool errors       – is_error=True, classified by error type
  3. Context absorption – bytes consumed per tool type
  4. Code churn        – files edited 3+ times in a single session

Usage:
    python3 scripts/transcript_analyzer.py
    python3 scripts/transcript_analyzer.py --project-dir ~/.claude/projects/-Users-andrew-GitHub-knowledge
    python3 scripts/transcript_analyzer.py --top 15 --min-churn 4 --output json
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ─── Data models ──────────────────────────────────────────────────────────────

@dataclass
class ToolCall:
    tool_name: str
    result_size: int
    is_error: bool


@dataclass
class BashFailure:
    session_id: str
    command: str
    exit_code: int
    output_snippet: str


@dataclass
class ToolError:
    session_id: str
    tool_name: str
    error_type: str
    content_snippet: str


@dataclass
class FileEdit:
    file_path: str


@dataclass
class SessionStats:
    session_id: str
    path: str
    tool_calls: list = field(default_factory=list)
    bash_failures: list = field(default_factory=list)
    tool_errors: list = field(default_factory=list)
    file_edits: list = field(default_factory=list)
    parallel_cancelled: int = 0  # cascade noise — counted separately, not in tool_errors


# ─── Parsing ──────────────────────────────────────────────────────────────────

ERROR_PATTERNS = [
    (re.compile(r"session not found", re.I), "mcp_session_timeout"),
    (re.compile(r"token expired|re-authorization", re.I), "auth_expired"),
    # Spaced form ("Input validation error") is what kmcp actually emits; the old
    # no-space regex missed every live case.
    (re.compile(r"input ?validation ?error", re.I), "schema_violation"),
    (re.compile(r"cancelled: parallel tool call", re.I), "parallel_cancelled"),
    (re.compile(r"blocked:", re.I), "hook_blocked"),
    # Read refusing an oversized file ("exceeds maximum allowed tokens/size") —
    # high-frequency and cleanly recoverable via offset/limit.
    (re.compile(r"exceeds maximum allowed (tokens|size)", re.I), "read_too_large"),
    (re.compile(r"timed out|the operation timed out", re.I), "timeout"),
    (re.compile(r"no such tool available", re.I), "phantom_tool"),
    (re.compile(r"file has not been read yet", re.I), "edit_without_read"),
    (re.compile(r"file has been modified since read", re.I), "edit_stale_read"),
    (re.compile(r"file does not exist|no such file", re.I), "file_not_found"),
    # Shell command-not-found (exit 127) — distinct from a phantom MCP tool.
    (re.compile(r"command not found|exit code 127", re.I), "command_not_found"),
    # MCP/entry lookup miss (kmcp "No entry found", JSON "Not found") — distinct
    # from a filesystem file_not_found.
    (re.compile(r"no entry found|\"not found\"", re.I), "not_found"),
    (re.compile(r"already exists", re.I), "entry_exists"),
    (re.compile(r"user doesn.t want to proceed|was rejected", re.I), "user_rejected"),
    (re.compile(r"permission denied", re.I), "permission_denied"),
    (re.compile(r"connection refused|connection reset", re.I), "connection_error"),
    # Generic non-zero Bash exit — LAST, so a more specific signal in the body
    # (file_not_found, command_not_found, etc.) always wins first.
    (re.compile(r"\bexit code [1-9]", re.I), "bash_nonzero_exit"),
]


def classify_error(tool_name: str, content: str) -> str:
    for pattern, label in ERROR_PATTERNS:
        if pattern.search(content):
            # phantom_tool: only valid when the error names the tool that was called.
            # Bash can print "No such tool available: BashOutput" in its stdout, which is
            # not a phantom Bash call — it's data produced by the script.
            if label == "phantom_tool" and tool_name == "Bash":
                continue
            return label
    return "unknown"


def parse_session(path: Path) -> Optional[SessionStats]:
    session_id = path.stem
    stats = SessionStats(session_id=session_id, path=str(path))

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except (IOError, PermissionError):
        return None

    tool_uses = {}  # id → {name, input}

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        rec_type = obj.get("type")

        if rec_type == "assistant":
            for block in obj.get("message", {}).get("content", []):
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool_uses[block["id"]] = {
                    "name": block["name"],
                    "input": block.get("input", {}),
                }

        elif rec_type == "user":
            for block in obj.get("message", {}).get("content", []):
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue

                tid = block.get("tool_use_id")
                if not tid or tid not in tool_uses:
                    continue

                tool = tool_uses[tid]
                name = tool["name"]
                inp = tool["input"]
                content = block.get("content", "")
                if not isinstance(content, str):
                    content = json.dumps(content)
                is_error = block.get("is_error", False)

                stats.tool_calls.append(ToolCall(
                    tool_name=name,
                    result_size=len(content),
                    is_error=is_error,
                ))

                # Bash exit code detection (not is_error — exit codes come as content text)
                if name == "Bash":
                    exit_match = re.match(r"Exit code (\d+)", content)
                    if exit_match:
                        code = int(exit_match.group(1))
                        cmd = re.sub(r"\s+", " ", inp.get("command", "")).strip()[:120]
                        snippet = content[exit_match.end():].strip()[:200].replace("\n", " ")
                        stats.bash_failures.append(BashFailure(
                            session_id=session_id,
                            command=cmd,
                            exit_code=code,
                            output_snippet=snippet,
                        ))

                # Tool error detection (is_error=True)
                if is_error:
                    # Bash exit codes are already captured above; skip double-counting
                    if name == "Bash" and re.match(r"Exit code \d+", content):
                        pass
                    else:
                        error_type = classify_error(name, content)
                        if error_type == "parallel_cancelled":
                            # Cascade noise: sibling of a failing parallel call.
                            # Count separately so they don't inflate the error table.
                            stats.parallel_cancelled += 1
                        else:
                            stats.tool_errors.append(ToolError(
                                session_id=session_id,
                                tool_name=name,
                                error_type=error_type,
                                content_snippet=content[:200],
                            ))

                # Edit/Write churn tracking
                if name in ("Edit", "Write"):
                    fp = inp.get("file_path", inp.get("path", ""))
                    if fp:
                        stats.file_edits.append(FileEdit(file_path=fp))

    return stats


# ─── Report rendering ─────────────────────────────────────────────────────────

def shorten_tool(name: str) -> str:
    return (
        name
        .replace("mcp__claude_ai_kmcp-personal__", "kmcp::")
        .replace("mcp__", "mcp::")
    )


def shorten_path(path: str) -> str:
    return path.replace(str(Path.home()), "~")


def render_text(sessions: list, top: int, min_churn: int) -> str:
    out = []

    all_tool_calls = [tc for s in sessions for tc in s.tool_calls]
    all_bash_failures = [bf for s in sessions for bf in s.bash_failures]
    all_tool_errors = [te for s in sessions for te in s.tool_errors]
    total_cancelled = sum(s.parallel_cancelled for s in sessions)

    out.append("═" * 72)
    out.append(
        f"Transcript Analysis  ·  {len(sessions)} sessions  "
        f"·  {len(all_tool_calls)} tool calls  "
        f"·  {len(all_bash_failures)} bash failures  "
        f"·  {len(all_tool_errors)} tool errors"
        + (f"  ·  {total_cancelled} parallel-cancelled (suppressed)" if total_cancelled else "")
    )
    out.append("═" * 72)
    out.append("")

    # ── Bash Failures ──
    out.append(f"BASH FAILURES  ({len(all_bash_failures)} total)")
    out.append("─" * 72)

    fail_groups = defaultdict(list)
    for bf in all_bash_failures:
        cmd_key = bf.command[:60]
        fail_groups[(bf.exit_code, cmd_key)].append(bf)

    ranked_fails = sorted(fail_groups.items(), key=lambda x: -len(x[1]))[:top]
    if ranked_fails:
        out.append(f"{'Hits':>4}  {'Code':>4}  Command")
        for (code, cmd), instances in ranked_fails:
            out.append(f"{len(instances):>4}  {code:>4}  {cmd}")
            snippet = instances[0].output_snippet[:80]
            if snippet:
                out.append(f"            ↳ {snippet}")
    else:
        out.append("  (none detected)")
    out.append("")

    # ── Tool Errors ──
    out.append(f"TOOL ERRORS  ({len(all_tool_errors)} total)")
    out.append("─" * 72)

    err_groups = defaultdict(list)
    for te in all_tool_errors:
        err_groups[(te.tool_name, te.error_type)].append(te)

    ranked_errs = sorted(err_groups.items(), key=lambda x: -len(x[1]))[:top]
    if ranked_errs:
        out.append(f"{'Hits':>4}  {'Tool':<45}  Error type")
        for (tool, etype), instances in ranked_errs:
            out.append(f"{len(instances):>4}  {shorten_tool(tool):<45}  {etype}")
    else:
        out.append("  (none detected)")
    out.append("")

    # ── Context Absorption ──
    out.append("CONTEXT ABSORPTION  (bytes consumed per tool)")
    out.append("─" * 72)

    tool_sizes = defaultdict(list)
    for tc in all_tool_calls:
        if tc.result_size > 0:
            tool_sizes[tc.tool_name].append(tc.result_size)

    tool_totals = {
        name: (len(sizes), sum(sizes) // len(sizes), sum(sizes))
        for name, sizes in tool_sizes.items()
    }
    ranked_absorption = sorted(tool_totals.items(), key=lambda x: -x[1][2])[:top]

    if ranked_absorption:
        out.append(f"{'Calls':>6}  {'Avg bytes':>10}  {'Total':>10}  Tool")
        for name, (calls, avg, total) in ranked_absorption:
            if total >= 1024 * 1024:
                total_str = f"{total/1024/1024:.1f}M"
            else:
                total_str = f"{total/1024:.0f}K"
            out.append(f"{calls:>6}  {avg:>10,}  {total_str:>10}  {shorten_tool(name)}")
    out.append("")

    # ── Code Churn ──
    out.append(f"CODE CHURN  (files with {min_churn}+ edits in a single session)")
    out.append("─" * 72)

    # Per session → per file edit counts
    file_churn: dict = defaultdict(list)  # file_path → [(session_id, count)]
    for s in sessions:
        file_counts: dict = defaultdict(int)
        for fe in s.file_edits:
            file_counts[fe.file_path] += 1
        for fp, count in file_counts.items():
            if count >= min_churn:
                file_churn[fp].append((s.session_id, count))

    ranked_churn = sorted(
        file_churn.items(),
        key=lambda x: -max(c for _, c in x[1])
    )[:top]

    if ranked_churn:
        out.append(f"{'Sessions':>8}  {'Max/sess':>8}  File")
        for fp, session_list in ranked_churn:
            max_count = max(c for _, c in session_list)
            out.append(f"{len(session_list):>8}  {max_count:>8}  {shorten_path(fp)}")
    else:
        out.append("  (none detected)")
    out.append("")

    return "\n".join(out)


def render_json(sessions: list, top: int, min_churn: int) -> str:
    all_bash = [bf for s in sessions for bf in s.bash_failures]
    all_errors = [te for s in sessions for te in s.tool_errors]
    all_calls = [tc for s in sessions for tc in s.tool_calls]

    fail_groups: dict = defaultdict(list)
    for bf in all_bash:
        fail_groups[(bf.exit_code, bf.command[:60])].append({
            "session": bf.session_id,
            "snippet": bf.output_snippet[:100],
        })

    err_groups: dict = defaultdict(list)
    for te in all_errors:
        err_groups[(te.tool_name, te.error_type)].append(te.session_id)

    tool_sizes: dict = defaultdict(list)
    for tc in all_calls:
        if tc.result_size > 0:
            tool_sizes[tc.tool_name].append(tc.result_size)

    file_churn: dict = defaultdict(list)
    for s in sessions:
        fc: dict = defaultdict(int)
        for fe in s.file_edits:
            fc[fe.file_path] += 1
        for fp, count in fc.items():
            if count >= min_churn:
                file_churn[fp].append((s.session_id, count))

    report = {
        "summary": {
            "sessions": len(sessions),
            "tool_calls": len(all_calls),
            "bash_failures": len(all_bash),
            "tool_errors": len(all_errors),
            "parallel_cancelled": sum(s.parallel_cancelled for s in sessions),
        },
        "bash_failures": [
            {"exit_code": code, "command": cmd, "hits": len(instances), "sample": instances[0]}
            for (code, cmd), instances in sorted(fail_groups.items(), key=lambda x: -len(x[1]))[:top]
        ],
        "tool_errors": [
            {"tool": tool, "error_type": etype, "hits": len(sids)}
            for (tool, etype), sids in sorted(err_groups.items(), key=lambda x: -len(x[1]))[:top]
        ],
        "context_absorption": [
            {
                "tool": name,
                "calls": len(sizes),
                "avg_bytes": sum(sizes) // len(sizes),
                "total_bytes": sum(sizes),
            }
            for name, sizes in sorted(tool_sizes.items(), key=lambda x: -sum(x[1]))[:top]
        ],
        "code_churn": [
            {
                "file": fp,
                "sessions": len(sl),
                "max_edits_per_session": max(c for _, c in sl),
            }
            for fp, sl in sorted(file_churn.items(), key=lambda x: -max(c for _, c in x[1]))[:top]
        ],
    }
    return json.dumps(report, indent=2)


# ─── Auto-detect project dir ──────────────────────────────────────────────────

def auto_detect_project_dir() -> Optional[Path]:
    """Derive ~/.claude/projects/<slug> from cwd using Claude's path→slug scheme."""
    cwd = Path.cwd().resolve()
    slug = str(cwd).replace("/", "-")
    candidate = Path.home() / ".claude" / "projects" / slug
    if candidate.exists():
        return candidate

    # Try parent directories as fallback (for worktrees inside a project).
    # Stop at or above the home directory — no project root lives there, and
    # the slug for '/' is '-' which matches the real ~/.claude/projects/- dir.
    for parent in cwd.parents:
        if parent == Path.home() or parent in Path.home().parents:
            break
        slug = str(parent).replace("/", "-")
        candidate = Path.home() / ".claude" / "projects" / slug
        if candidate.exists():
            return candidate

    return None


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze Claude Code transcripts for failure patterns and context waste.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--project-dir", "-p",
        help="Path to Claude project directory (auto-detected from cwd if omitted)",
    )
    parser.add_argument(
        "--sessions", "-n",
        type=int,
        default=0,
        help="Limit to N most recently modified sessions (default: all)",
    )
    parser.add_argument(
        "--top", "-t",
        type=int,
        default=20,
        help="Top N items per section (default: 20)",
    )
    parser.add_argument(
        "--min-churn",
        type=int,
        default=3,
        help="Minimum edits per file per session to flag as churn (default: 3)",
    )
    parser.add_argument(
        "--output", "-o",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress progress output",
    )
    args = parser.parse_args()

    # Resolve project dir
    if args.project_dir:
        project_dir = Path(args.project_dir).expanduser()
    else:
        project_dir = auto_detect_project_dir()

    if not project_dir or not project_dir.exists():
        print(
            f"Error: could not find Claude project directory. "
            f"Pass --project-dir explicitly.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not args.quiet:
        print(f"Project dir: {project_dir}", file=sys.stderr)

    # Collect JSONL files
    jsonl_files = sorted(
        project_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if args.sessions:
        jsonl_files = jsonl_files[: args.sessions]

    if not jsonl_files:
        print("No JSONL transcript files found.", file=sys.stderr)
        sys.exit(1)

    if not args.quiet:
        print(f"Parsing {len(jsonl_files)} sessions...", file=sys.stderr)

    # Parse
    sessions = []
    for i, path in enumerate(jsonl_files):
        if not args.quiet and i % 20 == 0:
            print(f"  {i}/{len(jsonl_files)}", file=sys.stderr, end="\r")
        stats = parse_session(path)
        if stats:
            sessions.append(stats)

    if not args.quiet:
        print(f"  {len(sessions)}/{len(jsonl_files)} parsed    ", file=sys.stderr)

    # Render
    if args.output == "json":
        print(render_json(sessions, args.top, args.min_churn))
    else:
        print(render_text(sessions, args.top, args.min_churn))


if __name__ == "__main__":
    main()
