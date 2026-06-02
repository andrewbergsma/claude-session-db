#!/usr/bin/env python3
"""
summary_ab — prepare the two input arms for the session-summary A/B context-reduction test.

Hypothesis: tool_result BODIES are the context bloat (per the design spec, tool results
are 80.8% of context; get_entry alone is 42.7% of tool-result bytes). The messages +
tool CALLS + their INPUTS are the actual signal a summary needs. So:

  Arm B (full)   — the raw JSONL, untouched. The "as normal" baseline.
  Arm A (strip)  — the transcript with tool_result bodies STRIPPED to a size marker.
                   Keeps every user/assistant turn and every tool_use (name + input).
  Arm T (tldr)   — like Arm A, but each stripped body is replaced by a heuristic
                   one-line tldr (claude_session_db.tool_tldr) instead of nothing.
                   The middle ground: a hair more tokens than strip, but the result
                   signal (errors, head line) survives for the summarizer.

This script BUILDS arms A + T and REPORTS token estimates for all three. It does not
call any model and writes nothing to knowledge-mcp — it only prepares inputs for the
summarizer agents and the gold-reference scorer (driven from the Claude Code session).

Design: claudecode:knowledge:design/session-archive-and-recompact (sidecar/redaction thesis)
Sibling: session_digest.py (lossy digest), recompact.py (in-place redactor),
         tool_tldr.py (the heuristic tldr arm)

Usage:
    python3 scripts/summary_ab.py <session-id-or-jsonl> [--out DIR]
    # writes <DIR>/arm_a.txt, arm_t.txt, metrics.json; prints the metrics table
"""
import argparse
import json
import sys
from pathlib import Path

from claude_session_db.tool_tldr import tldr_result
from claude_session_db.transcript_analyzer import classify_error


def find_transcript(sid_or_path: str) -> Path:
    p = Path(sid_or_path).expanduser()
    if p.exists():
        return p
    # treat as a session UUID: locate <uuid>.jsonl under ~/.claude/projects
    root = Path("~/.claude/projects").expanduser()
    hits = list(root.rglob(f"{sid_or_path}.jsonl"))
    if not hits:
        sys.exit(f"NO TRANSCRIPT FOUND for {sid_or_path} under {root}")
    return hits[0]


