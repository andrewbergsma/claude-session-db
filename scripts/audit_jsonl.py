#!/usr/bin/env python3
"""Phase 0 re-audit: field-frequency audit over current live JSONL.

Streams every *.jsonl under ~/.claude/projects (main sessions + subagents) and
produces a per-record-type field-presence report, so the parser/schema work in
later phases targets the *current* Claude Code data shape rather than the drifted
Feb 2026 DATA_MODEL.md.

Usage:
    python scripts/audit_jsonl.py [--limit N] [--json OUT.json]

Tracks, per record `type`:
  - top-level field presence counts
  - selected nested-field presence (message.*, message.usage.*, data.*, snapshot.*)
  - progress `data.type` distribution + per-subtype fields
  - system `subtype` distribution
  - tool_use names, toolUseResult shapes, CC version distribution
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def iter_jsonl_files(limit: int | None = None):
    root = projects_dir()
    files = sorted(root.glob("*/*.jsonl"))  # main + subagents are both under project dirs
    # subagents live in <session>/subagents/agent-*.jsonl -> need recursive
    files = sorted(root.rglob("*.jsonl"))
    if limit:
        files = files[:limit]
    return files


class Audit:
    def __init__(self) -> None:
        self.record_types: Counter = Counter()
        self.type_total: Counter = Counter()  # records seen per type (denominator)
        # type -> field -> count
        self.top_fields: dict[str, Counter] = defaultdict(Counter)
        # type -> nested path -> count
        self.nested_fields: dict[str, Counter] = defaultdict(Counter)
        self.progress_data_types: Counter = Counter()
        self.progress_fields: dict[str, Counter] = defaultdict(Counter)  # data.type -> field
        self.system_subtypes: Counter = Counter()
        self.system_fields: dict[str, Counter] = defaultdict(Counter)  # subtype -> field
        self.versions: Counter = Counter()
        self.models: Counter = Counter()
        self.tool_names: Counter = Counter()
        self.tool_result_shapes: Counter = Counter()
        self.usage_fields: Counter = Counter()
        self.files = 0
        self.lines = 0
        self.errors = 0

    def record(self, d: dict) -> None:
        rtype = d.get("type", "<no-type>")
        self.record_types[rtype] += 1
        self.type_total[rtype] += 1
        for k in d.keys():
            self.top_fields[rtype][k] += 1

        ver = d.get("version")
        if ver:
            self.versions[ver] += 1

        msg = d.get("message")
        if isinstance(msg, dict):
            for k in msg.keys():
                self.nested_fields[rtype][f"message.{k}"] += 1
            model = msg.get("model")
            if model:
                self.models[model] += 1
            usage = msg.get("usage")
            if isinstance(usage, dict):
                for k in usage.keys():
                    self.usage_fields[k] += 1
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    bt = block.get("type")
                    if bt == "tool_use":
                        self.tool_names[block.get("name", "<unknown>")] += 1
                    elif bt == "tool_result":
                        c = block.get("content")
                        if isinstance(c, str):
                            self.tool_result_shapes["string"] += 1
                        elif isinstance(c, list):
                            self.tool_result_shapes["array"] += 1
                        else:
                            self.tool_result_shapes[type(c).__name__] += 1

        # toolUseResult shape (client-side enrichment on user records)
        tur = d.get("toolUseResult")
        if tur is not None:
            self.nested_fields[rtype][f"toolUseResult:{type(tur).__name__}"] += 1

        if rtype == "progress":
            data = d.get("data", {})
            if isinstance(data, dict):
                dt = data.get("type", "<no-data-type>")
                self.progress_data_types[dt] += 1
                for k in data.keys():
                    self.progress_fields[dt][k] += 1

        if rtype == "system":
            st = d.get("subtype", "<no-subtype>")
            self.system_subtypes[st] += 1
            for k in d.keys():
                self.system_fields[st][k] += 1

        if rtype == "file-history-snapshot":
            snap = d.get("snapshot", {})
            if isinstance(snap, dict):
                for k in snap.keys():
                    self.nested_fields[rtype][f"snapshot.{k}"] += 1

    def run(self, files) -> None:
        for fp in files:
            self.files += 1
            try:
                with open(fp, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        self.lines += 1
                        try:
                            self.record(json.loads(line))
                        except json.JSONDecodeError:
                            self.errors += 1
            except OSError:
                self.errors += 1

    def report(self) -> str:
        out: list[str] = []
        w = out.append
        w("# JSONL Re-Audit (Phase 0)")
        w("")
        w(f"Files: {self.files:,}  |  Lines: {self.lines:,}  |  Parse errors: {self.errors:,}")
        w("")
        w("## Record Type Distribution")
        w("")
        w("| Type | Count | % |")
        w("|---|---|---|")
        for t, c in self.record_types.most_common():
            w(f"| `{t}` | {c:,} | {100*c/self.lines:.1f}% |")
        w("")

        w("## CC Versions")
        w("")
        for v, c in self.versions.most_common(20):
            w(f"- `{v}`: {c:,}")
        w("")

        w("## Models")
        w("")
        for m, c in self.models.most_common(20):
            w(f"- `{m}`: {c:,}")
        w("")

        w("## message.usage fields")
        w("")
        for k, c in self.usage_fields.most_common():
            w(f"- `{k}`: {c:,}")
        w("")

        w("## tool_result content shapes")
        w("")
        for s, c in self.tool_result_shapes.most_common():
            w(f"- {s}: {c:,}")
        w("")

        for rtype in ["user", "assistant", "progress", "system",
                      "file-history-snapshot", "summary", "queue-operation",
                      "custom-title"]:
            tot = self.type_total.get(rtype, 0)
            if not tot:
                continue
            w(f"## Top-level fields: `{rtype}` (n={tot:,})")
            w("")
            w("| field | freq | % |")
            w("|---|---|---|")
            for k, c in self.top_fields[rtype].most_common():
                w(f"| `{k}` | {c:,} | {100*c/tot:.1f}% |")
            if self.nested_fields[rtype]:
                w("")
                w("Nested:")
                for k, c in self.nested_fields[rtype].most_common():
                    w(f"- `{k}`: {c:,} ({100*c/tot:.1f}%)")
            w("")

        w("## progress data.type distribution")
        w("")
        ptot = sum(self.progress_data_types.values())
        for dt, c in self.progress_data_types.most_common():
            w(f"### `{dt}` — {c:,} ({100*c/ptot:.1f}%)")
            for k, kc in self.progress_fields[dt].most_common():
                w(f"  - `{k}`: {kc:,} ({100*kc/c:.1f}%)")
            w("")

        w("## system subtype distribution")
        w("")
        stot = sum(self.system_subtypes.values())
        for st, c in self.system_subtypes.most_common():
            w(f"### `{st}` — {c:,} ({100*c/stot:.1f}%)")
            for k, kc in self.system_fields[st].most_common():
                w(f"  - `{k}`: {kc:,} ({100*kc/c:.1f}%)")
            w("")

        w("## Tool names (top 60)")
        w("")
        for n, c in self.tool_names.most_common(60):
            w(f"- `{n}`: {c:,}")
        w("")
        return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="limit number of files")
    ap.add_argument("--json", type=str, default=None, help="also dump raw counters to JSON")
    ap.add_argument("--out", type=str, default=None, help="write markdown report to file")
    args = ap.parse_args()

    files = iter_jsonl_files(args.limit)
    print(f"Auditing {len(files):,} files...", file=sys.stderr)
    audit = Audit()
    audit.run(files)
    report = audit.report()

    if args.out:
        Path(args.out).write_text(report)
        print(f"Wrote report to {args.out}", file=sys.stderr)
    else:
        print(report)

    if args.json:
        raw = {
            "record_types": dict(audit.record_types),
            "versions": dict(audit.versions),
            "models": dict(audit.models),
            "usage_fields": dict(audit.usage_fields),
            "tool_result_shapes": dict(audit.tool_result_shapes),
            "progress_data_types": dict(audit.progress_data_types),
            "system_subtypes": dict(audit.system_subtypes),
            "tool_names": dict(audit.tool_names),
            "top_fields": {k: dict(v) for k, v in audit.top_fields.items()},
            "nested_fields": {k: dict(v) for k, v in audit.nested_fields.items()},
            "progress_fields": {k: dict(v) for k, v in audit.progress_fields.items()},
            "system_fields": {k: dict(v) for k, v in audit.system_fields.items()},
            "files": audit.files,
            "lines": audit.lines,
        }
        Path(args.json).write_text(json.dumps(raw, indent=2))
        print(f"Wrote raw counters to {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
