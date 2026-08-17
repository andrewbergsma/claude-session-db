#!/usr/bin/env python3
"""cr — CR (Context Reduction): a fork with curation.

The operator selects which context survives (messages, tool calls, kmcp reads),
optionally compiles a cart of kmcp refs, and CR writes a NEW reduced session
file — a REDACTION FORK. Every record is kept structurally; for each block the
operator did not keep, the content string is swapped for a short breadcrumb
stub (`[CR: Bash pytest — 9.8K elided]`) in BOTH copies where applicable (the
message.content block AND the top-level toolUseResult mirror). This is the
empirically validated edit class (resume loads clean — recompact.py proved it).

NEVER touched: uuid, parentUuid, tool_use_id, thinking blocks (their
signatures), block counts, record order. Sidechain records are dropped from
the copy outright (they never enter resumed context). The original session is
never mutated — the fork is a new file under a console-minted uuid4.

Engine ancestor: recompact.py — load/dump, est_tokens (chars/4), sha1 dedup,
both-copies stubbing are lifted from it; its policy layer (dedup wins
regardless of recency, recency protects the tail) became CR's default
recommendations. Everything here is deterministic code — no LLM.

Manifest row ids (stable, client-addressable):
    u:<uuid>    user prompt                 (record-level, text)
    a:<uuid>    assistant narration         (record-level, text blocks)
    s:<uuid>    skill/meta injection        (record-level, user-role text)
    t:<tool_id> tool_result                 (block-level, both copies)
    th:<uuid>   thinking                    (LOCKED — never stubbable)
"""
import hashlib
import json
import re
import uuid as uuidlib
from datetime import datetime, timezone
from pathlib import Path

from .recompact import content_chars, dump, est_tokens, human, input_hint, load

# ─── Version guard ────────────────────────────────────────────────────────────
# The JSONL schema is internal/unversioned upstream; records self-report the
# Claude Code version that wrote them (e.g. "2.1.233"). The redaction edit
# class was validated against 1.x/2.x records — a major we have not seen is
# treated as unsupported and CR refuses politely rather than forging a fork
# whose acceptance is unknown.
SUPPORTED_VERSION_RE = re.compile(r"^[12]\.")


def unsupported_versions(records) -> list:
    """Distinct record `version` values the redactor has not been validated
    against (None/absent is fine — many record types carry no version)."""
    bad = set()
    for r in records:
        v = r.get("version")
        if v is not None and not SUPPORTED_VERSION_RE.match(str(v)):
            bad.add(str(v))
    return sorted(bad)


# ─── Classification ───────────────────────────────────────────────────────────
KMCP_RE = re.compile(r"^mcp__.+__(?P<base>[a-z_]+)$")
KMCP_READ_TOOLS = {"get_entry", "get_section", "get_entries"}

SKIP_USER_PREFIXES = ("<bash-", "<task-notification>", "<command-",
                      "<local-command")

RECENT_TURNS = 6          # the last N user-prompt turns are pre-kept
DUP_MIN_CHARS = 1         # zero-char content never counts as a duplicate

# Non-redactable scaffolding floor (system prompt, tool schemas, memory files,
# harness overhead) — shown separately so the AFTER number is honest. A band,
# not a point: it varies with the project's CLAUDE.md/skill surface.
FLOOR_TOKENS = {"low": 70_000, "high": 100_000, "est": 85_000}


def _is_real_prompt(r, text) -> bool:
    """Same semantics as the console's _is_real_user_turn (kept local to avoid
    a cr -> console import cycle; the console imports cr)."""
    if r.get("isMeta"):
        return False
    if not text:
        return False
    t = text.lstrip()
    if t.startswith(SKIP_USER_PREFIXES):
        return False
    if t.startswith("<") and "system-reminder" in t[:80]:
        return False
    return True


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if isinstance(b, dict) and isinstance(b.get("text"), str):
            parts.append(b["text"])
        elif isinstance(b, str):
            parts.append(b)
    return "".join(parts)


def _digest(content) -> str:
    return hashlib.sha1(
        json.dumps(content, ensure_ascii=False).encode()).hexdigest()


def _kmcp_refs(base: str, inp: dict) -> list:
    """['app:path', ...] a kmcp read tool_use targets (for the → ref verb)."""
    if base == "get_entries":
        return [f"{it.get('application', '?')}:{it.get('path', '?')}"
                for it in (inp.get("entries") or inp.get("paths") or [])
                if isinstance(it, dict)]
    app, path = inp.get("application"), inp.get("path")
    if app or path:
        return [f"{app or '?'}:{path or '?'}"]
    return []