def load(p: Path):
    out = []
    for line in p.open(encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def text_of(content):
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text", ""))
    return "\n".join(p for p in parts if p)


def est_tokens(chars: int) -> int:
    # chars/4 — rough in absolute terms, but applied identically to both arms so the
    # A:B RATIO (the thing we care about) is honest.
    return chars // 4


def build_arm_a(recs) -> str:
    """Transcript with tool_result BODIES stripped. Everything else kept verbatim."""
    # tool_use_id -> (name, input) for labelling the (now bodiless) result markers
    tu = {}
    for o in recs:
        if o.get("type") == "assistant":
            for b in o.get("message", {}).get("content", []) or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tu[b["id"]] = b.get("name", "?")

    first_ts = next((o.get("timestamp") for o in recs if o.get("timestamp")), "?")
    last_ts = next((o.get("timestamp") for o in reversed(recs) if o.get("timestamp")), "?")

    out = [
        "SESSION (messages + tool calls, result bodies stripped)",
        f"span: {first_ts} -> {last_ts}   ({len(recs)} records)",
        "=" * 72,
    ]

    for o in recs:
        if o.get("isSidechain"):
            continue
        typ = o.get("type")
        content = o.get("message", {}).get("content")

        if typ == "user":
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        name = tu.get(b.get("tool_use_id"), "?")
                        body = b.get("content", "")
                        body = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
                        nbytes = len(body)
                        err = " [ERROR]" if b.get("is_error") else ""
                        out.append(f"    ⇎ result[{name}]{err}: <body stripped, {nbytes}c>")
            else:
                t = text_of(content).strip()
                if t:
                    out.append(f"\n[USER] {t}")

        elif typ == "assistant":
            t = text_of(content).strip()
            if t:
                out.append(f"\n[ASSISTANT] {t}")
            for b in content or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    inp = json.dumps(b.get("input", {}), ensure_ascii=False)
                    out.append(f"  → {b.get('name')}({inp})")

    return "\n".join(out) + "\n"


def build_arm_tldr(recs) -> str:
    """Like Arm A, but each stripped result body becomes a heuristic one-line tldr."""
    tu = {}
    for o in recs:
        if o.get("type") == "assistant":
            for b in o.get("message", {}).get("content", []) or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tu[b["id"]] = b.get("name", "?")

    first_ts = next((o.get("timestamp") for o in recs if o.get("timestamp")), "?")
    last_ts = next((o.get("timestamp") for o in reversed(recs) if o.get("timestamp")), "?")

    out = [
        "SESSION (messages + tool calls, result bodies replaced by heuristic tldr)",
        f"span: {first_ts} -> {last_ts}   ({len(recs)} records)",
        "=" * 72,
    ]

    for o in recs:
        if o.get("isSidechain"):
            continue
        typ = o.get("type")
        content = o.get("message", {}).get("content")

        if typ == "user":
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        name = tu.get(b.get("tool_use_id"), "?")
                        body = b.get("content", "")
                        body = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
                        is_err = bool(b.get("is_error"))
                        ecls = classify_error(name, body) if is_err else None
                        tl = tldr_result(body, is_error=is_err, error_class=ecls)
                        out.append(f"    -> result[{name}]: {tl}")
            else:
                t = text_of(content).strip()
                if t:
                    out.append(f"\n[USER] {t}")

        elif typ == "assistant":
            t = text_of(content).strip()
            if t:
                out.append(f"\n[ASSISTANT] {t}")
            for b in content or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    inp = json.dumps(b.get("input", {}), ensure_ascii=False)
                    out.append(f"  → {b.get('name')}({inp})")

    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session", help="session UUID or path to .jsonl")
    ap.add_argument("--out", default="/tmp/summary_ab", help="output dir (default /tmp/summary_ab/<sid>)")
    args = ap.parse_args()

    t = find_transcript(args.session)
    sid = t.stem
    out_dir = Path(args.out) / sid
    out_dir.mkdir(parents=True, exist_ok=True)

    recs = load(t)
    arm_a = build_arm_a(recs)
    arm_t = build_arm_tldr(recs)
    arm_a_path = out_dir / "arm_a.txt"
    arm_t_path = out_dir / "arm_t.txt"
    arm_a_path.write_text(arm_a, encoding="utf-8")
    arm_t_path.write_text(arm_t, encoding="utf-8")

    raw_bytes = t.stat().st_size
    a_bytes = len(arm_a.encode("utf-8"))
    tl_bytes = len(arm_t.encode("utf-8"))

    def arm(label, path, nbytes):
        return {
            "label": label, "path": str(path), "bytes": nbytes,
            "est_tokens": est_tokens(nbytes),
            "pct_of_full": round(100 * nbytes / raw_bytes, 1) if raw_bytes else None,
        }

    metrics = {
        "session_id": sid,
        "transcript": str(t),
        "records": len(recs),
        "arm_b_full": arm("raw transcript (untouched)", t, raw_bytes),
        "arm_a_strip": arm("messages + tool calls (result bodies stripped)", arm_a_path, a_bytes),
        "arm_t_tldr": arm("messages + tool calls (result bodies -> heuristic tldr)", arm_t_path, tl_bytes),
    }
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    print(f"session   {sid}")
    print(f"records   {len(recs)}")
    print(f"{'arm':<14} {'bytes':>10} {'~tokens':>10} {'%full':>7}   input")
    for key in ("arm_b_full", "arm_a_strip", "arm_t_tldr"):
        m = metrics[key]
        print(f"{key:<14} {m['bytes']:>10} {m['est_tokens']:>10} {m['pct_of_full'] or 0:>6}%   {m['path']}")
    print(f"\nStrip keeps {metrics['arm_a_strip']['pct_of_full']}% of full; "
          f"tldr keeps {metrics['arm_t_tldr']['pct_of_full']}%. "
          f"tldr costs {metrics['arm_t_tldr']['bytes'] - metrics['arm_a_strip']['bytes']:,} bytes "
          f"over strip to retain result signal.")
    print(f"metrics   {out_dir / 'metrics.json'}")


if __name__ == "__main__":
    main()
