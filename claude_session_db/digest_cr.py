"""digest_cr — the `csd digest --cr` cut: a summary-shaped session rendering
built on the console's CR manifest.

`/session-summary` always hands a FRESH subagent the session to summarize. That
agent should read what the session was made of — prompts, narration, what was
written to the base, what went wrong, what subagents reported — without paying
for every tool result body. This renders exactly that, and it is built on
`cr.build_manifest` (the console's Context-Reduction panel) so the console and
the summarizer agree on the row taxonomy, the row ids and the token model.

Every manifest row in the window produces output — kept rows verbatim (or
head-capped), every other row as a ONE-LINE breadcrumb carrying its row id.
Nothing vanishes: an elided row is a stub, never an absence (the bug in the
curated-digest prior art, which iterated kept rows only). Any stub is
dereferenced with `csd digest <uuid> --row <row-id>` (`row_text` below).

Row ids are CR's own: `u:` prompt, `a:` narration, `s:` injection, `t:<id>`
tool result, `x:<id>` tool_use input, `i:` image, `th:` thinking, `o:` other.
A tool_use and its successful result collapse to ONE line; `t:` and `x:` with
the same tool id are the two halves.

The keep/stub exceptions are table-driven (the constants right below) so a
future `--keep` / profile flag is a matter of swapping tables, not code.
Deterministic code only — no LLM, no DB.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from . import cr
from . import tool_labels
from .recompact import load
from .session_digest import (WATERMARK_SLACK_S, _parse_iso, compaction_marker,
                             is_compaction)

# ─── The rules (one table per rule — a future --keep flag swaps these) ────────
# Row kinds rendered VERBATIM.
KEEP_VERBATIM_KINDS = {"prompt", "narration"}
# Thinking is kept only when the transcript stored its text (usually it does
# not: the file keeps the signature, not the reasoning).
KEEP_THINKING_WITH_TEXT = True

# Subagent dispatch. The input becomes a one-line stub naming the agent; the
# RESULT is kept, head-capped — a subagent's final report is narration by proxy,
# and in a delegation-heavy session it carries most of the work.
AGENT_TOOLS = {"Agent", "Task"}
AGENT_RESULT_CAP = 2000

# A failed tool result is kept (head only): failed-then-retried is the stumble
# signal a summary turns into a lesson, and a stub would make a failure look
# identical to a success.
ERROR_RESULT_CAP = 400
KMCP_WRITE_ERROR_CAP = 200

# File-mutating tools: stub, but the hint is the FULL path (CR's input_hint
# truncates to 60 chars; a summary's scope wants the whole path).
EDIT_TOOLS = {
    "Edit": "Edit",
    "Write": "Write (create/overwrite)",
    "MultiEdit": "MultiEdit",
    "NotebookEdit": "NotebookEdit",
}
PATH_KEYS = ("file_path", "notebook_path", "path")

# kmcp WRITES — the most important non-prose rows (they feed a summary's event
# `scope`). CR classifies reads only (cr.KMCP_READ_TOOLS); writes are detected
# here, via the MCP name `mcp__*__<base>` or the knowledge-cli Bash shim.
KMCP_WRITE_TOOLS = {
    "create_entry", "update_entry", "patch_content", "import_entries",
    "create_relationship", "delete_relationship", "delete_entry",
    "move_entry", "rename_entry", "add_entry_tag", "upload_file",
    "create_application", "update_application",
    "import_lessons",          # angles._WRITE_TOOLS counts it; it writes too
}

# Injections that carry work rather than scaffolding, keyed by CR's
# injection_label name -> cap on the kept text (None = verbatim).
#   task-notification: a BACKGROUND agent's final report arrives here, not in
#     the Agent tool_result (which only says "Async agent launched") — the
#     same narration-by-proxy the AGENT_TOOLS rule keeps.
#   command: a slash command's ARGS are the user's own words (`/delegate …`);
#     CR's label keeps 50 chars of them.
INJECTION_KEEP = {"task-notification": AGENT_RESULT_CAP, "command": None}

HINT_CAP = 100            # free-text hints (commands, queries); paths are whole

# ─── small helpers ────────────────────────────────────────────────────────────
_WS = re.compile(r"\s+")


def _one_line(s, cap: Optional[int] = HINT_CAP) -> str:
    t = _WS.sub(" ", str(s or "")).strip()
    if cap is not None and len(t) > cap:
        t = t[: cap - 1] + "…"
    return t


def _sz(chars: int) -> str:
    """Characters as `1.2K` (the breadcrumb unit)."""
    return f"{(chars or 0) / 1000:.1f}K"


def _ktok(n: int) -> str:
    return f"~{(n or 0) / 1000:.1f}K"


def _cap(text: str, cap: int, rid: str) -> str:
    text = (text or "").rstrip()
    if len(text) <= cap:
        return text
    return (text[:cap].rstrip()
            + f"\n… [+{_sz(len(text) - cap)} elided, --row {rid}]")


def tool_hint(name: str, inp) -> str:
    """The salient input field of a tool call, for a stub line. Paths are
    never truncated; free text is capped at HINT_CAP."""
    inp = inp if isinstance(inp, dict) else {}
    for k in PATH_KEYS:
        if isinstance(inp.get(k), str) and inp[k].strip():
            return inp[k].strip()
    for k in ("command", "pattern", "query", "url", "entry_path", "skill",
              "subject", "description", "prompt"):
        if inp.get(k):
            return _one_line(inp[k])
    return _one_line(tool_labels.tool_target(name, inp))


def _console_hooks():
    """(bash_kmcp, import_docs, parse_write_result) from the console, where the
    knowledge-cli shim parser and the import-document scraper already live.
    Imported lazily (the console module imports cr); degrades to None."""
    try:
        from .console import server
        return server._bash_kmcp, server._import_docs, server._parse_write_result
    except Exception:  # noqa: BLE001 — the digest must never fail on this
        return None, None, None


def _ref(app, path) -> str:
    return f"{app or '?'}:{path or '?'}"


def kmcp_write_refs(base: str, inp, import_docs: Optional[Callable] = None) -> str:
    """The target(s) of one kmcp write, as one string."""
    inp = inp if isinstance(inp, dict) else {}
    app = inp.get("application")
    if base in ("import_entries", "import_lessons"):
        docs = import_docs(inp) if import_docs else []
        if not docs:
            for key in ("entries", "documents"):
                v = inp.get(key)
                if isinstance(v, list):
                    docs += [{"app": d.get("application"), "path": d.get("path")}
                             for d in v if isinstance(d, dict) and d.get("path")]
        if docs:
            return ", ".join(_ref(d.get("app") or app, d.get("path")) for d in docs)
        return f"file: {inp['file_path']}" if inp.get("file_path") else "(no refs readable)"
    if base in ("create_relationship", "delete_relationship"):
        src_app = (inp.get("source_application") or app
                   or inp.get("source_app") or inp.get("from_application"))
        tgt_app = (inp.get("target_application") or inp.get("target_app")
                   or inp.get("to_application"))
        tgt = inp.get("target_path") or "?"
        if tgt_app and tgt_app != src_app:
            tgt = f"{tgt_app}:{tgt}"
        rel = inp.get("relationship_type") or inp.get("type") or "related"
        return f"{_ref(src_app, inp.get('source_path'))} -> {tgt} ({rel})"
    if base in ("move_entry", "rename_entry"):
        new = inp.get("new_path") or "?"
        new_app = inp.get("new_application")
        if new_app and new_app != app:
            new = f"{new_app}:{new}"
        return f"{_ref(app, inp.get('old_path') or inp.get('path'))} -> {new}"
    if base in ("create_application", "update_application"):
        return str(inp.get("name") or app or "?")
    if base == "upload_file":
        return str(inp.get("filename") or inp.get("file_path") or "?")
    out = _ref(app, inp.get("path") or inp.get("entry_path"))
    if base == "add_entry_tag" and inp.get("tag"):
        out += f" +{inp['tag']}"
    return out


def _is_async_launch(text: str, tur) -> bool:
    if isinstance(tur, dict) and (tur.get("isAsync")
                                  or tur.get("status") == "async_launched"):
        return True
    return (text or "").lstrip().startswith("Async agent launched")


# ─── the manifest, shared by the digest and --row ─────────────────────────────
def _main_chain(records) -> list:
    """What CR sees, minus compaction carriers: sidechains never enter the
    parent's context, and a compaction summary RESTATES history (in a digest
    it reads as new work and gets summarized twice)."""
    return [r for r in records
            if not r.get("isSidechain") and not is_compaction(r)]


def manifest_for(records, bash_kmcp=None) -> dict:
    return cr.build_manifest(_main_chain(records), bash_kmcp=bash_kmcp)


def row_text(path, row_id: str) -> Optional[str]:
    """Full body of one manifest row (the stub dereference), or None when the
    id is not in the manifest."""
    records = load(Path(path))
    bash_kmcp, _, _ = _console_hooks()
    main = _main_chain(records)
    man = cr.build_manifest(main, bash_kmcp=bash_kmcp)
    row = next((w for w in man["rows"] if w["id"] == row_id), None)
    if row is None:
        return None
    rec = next((r for r in main if r.get("uuid") == row.get("uuid")), None)
    body = cr.row_body([rec] if rec else [], row)
    if body is None:
        return None
    if body.get("image"):
        img = body["image"]
        return (f"{body.get('text') or 'image'} ({img.get('media_type')}, "
                f"{len(img.get('data') or '')} base64 chars — not printed)\n")
    text = body.get("text") or ""
    return text if text.endswith("\n") else text + "\n"


# ─── the render ───────────────────────────────────────────────────────────────
def render_cr(session_path, session_id: Optional[str] = None,
              since: Optional[datetime] = None) -> tuple[str, dict]:
    """(digest text, stats) for one session JSONL.

    `since` (aware datetime) keeps only records strictly after it (+ the shared
    WATERMARK_SLACK_S), exactly as session_digest's delta window does. The
    manifest is built over the WHOLE main chain first, so row ids, turn numbers
    and tool names resolve even when a call sits before the window.
    """
    p = Path(session_path).expanduser()
    sid = session_id or p.stem
    records = load(p)
    total = len(records)
    bash_kmcp, import_docs, parse_write = _console_hooks()

    # Tool index over the full main chain: tid -> name/input/base, result
    # error flag, result text and the toolUseResult mirror.
    tools: dict = {}
    for r in records:
        if r.get("isSidechain"):
            continue
        content = (r.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if r.get("type") == "assistant" and b.get("type") == "tool_use":
                name, inp = b.get("name") or "?", b.get("input")
                inp = inp if isinstance(inp, dict) else {}
                m = cr.KMCP_RE.match(name)
                base, kin = (m.group("base") if m else None), inp
                if base is None and name == "Bash" and bash_kmcp:
                    shim = bash_kmcp(inp)
                    if shim:
                        base, kin = shim
                tools.setdefault(b.get("id"), {}).update(
                    name=name, input=inp, base=base, kinput=kin or {})
            elif r.get("type") == "user" and b.get("type") == "tool_result":
                t = tools.setdefault(b.get("tool_use_id"), {})
                t["is_error"] = bool(b.get("is_error"))
                t["result"] = cr._result_text(b.get("content", ""))
                t["tur"] = r.get("toolUseResult")

    main = _main_chain(records)
    man = cr.build_manifest(main, bash_kmcp=bash_kmcp)
    by_id = {w["id"]: w for w in man["rows"]}

    # The window, over ALL records (a compaction boundary in it still marks).
    first_ts = next((o.get("timestamp") for o in records if o.get("timestamp")), "?")
    last_ts = next((o.get("timestamp") for o in reversed(records)
                    if o.get("timestamp")), "?")
    window = records
    if since is not None:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        cutoff = since + timedelta(seconds=WATERMARK_SLACK_S)
        window = [o for o in records
                  if (ts := _parse_iso(o.get("timestamp"))) is not None
                  and ts > cutoff]
    win_uuids = {o.get("uuid") for o in window
                 if not o.get("isSidechain") and not is_compaction(o)}
    rows_in = [w for w in man["rows"] if w.get("uuid") in win_uuids]

    # Rows grouped by record, in block order — a record's text (prompt /
    # narration / injection) sits where its first text block sits.
    by_uuid_rec = {}
    for r in main:
        by_uuid_rec.setdefault(r.get("uuid"), r)

    def _text_bidx(rec):
        c = (rec.get("message") or {}).get("content")
        if isinstance(c, list):
            for i, b in enumerate(c):
                if isinstance(b, str) or (isinstance(b, dict)
                                          and isinstance(b.get("text"), str)):
                    return i
        return -1

    rows_by_uuid: dict = {}
    for w in rows_in:
        rows_by_uuid.setdefault(w["uuid"], []).append(w)
    for u, ws in rows_by_uuid.items():
        tb = _text_bidx(by_uuid_rec.get(u) or {})
        ws.sort(key=lambda w: (w["bidx"] if w.get("bidx") is not None else tb,
                               w.get("sub") if w.get("sub") is not None else -1))

    def body_of(w) -> str:
        rec = by_uuid_rec.get(w.get("uuid"))
        b = cr.row_body([rec] if rec else [], w)
        return ((b or {}).get("text") or "")

    out: list = []
    kept_ids: set = set()
    consumed: set = set()

    def tool_label(t) -> str:
        name = t.get("name") or "?"
        return EDIT_TOOLS.get(name, name)

    def err_mark(t) -> str:
        """` ✗ <first chars of the error>` for a failed call, else ''."""
        if "result" not in t:
            return ""
        msg = None
        if t.get("is_error"):
            msg = t.get("result") or "error"
        elif t.get("base") in KMCP_WRITE_TOOLS and parse_write:
            # A REFUSED write answers {error, message} with is_error unset.
            try:
                wres = parse_write(t.get("result"))
            except Exception:  # noqa: BLE001
                wres = None
            if wres and wres.get("errors"):
                msg = wres["errors"][0]
        return f" ✗ {_one_line(msg, KMCP_WRITE_ERROR_CAP)}" if msg else ""

    def emit_tool(tid: str, at_result: bool) -> None:
        """Render one tool pair at the first of its two rows in the window."""
        t = tools.get(tid) or {}
        xr, tr = by_id.get(f"x:{tid}"), by_id.get(f"t:{tid}")
        x_in = xr is not None and xr.get("uuid") in win_uuids
        t_in = tr is not None and tr.get("uuid") in win_uuids
        name, base = t.get("name") or "?", t.get("base")
        inp = t.get("input") or {}
        in_sz = f"in {_sz(xr['chars'])}" if xr else None
        out_sz = f"out {_sz(tr['chars'])}" if tr else None

        # kmcp READ — refs only, never bodies.
        if tr is not None and tr["kind"] == "kmcp" or (
                base in cr.KMCP_READ_TOOLS and tr is None):
            refs = (tr or {}).get("refs") or cr._kmcp_refs(base, t.get("kinput") or {})
            rid = f"t:{tid}" if tr else f"x:{tid}"
            out.append(f"  KMCP READ {rid}: "
                       f"{', '.join(refs) if refs else base}{err_mark(t)}")
            consumed.update({f"x:{tid}", f"t:{tid}"})
            return

        # kmcp WRITE — op + target(s), and whether it failed.
        if base in KMCP_WRITE_TOOLS:
            target = kmcp_write_refs(base, t.get("kinput") or {}, import_docs)
            dry = " (dry-run)" if (t.get("kinput") or {}).get("dry_run") else ""
            via = " (via knowledge-cli)" if name == "Bash" else ""
            out.append(f"  KMCP WRITE x:{tid}: {base} {target}{dry}{via}"
                       f"{err_mark(t)}")
            consumed.update({f"x:{tid}", f"t:{tid}"})
            return

        # Subagent dispatch — input stub here, the result kept where it lands.
        if name in AGENT_TOOLS:
            what = " — ".join(x for x in (inp.get("subagent_type") or "agent",
                                          _one_line(inp.get("description"))) if x)
            if x_in and f"x:{tid}" not in consumed:
                bg = ""
                if tr is not None and _is_async_launch(t.get("result"), t.get("tur")):
                    bg = " (background — its report arrives as a task-notification)"
                    consumed.add(f"t:{tid}")
                out.append(f"  AGENT x:{tid}: {what}{bg}")
                consumed.add(f"x:{tid}")
            if at_result and t_in and f"t:{tid}" not in consumed:
                label = "AGENT ERROR" if t.get("is_error") else "AGENT RESULT"
                out.append(f"\n{label} t:{tid} ({what}):")
                out.append(_cap(body_of(tr), AGENT_RESULT_CAP, f"t:{tid}"))
                kept_ids.add(f"t:{tid}")
                consumed.add(f"t:{tid}")
            return

        hint = tool_hint(name, inp)
        label = f"{tool_label(t)}{(' ' + hint) if hint else ''}"

        # Failed result — input stub at the call, error head where it lands.
        if tr is not None and t.get("is_error"):
            if x_in and f"x:{tid}" not in consumed:
                out.append(f"  [x:{tid} {label} — {in_sz} elided]")
                consumed.add(f"x:{tid}")
            if at_result and t_in and f"t:{tid}" not in consumed:
                out.append(f"  TOOL ERROR t:{tid} ({label}): "
                           + _one_line(body_of(tr), None)[:ERROR_RESULT_CAP]
                           + (f" … [--row t:{tid}]"
                              if tr["chars"] > ERROR_RESULT_CAP else ""))
                kept_ids.add(f"t:{tid}")
                consumed.add(f"t:{tid}")
            return

        # Everything else — one line for the pair.
        if x_in and t_in:
            out.append(f"  [t:{tid} {label} — {in_sz} / {out_sz} elided]")
        elif x_in:
            out.append(f"  [x:{tid} {label} — {in_sz} elided"
                       + (" · no result]" if tr is None else "]"))
        else:
            out.append(f"  [t:{tid} {label} — {out_sz} elided]")
        consumed.update({f"x:{tid}", f"t:{tid}"})

    def emit_injection(w) -> None:
        name, hint = w.get("name"), w.get("hint")
        what = " ".join(x for x in (name, hint) if x)
        label = (f"injected {what}" if name
                 else f"injected context · {hint}" if hint
                 else "injected context")
        if name in INJECTION_KEEP:
            text = body_of(w)
            cap = INJECTION_KEEP[name]
            if name == "task-notification":
                inner = cr._tag(text, "result")
                if inner:
                    tid = cr._tag(text, "tool-use-id")
                    status = cr._tag(text, "status") or "?"
                    desc = _one_line(((tools.get(tid) or {}).get("input") or {})
                                     .get("description"))
                    what = (f"x:{tid}" + (f" {desc}" if desc else "")) if tid \
                        else _one_line(cr._tag(text, "summary"))
                    out.append(f"\nAGENT REPORT {w['id']} ({status} · {what}):")
                    out.append(_cap(inner, cap, w["id"]))
                    kept_ids.add(w["id"])
                    return
            elif name == "command":
                cname = cr._tag(text, "command-name") or cr._tag(text, "command-message")
                if cname and not cname.startswith("/"):
                    cname = "/" + cname
                args = cr._tag(text, "command-args")
                if args:
                    out.append(f"\n[USER {cname or '/command'}] {args.strip()}")
                    kept_ids.add(w["id"])
                    return
        out.append(f"  [{w['id']} {label} — {_sz(w['chars'])} elided]")

    # ── header ──
    think_rows = [w for w in rows_in if w["kind"] == "thinking"]
    out.append(f"SESSION DIGEST  ·  {sid}   [cr]")
    out.append(f"span: {first_ts} -> {last_ts}   ({total} records)")
    if since is not None:
        wf = next((o.get("timestamp") for o in window if o.get("timestamp")), "?")
        wl = next((o.get("timestamp") for o in reversed(window)
                   if o.get("timestamp")), "?")
        out.append(f"delta span: records after {since.isoformat()}   "
                   f"({len(window)} of {total} records; {wf} -> {wl})")
    out.append("kept: [USER] prompts and slash-command args, [ASSISTANT] "
               "narration, stored thinking, subagent results/reports (first "
               f"{AGENT_RESULT_CAP:,} chars), tool errors (first "
               f"{ERROR_RESULT_CAP} chars)")
    out.append("stubbed (one line each, sizes in chars): every other tool "
               "call + result as `[t:<id> Tool hint — in / out]`, injected "
               "context, images; kmcp reads as `KMCP READ` refs, kmcp writes "
               "as `KMCP WRITE` op + target (✗ = failed)")
    out.append(f"thinking: {len(think_rows)} blocks present, "
               f"{sum(1 for w in think_rows if w.get('stored') == 'text')} "
               "with stored text (the transcript keeps the signature, not the "
               "reasoning)")
    out.append("")
    out.append("NOTE FOR THE SUMMARIZER: tool calls, tool results and injected "
               "context are stubs. Expand one with "
               f"`csd digest {sid} --row <row-id>` (`t:<id>` = a tool's "
               "result, `x:<id>` = its input) ONLY when a stub is "
               "load-bearing for the summary. Say what you could not see. "
               "The session_id for any entry you write is the UUID in the "
               "first line of this header.")
    out.append("=" * 72)
    if since is not None and not window:
        out.append("(no records after the watermark)")

    # ── body ──
    turn = None
    emitted = set()
    for rec in window:
        if rec.get("isSidechain"):
            continue
        if is_compaction(rec):
            if rec.get("subtype") == "compact_boundary":
                out.append(compaction_marker(rec))
            continue
        u = rec.get("uuid")
        if u in emitted or u not in rows_by_uuid:
            continue
        emitted.add(u)
        for w in rows_by_uuid[u]:
            if w.get("turn") != turn:
                turn = w.get("turn")
                out.append("")
                out.append(f"── turn {turn} " + "─" * 60)
            kind, rid = w["kind"], w["id"]
            if kind in KEEP_VERBATIM_KINDS:
                text = body_of(w).strip()
                if text:
                    tag = "USER" if kind == "prompt" else "ASSISTANT"
                    out.append(f"\n[{tag}] {text}")
                    kept_ids.add(rid)
            elif kind == "thinking":
                text = body_of(w).strip() if w.get("stored") == "text" else ""
                if text and KEEP_THINKING_WITH_TEXT:
                    out.append(f"\n[THINKING] {text}")
                    kept_ids.add(rid)
            elif kind in ("tool_use", "result", "kmcp"):
                if rid in consumed:
                    continue
                emit_tool(w.get("tid"), at_result=kind != "tool_use")
            elif kind == "injection":
                emit_injection(w)
            elif kind == "image":
                out.append(f"  {rid} {cr._image_crumb(w)}")
            else:
                out.append(f"  [{rid} {w.get('name') or kind} — "
                           f"{_sz(w['chars'])} elided]")

    text = "\n".join(out) + "\n"
    total_tok = sum(w["est_tokens"] for w in rows_in)
    out_tok = len(text) // 4
    stats = {
        "session_id": sid,
        "rows": len(rows_in),
        "kept": len(kept_ids),
        "est_tokens_out": out_tok,
        "est_tokens_rows": total_tok,
        "pct": round(100 * out_tok / total_tok) if total_tok else 0,
    }
    return text, stats


def accounting_line(stats: dict) -> str:
    return (f"kept {stats['kept']} of {stats['rows']} rows · "
            f"{_ktok(stats['est_tokens_out'])} of "
            f"{_ktok(stats['est_tokens_rows'])} est tokens ({stats['pct']}%)")