def build_manifest(records, bash_kmcp=None) -> dict:
    """One row per context block, with deterministic defaults. No LLM.

    `bash_kmcp` (optional callable, e.g. the console's _bash_kmcp) maps a Bash
    tool input to a (base_tool, input) pair when the record is a knowledge-cli
    shim read, so those classify as kmcp rows too.
    """
    bad = unsupported_versions(records)

    # Pass 1: tool_use_id -> {name, input, kmcp_base} from assistant records.
    tool_uses = {}
    for r in records:
        if r.get("type") != "assistant" or r.get("isSidechain"):
            continue
        for b in (r.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                name, inp = b.get("name", "?"), b.get("input") or {}
                m = KMCP_RE.match(name)
                base = m.group("base") if m else None
                if base is None and name == "Bash" and bash_kmcp:
                    shim = bash_kmcp(inp)
                    if shim:
                        base, inp = shim[0], shim[1]
                tool_uses[b.get("id")] = {
                    "name": name, "input": inp,
                    "kmcp": base if base in KMCP_READ_TOOLS else None,
                    "base": base,
                }

    # Pass 2: turn index per record (turn = # of real user prompts seen).
    n_turns = 0
    turn_of = []
    for r in records:
        if (r.get("type") == "user" and not r.get("isSidechain")
                and _is_real_prompt(r, _text_of((r.get("message") or {})
                                                .get("content")))):
            n_turns += 1
        turn_of.append(n_turns)
    recent_from = max(0, n_turns - RECENT_TURNS)   # turn > recent_from ⇒ recent

    # Pass 3: rows.
    rows, seen = [], {}
    fixed_chars = 0            # non-row transcript overhead (tool_use inputs)
    for ri, r in enumerate(records):
        t = r.get("type")
        if t not in ("user", "assistant") or r.get("isSidechain"):
            continue
        uid = r.get("uuid")
        msg = r.get("message") or {}
        content = msg.get("content")
        recent = turn_of[ri] > recent_from
        turn = turn_of[ri]

        def row(rid, kind, chars, *, name=None, hint=None, refs=None,
                dg=None, locked=False, tid=None):
            dup = False
            if dg is not None and chars >= DUP_MIN_CHARS:
                if dg in seen:
                    dup = True
                else:
                    seen[dg] = rid
            if locked:
                default = "keep"
            elif dup:
                default = "stub"               # dedup wins regardless of recency
            elif recent:
                default = "keep"
            elif kind == "kmcp":
                default = "ref"
            elif kind in ("result", "injection"):
                default = "stub"
            else:                              # prompt / narration
                default = "keep"
            rows.append({
                "id": rid, "kind": kind, "uuid": uid, "tid": tid,
                "name": name, "hint": hint, "refs": refs,
                "chars": chars, "est_tokens": est_tokens(chars),
                "dup": dup, "dup_of": seen.get(dg) if dup else None,
                "turn": turn, "recent": recent,
                "locked": locked, "default": default, "ts": r.get("timestamp"),
            })

        if t == "assistant":
            narr = 0
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    narr += len(b.get("text") or "")
                elif bt == "thinking":
                    row(f"th:{uid}", "thinking",
                        len(b.get("thinking") or ""), locked=True)
                elif bt == "tool_use":
                    fixed_chars += content_chars(b.get("input") or {})
            if isinstance(content, str):
                narr = len(content)
            if narr:
                row(f"a:{uid}", "narration", narr)
        else:   # user
            has_result = isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content)
            if has_result:
                for b in content:
                    if not (isinstance(b, dict)
                            and b.get("type") == "tool_result"):
                        continue
                    tid = b.get("tool_use_id")
                    meta = tool_uses.get(tid) or {}
                    chars = content_chars(b.get("content", ""))
                    if meta.get("kmcp"):
                        row(f"t:{tid}", "kmcp", chars, tid=tid,
                            name=meta.get("kmcp"),
                            refs=_kmcp_refs(meta["kmcp"], meta.get("input", {})),
                            dg=_digest(b.get("content", "")))
                    else:
                        row(f"t:{tid}", "result", chars, tid=tid,
                            name=meta.get("name", "?"),
                            hint=input_hint(meta.get("name", "?"),
                                            meta.get("input", {})),
                            dg=_digest(b.get("content", "")))
            else:
                txt = _text_of(content)
                if not txt:
                    continue
                if _is_real_prompt(r, txt):
                    row(f"u:{uid}", "prompt", len(txt))
                else:
                    row(f"s:{uid}", "injection", len(txt),
                        hint=txt.lstrip()[:60], dg=_digest(txt))

    groups: dict = {}
    for rw in rows:
        g = groups.setdefault(rw["kind"], {"count": 0, "chars": 0,
                                           "est_tokens": 0})
        g["count"] += 1
        g["chars"] += rw["chars"]
        g["est_tokens"] += est_tokens(rw["chars"])
    total = sum(rw["chars"] for rw in rows) + fixed_chars

    return {
        "version_ok": not bad,
        "unsupported_versions": bad,
        "rows": rows,
        "groups": groups,
        "turns": n_turns,
        "recent_turns": RECENT_TURNS,
        "fixed_chars": fixed_chars,
        "fixed_tokens": est_tokens(fixed_chars),
        "totals": {"chars": total, "est_tokens": est_tokens(total)},
        "floor": dict(FLOOR_TOKENS),
    }


