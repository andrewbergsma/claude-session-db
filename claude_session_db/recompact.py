#!/usr/bin/env python3
"""
recompact — deterministic, zero-inference transcript redactor for Claude Code.

Shrinks a session's live context surface by stubbing heavy tool_result payloads
*in place*, preserving the structure `claude --resume` needs (uuid/parentUuid DAG,
tool_use↔tool_result pairing, thinking-block signatures). No model is called: this
is the deterministic half of the recompact design — dedup + threshold stubbing only.
The local-LLM TL;DR of residual prose is a separate, later layer.

Design: claudecode:knowledge:design/session-archive-and-recompact
Sibling: transcript_analyzer.py (shares the tool_use→tool_result join pattern)

SAFETY: never mutates the source session. Default is a dry-run measurement.
        --write emits a redacted COPY to an output path (never the original).

Usage:
    python3 scripts/recompact.py <session.jsonl>                  # measure only
    python3 scripts/recompact.py <session.jsonl> --threshold 2000 # stub >2KB results
    python3 scripts/recompact.py <session.jsonl> --keep-recent 8  # protect last 8
    python3 scripts/recompact.py <session.jsonl> --write out.jsonl --output json
"""

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional


# ─── Token surface heuristic ──────────────────────────────────────────────────
# chars/4 is rough in absolute terms but the before/after DELTA is exact: we redact
# the identical block set in both passes, so the heuristic error cancels.
def est_tokens(chars: int) -> int:
    return chars // 4


def content_chars(content) -> int:
    """Context-bytes of a tool_result `content` (string or list of sub-blocks)."""
    if isinstance(content, str):
        return len(content)
    return len(json.dumps(content, ensure_ascii=False))


def human(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}M"
    if n >= 1024:
        return f"{n / 1024:.0f}K"
    return str(n)


# ─── Input-key hint for placeholders ──────────────────────────────────────────
def input_hint(name: str, inp: dict) -> str:
    for key in ("path", "file_path", "entry_path", "query", "pattern", "command", "url"):
        if isinstance(inp, dict) and inp.get(key):
            return str(inp[key])[:60]
    return ""


