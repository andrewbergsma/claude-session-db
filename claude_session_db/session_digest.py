#!/usr/bin/env python3
"""
session_digest — cheap, lossy transcript digest for off-session summarization.

Produces a compact text rendering of a Claude Code session that KEEPS the
high-signal/low-byte parts (user prompts, assistant narration, tool calls + their
inputs) and DROPS the low-signal/high-byte parts (full tool_result bodies, which
are ~80% of transcript bytes but near-useless for a summary). Tool results are
truncated to a short head so errors and key outputs survive.

The output is meant to be fed to a *minimal-context* subagent that writes the
session-summary kmcp entries — without the caller ever loading the full transcript.

Design: claudecode:knowledge:design/session-archive-and-recompact (minimal-context harness)

Usage:
    python3 scripts/session_digest.py <session.jsonl> [--result-head 200] > digest.txt
"""
import argparse, json, sys
from pathlib import Path


def load(p):
    out = []
    for line in open(p, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def text_of(content):
    """Join the text blocks of an assistant/user message; ignore tool blocks."""
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text", ""))
    return "\n".join(p for p in parts if p)


def input_hint(inp):
    if not isinstance(inp, dict):
        return ""
    for k in ("path", "file_path", "entry_path", "query", "pattern", "command", "url", "prompt"):
        if inp.get(k):
            return str(inp[k]).replace("\n", " ")[:120]
    return ""


def _parse_iso(s):
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


_ELIDED = object()  # sentinel injected between head and tail windows


def render(session_path, result_head: int = 200, full_inputs: bool = False,
           head=None, tail=None, since=None, note: str = "") -> str:
    """Digest one session JSONL into compact text (the CLI body, importable —
    the phase-4 summarizer calls this in-process instead of a subprocess).

    Windowing (all optional; full digest when omitted):
      since — aware datetime: keep only records with timestamp AFTER it (the
              "delta digest" — everything a prior summary has not seen).
      head/tail — record counts: keep the first `head` + last `tail` selected
              records, eliding the middle (full digests of 7MB+ transcripts
              are unreadable and unpayable in context).
    Tool-result labels are mapped over the WHOLE file first, so results in the
    kept window still resolve names from elided/filtered tool_use records.
    """
    p = Path(session_path).expanduser()
    recs = load(p)
    total = len(recs)

    # Map tool_use_id -> (name, hint) so we can label results.
    tu = {}
    for o in recs:
        if o.get("type") == "assistant":
            for b in o.get("message", {}).get("content", []) or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tu[b["id"]] = (b.get("name", "?"), input_hint(b.get("input", {})))

    first_ts = next((o.get("timestamp") for o in recs if o.get("timestamp")), "?")
    last_ts = next((o.get("timestamp") for o in reversed(recs) if o.get("timestamp")), "?")

    if since is not None:
        recs = [o for o in recs
                if (ts := _parse_iso(o.get("timestamp"))) is not None and ts > since]
    selected = len(recs)
    if (head is not None or tail is not None) and selected > (head or 0) + (tail or 0):
        elided = selected - (head or 0) - (tail or 0)
        recs = (recs[:head or 0] + [(_ELIDED, elided)]
                + (recs[-tail:] if tail else []))

    out = []
    out.append(f"SESSION DIGEST  ·  {p.name}")
    out.append(f"span: {first_ts} -> {last_ts}   ({total} records)")
    if note:
        out.append(note)
    if since is not None:
        out.append(f"window: {selected} of {total} records after "
                   f"{since.isoformat()}")
    out.append("=" * 72)
    if since is not None and selected == 0:
        out.append("(no records after the watermark)")

    for o in recs:
        if isinstance(o, tuple) and o[0] is _ELIDED:
            out.append(f"\n⋯ ⋯ ⋯  (+{o[1]} records elided — head/tail window)  ⋯ ⋯ ⋯")
            continue
        if o.get("isSidechain"):
            continue
        typ = o.get("type")
        msg = o.get("message", {})
        content = msg.get("content")

        if typ == "user":
            # A user record is either a real human prompt or a tool_result carrier.
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        name, hint = tu.get(b.get("tool_use_id"), ("?", ""))
                        body = b.get("content", "")
                        body = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
                        body = body.replace("\n", " ").strip()
                        head = body[:result_head]
                        more = f" …(+{len(body) - result_head}c)" if len(body) > result_head else ""
                        err = " [ERROR]" if b.get("is_error") else ""
                        out.append(f"    ⮑ result[{name}{(' ' + hint) if hint else ''}]{err}: {head}{more}")
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
                    inp = b.get("input", {})
                    shown = json.dumps(inp, ensure_ascii=False) if full_inputs else input_hint(inp)
                    out.append(f"  → {b.get('name')}({shown})")

    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("--result-head", type=int, default=200,
                    help="Chars of each tool_result to keep (default 200)")
    ap.add_argument("--full-inputs", action="store_true",
                    help="Keep tool_use inputs VERBATIM instead of a one-field hint. "
                         "Actions (create_entry/create_relationship/Edit args) live in tool inputs; "
                         "hinting them loses 'what was done' recall. Costs more tokens than the hint.")
    ap.add_argument("--head", type=int, default=None,
                    help="Keep only the first N selected records (with --tail, elides the middle).")
    ap.add_argument("--tail", type=int, default=None,
                    help="Keep only the last N selected records.")
    ap.add_argument("--since", default=None,
                    help="ISO timestamp: keep only records strictly after it (delta digest).")
    args = ap.parse_args()
    sys.stdout.write(render(args.session, args.result_head, args.full_inputs,
                            head=args.head, tail=args.tail,
                            since=_parse_iso(args.since)))


if __name__ == "__main__":
    main()