# ─── Redaction (the validated edit class, both copies) ────────────────────────
def _breadcrumb(row) -> str:
    if row["kind"] == "kmcp":
        refs = row.get("refs") or []
        label = f"kmcp {refs[0]}" + (f" (+{len(refs)-1})" if len(refs) > 1 else "") \
            if refs else f"kmcp {row.get('name') or 'read'}"
    elif row["kind"] == "result":
        hint = row.get("hint") or ""
        label = f"{row.get('name') or 'tool'}{(' ' + hint) if hint else ''}"
    elif row["kind"] == "injection":
        label = "injected context"
    elif row["kind"] == "narration":
        label = "assistant narration"
    else:
        label = row["kind"]
    return f"[CR: {label} — {human(row['chars'])} elided]"


def apply_stubs(records, manifest, stub_ids) -> dict:
    """Stub the named manifest rows in place. Locked/unknown ids are reported,
    never raised. Returns {stubbed, ignored, saved_chars}."""
    by_id = {rw["id"]: rw for rw in manifest["rows"]}
    by_uuid = {}
    for r in records:
        u = r.get("uuid")
        if u:
            by_uuid.setdefault(u, r)

    stubbed, ignored, saved = [], [], 0
    for rid in stub_ids:
        rw = by_id.get(rid)
        if rw is None or rw.get("locked"):
            ignored.append(rid)
            continue
        rec = by_uuid.get(rw["uuid"])
        if rec is None:
            ignored.append(rid)
            continue
        crumb = _breadcrumb(rw)
        content = (rec.get("message") or {}).get("content")
        did = False
        if rw["kind"] in ("result", "kmcp"):
            for b in content if isinstance(content, list) else []:
                if (isinstance(b, dict) and b.get("type") == "tool_result"
                        and b.get("tool_use_id") == rw["tid"]):
                    b["content"] = crumb
                    did = True
            # copy 2: the top-level toolUseResult mirror (validated class)
            if did and isinstance(rec.get("toolUseResult"), (dict, list, str)):
                rec["toolUseResult"] = crumb
        elif rw["kind"] == "narration":
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "text":
                    b["text"] = crumb
                    did = True
            if isinstance(content, str):
                rec["message"]["content"] = crumb
                did = True
        else:   # prompt / injection — user-role text
            if isinstance(content, str):
                rec["message"]["content"] = crumb
                did = True
            else:
                for b in content if isinstance(content, list) else []:
                    if isinstance(b, dict) and isinstance(b.get("text"), str):
                        b["text"] = crumb
                        did = True
        if did:
            stubbed.append(rid)
            saved += max(0, rw["chars"] - len(crumb))
        else:
            ignored.append(rid)
    return {"stubbed": stubbed, "ignored": ignored, "saved_chars": saved}


def context_surface(records) -> int:
    """Char-count of the redaction-relevant context surface (recompact's
    measure, minus sidechain records which CR drops from the copy)."""
    total = 0
    for r in records:
        if r.get("type") not in ("user", "assistant") or r.get("isSidechain"):
            continue
        c = (r.get("message") or {}).get("content")
        if c is not None:
            total += len(json.dumps(c, ensure_ascii=False))
    return total


# ─── kmcp cart → compiled preamble ───────────────────────────────────────────
PREAMBLE_HEADER = (
    "[CR context preamble — curated by the operator at fork time]\n"
    "This session is a CONTEXT-REDUCTION fork: bulky tool results and injected\n"
    "context were elided in place (each elision left a `[CR: …]` breadcrumb;\n"
    "turns were gutted, not removed). The knowledge refs below were selected\n"
    "to travel with the fork — re-load any of them with get_entry/get_entries."
)
BODY_INLINE_MAX = 2500      # entry bodies bigger than this ride as summary only