# ─── Core redaction ───────────────────────────────────────────────────────────
def recompact(records: list, threshold: int, keep_recent: int):
    """
    Mutate `records` in place: stub tool_result content (both the message.content
    block and the top-level toolUseResult mirror) for results that are large or
    duplicates. Returns a stats dict.

    Preserves: uuid, parentUuid, tool_use_id pairing, thinking blocks. Only the
    *content* of a tool_result is replaced — the block itself always survives.
    """
    # Pass 1: map tool_use_id -> {name, input} from assistant records.
    tool_uses = {}
    for obj in records:
        if obj.get("type") != "assistant":
            continue
        for block in obj.get("message", {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_uses[block["id"]] = {"name": block["name"], "input": block.get("input", {})}

    # Pass 2: enumerate tool_result blocks in chain order, with size + content hash.
    # A "site" is a (record, block-index-or-mirror) we can stub.
    results = []  # ordered list of dicts describing each tool_result
    for chain_idx, obj in enumerate(records):
        if obj.get("type") != "user" or obj.get("isSidechain"):
            continue
        content = obj.get("message", {}).get("content")
        if not isinstance(content, list):
            continue
        for bi, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tid = block.get("tool_use_id")
            meta = tool_uses.get(tid, {})
            name = meta.get("name", "?")
            chars = content_chars(block.get("content", ""))
            digest = hashlib.sha1(
                json.dumps(block.get("content", ""), ensure_ascii=False).encode()
            ).hexdigest()
            results.append({
                "obj": obj, "bi": bi, "tid": tid, "name": name,
                "input": meta.get("input", {}), "chars": chars,
                "digest": digest, "chain_idx": chain_idx,
            })

    total = len(results)
    protect_from = total - keep_recent  # index threshold: protect the tail

    # Decide what to stub. Dedup wins regardless of size; threshold for the rest.
    # Recency protection only shields the *threshold* rule, not dedup — a duplicate
    # is redundant even if recent (the first copy carries the bytes).
    seen = {}  # digest -> first chain position label
    stubbed_dup, stubbed_big = [], []
    by_tool = defaultdict(lambda: [0, 0])  # tool -> [count, bytes_saved]

    for i, r in enumerate(results):
        d = r["digest"]
        first = seen.get(d)
        reason = None
        if first is not None and r["chars"] > 0:
            reason = "dup"
        elif r["chars"] >= threshold and i < protect_from:
            reason = "big"
        else:
            seen.setdefault(d, r)
            continue

        hint = input_hint(r["name"], r["input"])
        label = f"{r['name']}{(' ' + hint) if hint else ''}"
        if reason == "dup":
            placeholder = f"[recompact: duplicate of earlier {label} result — {human(r['chars'])} elided]"
            stubbed_dup.append(r)
        else:
            placeholder = f"[recompact: {label} — {human(r['chars'])} elided]"
            stubbed_big.append(r)
            seen.setdefault(d, r)

        # Stub copy 1: the message.content tool_result block.
        block = r["obj"]["message"]["content"][r["bi"]]
        block["content"] = placeholder
        block["_recompact"] = {"original_chars": r["chars"], "reason": reason}
        # Stub copy 2: the top-level toolUseResult mirror (if present).
        if isinstance(r["obj"].get("toolUseResult"), (dict, list, str)):
            r["obj"]["toolUseResult"] = placeholder

        by_tool[r["name"]][0] += 1
        by_tool[r["name"]][1] += r["chars"]

    return {
        "total_results": total,
        "stubbed_dup": len(stubbed_dup),
        "stubbed_big": len(stubbed_big),
        "protected_recent": min(keep_recent, total),
        "by_tool": dict(by_tool),
        "dup_bytes": sum(r["chars"] for r in stubbed_dup),
        "big_bytes": sum(r["chars"] for r in stubbed_big),
    }


# ─── Surface measurement ──────────────────────────────────────────────────────
def context_surface(records: list) -> int:
    """Char-count of the redaction-relevant context surface: message.content of
    every non-sidechain user/assistant record (where tool_results live)."""
    total = 0
    for obj in records:
        if obj.get("type") not in ("user", "assistant") or obj.get("isSidechain"):
            continue
        c = obj.get("message", {}).get("content")
        if c is not None:
            total += len(json.dumps(c, ensure_ascii=False))
    return total


# ─── IO ───────────────────────────────────────────────────────────────────────
def load(path: Path) -> list:
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def dump(records: list, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for obj in records:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ─── Report ───────────────────────────────────────────────────────────────────
def render_text(path, before, after, stats) -> str:
    saved = before - after
    pct = (saved / before * 100) if before else 0
    o = []
    o.append("═" * 72)
    o.append(f"recompact (dry-run measurement)  ·  {Path(path).name}")
    o.append("═" * 72)
    o.append(f"  tool_result blocks : {stats['total_results']}")
    o.append(f"  stubbed (oversize) : {stats['stubbed_big']}   ({human(stats['big_bytes'])})")
    o.append(f"  stubbed (duplicate): {stats['stubbed_dup']}   ({human(stats['dup_bytes'])})")
    o.append(f"  protected (recent) : {stats['protected_recent']}")
    o.append("")
    o.append("  CONTEXT SURFACE (message.content, non-sidechain)")
    o.append(f"    before : {human(before):>8}  ≈ {est_tokens(before):>9,} tok")
    o.append(f"    after  : {human(after):>8}  ≈ {est_tokens(after):>9,} tok")
    o.append(f"    saved  : {human(saved):>8}  ≈ {est_tokens(saved):>9,} tok   ({pct:.1f}%)")
    o.append("")
    o.append("  SAVINGS BY TOOL")
    o.append(f"    {'Stubbed':>7}  {'Bytes':>8}  Tool")
    ranked = sorted(stats["by_tool"].items(), key=lambda x: -x[1][1])
    for name, (cnt, byts) in ranked:
        short = name.replace("mcp__claude_ai_kmcp-personal__", "kmcp::").replace("mcp__", "mcp::")
        o.append(f"    {cnt:>7}  {human(byts):>8}  {short}")
    o.append("")
    return "\n".join(o)


# ─── CLI ──────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session", help="Path to a session .jsonl (or a session-id under the cwd project dir)")
    ap.add_argument("--threshold", "-T", type=int, default=2000,
                    help="Stub tool_result content larger than N chars (default: 2000)")
    ap.add_argument("--keep-recent", "-k", type=int, default=10,
                    help="Protect the last N tool_results from the size rule — they are "
                         "cache-warm and most relevant on resume (default: 10)")
    ap.add_argument("--write", "-w", metavar="OUT",
                    help="Write the redacted COPY to OUT (default: dry-run, no file written)")
    ap.add_argument("--output", "-o", choices=["text", "json"], default="text")
    args = ap.parse_args()

    # Resolve session path: explicit file, or <id> under the cwd project dir.
    p = Path(args.session).expanduser()
    if not p.exists():
        slug = str(Path.cwd().resolve()).replace("/", "-")
        cand = Path.home() / ".claude" / "projects" / slug / f"{args.session}.jsonl"
        if cand.exists():
            p = cand
    if not p.exists():
        print(f"Error: session not found: {args.session}", file=sys.stderr)
        sys.exit(1)

    records = load(p)
    before = context_surface(records)
    stats = recompact(records, args.threshold, args.keep_recent)
    after = context_surface(records)

    if args.write:
        out = Path(args.write).expanduser()
        if out.resolve() == p.resolve():
            print("Error: refusing to overwrite the source session.", file=sys.stderr)
            sys.exit(2)
        dump(records, out)
        print(f"Wrote redacted copy: {out}", file=sys.stderr)

    if args.output == "json":
        print(json.dumps({
            "session": str(p),
            "before_chars": before, "after_chars": after,
            "before_tokens": est_tokens(before), "after_tokens": est_tokens(after),
            "saved_tokens": est_tokens(before - after),
            "saved_pct": round((before - after) / before * 100, 2) if before else 0,
            **stats,
            "by_tool": {k: {"stubbed": v[0], "bytes": v[1]} for k, v in stats["by_tool"].items()},
        }, indent=2))
    else:
        print(render_text(p, before, after, stats))


if __name__ == "__main__":
    main()
