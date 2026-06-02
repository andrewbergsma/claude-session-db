"""Heuristic tool-result tldr — deterministic, free, zero model calls.

The archive stores tool results verbatim (`tool_results.content_text`); the
nullable `tldr` sibling is meant to carry a compressed, signal-preserving
rendering for summarization contexts. This module produces that rendering with
pure heuristics — the cheap arm of the tldr A/B (the expensive arm is a haiku
call per result).

Thesis (claudecode:design/session-archive-and-recompact): tool-result BODIES are
~80% of transcript bytes but near-useless for a summary; the head line + an error
signal + a size tag preserve what a summary actually needs.

The result body is the only input — the tool CALL and its INPUT are kept verbatim
by the digest, so the tldr need only compress the RESULT.
"""
from __future__ import annotations

import re

# Lines that are pure noise in a result head (ANSI, separators, blank prompts).
_NOISE = re.compile(r"^[\s\-=_*·•]+$")
# A line that looks like the operative error (stack-trace tail, exception, etc.).
_ERRORISH = re.compile(
    r"error|exception|failed|fatal|denied|not found|no such|exit code|traceback|"
    r"refused|timed out|invalid|cannot|unable",
    re.I,
)


def _clean_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not _NOISE.match(ln.strip())]


def tldr_result(
    content_text: str | None,
    *,
    is_error: bool = False,
    error_class: str | None = None,
    max_len: int = 200,
) -> str:
    """Compress a tool-result body to a one-line, signal-preserving tldr.

    - Errors: `[ERR:class] <head> … <operative error line>` — the error class plus
      the line that actually names the failure (often the trace tail, not the head).
    - Non-errors: the first meaningful line plus a `(+Nc)` size tag so the reader
      knows how much body was elided.
    """
    text = (content_text or "").strip()
    if not text:
        return "(empty result)"
    lines = _clean_lines(text)
    if not lines:
        return f"({len(text)}c, no text lines)"
    head = lines[0]
    total = len(text)

    if is_error:
        # The operative message is often NOT the first line (stack traces bury it
        # at the tail). Prefer the last error-ish line; fall back to the head.
        operative = next((ln for ln in reversed(lines) if _ERRORISH.search(ln)), "")
        prefix = f"[ERR:{error_class or 'unknown'}] "
        if operative and operative != head:
            body = f"{head[:90]} … {operative[:90]}"
        else:
            body = head
        return (prefix + body)[:max_len]

    # Non-error: head line + how much was dropped.
    summary = head[:max_len]
    dropped = total - len(head)
    if dropped > 40:
        summary = f"{summary} (+{dropped}c)"
    return summary