def parse_ref(ref: str):
    app, _, path = (ref or "").partition(":")
    return (app.strip(), path.strip()) if app and path else (None, None)


def render_preamble(refs, entries=None, error=None) -> str:
    """The compiled preamble document. `entries` is the (input-ordered) result
    of ONE get_entries batch, or None when kmcp was unreachable — refs then
    degrade to plain text pointers, never block the fork."""
    if not refs:
        return PREAMBLE_HEADER
    out = [PREAMBLE_HEADER, ""]
    if error:
        out.append(f"(refs not hydrated — {error}; plain pointers below)")
        out.append("")
    ent_by_i = list(entries) if entries else []
    for i, ref in enumerate(refs):
        e = ent_by_i[i] if i < len(ent_by_i) and isinstance(ent_by_i[i], dict) \
            else None
        if not e or e.get("error"):
            why = (e or {}).get("error")
            out.append(f"- {ref}" + (f"  (unresolved: {why})" if why else ""))
            continue
        title = e.get("title") or ref
        desc = e.get("description") or ""
        body = e.get("content")
        if isinstance(body, dict):
            body = body.get("summary") or json.dumps(body, ensure_ascii=False)
        body = body if isinstance(body, str) else ""
        if len(body) > BODY_INLINE_MAX:
            body = (e.get("summary") if isinstance(e.get("summary"), str)
                    else "") or (body[:BODY_INLINE_MAX] + " …[truncated — "
                                 f"re-load {ref} for the rest]")
        out.append(f"## {ref}")
        out.append(f"{title}" + (f" — {desc}" if desc else ""))
        if body:
            out.append(body.strip())
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# ─── Fork forge ───────────────────────────────────────────────────────────────
class CRUnsupported(RuntimeError):
    """Records carry a version the redactor has not been validated against."""


def forge_fork(src_path, stub_ids, preamble_text=None, new_id=None,
               bash_kmcp=None) -> dict:
    """Write the redacted COPY as a new session file beside the source.

    House doctrine: the console always mints the fork's session id itself —
    `new_id` defaults to a fresh uuid4 chosen HERE, never inferred later.
    The source file is read, never touched.
    """
    src = Path(src_path)
    records = load(src)
    bad = unsupported_versions(records)
    if bad:
        raise CRUnsupported(
            "transcript carries record version(s) this redactor has not been "
            f"validated against: {', '.join(bad)} — refusing to forge")

    # Sidechain records never enter resumed context — drop them outright.
    records = [r for r in records if not r.get("isSidechain")]

    before = context_surface(records)
    manifest = build_manifest(records, bash_kmcp=bash_kmcp)
    stats = apply_stubs(records, manifest, stub_ids)

    new_id = new_id or str(uuidlib.uuid4())
    last_main = None
    template = {}
    for r in records:
        if r.get("type") in ("user", "assistant") and r.get("uuid"):
            last_main = r.get("uuid")
            template = r
    for r in records:
        if r.get("sessionId"):
            r["sessionId"] = new_id

    if preamble_text:
        # ONE synthetic user record at the fork tip (parentUuid = last kept
        # main-chain uuid) — the same append class as the custom-title record
        # and /compact's isCompactSummary message.
        records.append({
            "type": "user",
            "uuid": str(uuidlib.uuid4()),
            "parentUuid": last_main,
            "sessionId": new_id,
            "timestamp": datetime.now(timezone.utc)
                                 .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "isSidechain": False,
            "userType": "external",
            "cwd": template.get("cwd"),
            "gitBranch": template.get("gitBranch"),
            "version": template.get("version"),
            "message": {"role": "user",
                        "content": [{"type": "text", "text": preamble_text}]},
        })
    src_sid = src.stem
    records.append({"type": "custom-title", "sessionId": new_id,
                    "customTitle": f"CR fork of {src_sid[:8]}"})

    dst = src.parent / f"{new_id}.jsonl"
    if dst.resolve() == src.resolve():
        raise ValueError("refusing to overwrite the source session")
    dump(records, dst)
    after = context_surface(records)
    return {
        "new_session": new_id,
        "path": str(dst),
        "before_chars": before, "after_chars": after,
        "before_tokens": est_tokens(before), "after_tokens": est_tokens(after),
        "saved_pct": round((before - after) / before * 100, 1) if before else 0,
        **stats,
    }
