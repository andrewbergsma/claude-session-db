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
    x:<tool_id> tool_use INPUT              (block-level, assistant side)
    i:<uuid>#<b>[.<s>]  image block         (block-level, user msg or result)
    th:<uuid>   thinking                    (LOCKED — never stubbable)
    o:<uuid>#<b>  unrecognised block        (LOCKED — counted, never hidden)

─── THE ACCOUNTING MODEL (what BEFORE actually measures) ─────────────────────
CR's BEFORE was once `len(json.dumps(content))` over every main-chain record —
the transcript's BYTES. That number is not what the API bills, and on a real
session it was wrong by ~50%: one pasted screenshot's base64 read as ≈119K
"tokens" (the API bills an image at ~1-2K), 88 thinking SIGNATURES read as
≈51K (they are opaque provenance strings, not tokens at all), and the JSON
quoting/keys themselves were counted. Meanwhile the ~36K real tokens of
tool_use INPUT (file bodies in a Write, kmcp documents in an import_entries)
were invisible, folded into an opaque `fixed_chars` bucket the UI never showed
— which is why the group sizes could never add up to BEFORE.

The model now, per block, is what the API would count:

  text / prompt / narration / injection   chars/4  (`est_tokens`, unchanged)
  thinking                                chars/4 of the THINKING TEXT ONLY
  tool_result content                     chars/4 (image sub-blocks excluded —
                                          they become their own image rows)
  tool_use input                          len(json.dumps(input))/4 — the API
                                          receives the input AS JSON, so its
                                          serialisation IS the token surface
  image                                   (w x h)/750, capped at 1600 (images
                                          are downscaled to ~1.15MP); the
                                          dimensions are sniffed from the
                                          decoded header, and a format we
                                          cannot read degrades to a documented
                                          flat estimate — NEVER to base64/4
  thinking `signature`                    EXCLUDED (not tokens)
  image base64 payload                    EXCLUDED (the pixels are billed, not
                                          the transport encoding)
  JSON envelope (keys, quoting, escapes)  EXCLUDED

**Every source is a row, and the rows close.** `totals.est_tokens` is defined
as Σ rows — there is no residual bucket to hide in. What the raw bytes carried
but the count deliberately excludes is reported in `excluded` (signature /
image-payload / envelope chars) so the gap between the file size and the token
estimate is stated, never swallowed.
"""
import base64
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


# ─── Image accounting ─────────────────────────────────────────────────────────
# The API bills an image at roughly (width x height)/750 tokens and downscales
# anything above ~1.15 megapixels, so 1600 is the practical ceiling. Transcript
# image blocks carry no dimensions — only base64 — so the dimensions are read
# out of the DECODED HEADER (pure stdlib: PNG/GIF/JPEG/WEBP magic). A format we
# cannot parse degrades to a flat estimate, which is wrong by a factor of ~2 at
# worst; counting the base64 instead was wrong by a factor of ~100.
IMAGE_PX_PER_TOKEN = 750
IMAGE_MAX_TOKENS = 1600         # ~1.15MP downscale ceiling
IMAGE_FLAT_TOKENS = 1200        # documented estimate when dims are unreadable
_SNIFF_B64_CHARS = 65536        # enough base64 to clear an EXIF/ICC preamble


def _sniff_dims(data):
    """(w, h) read from an image header, or None. Never raises."""
    if not isinstance(data, str) or len(data) < 32:
        return None
    try:
        chunk = data[:_SNIFF_B64_CHARS]
        raw = base64.b64decode(chunk[:len(chunk) // 4 * 4])
    except Exception:
        return None
    try:
        if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
            return (int.from_bytes(raw[16:20], "big"),
                    int.from_bytes(raw[20:24], "big"))
        if raw[:6] in (b"GIF87a", b"GIF89a") and len(raw) >= 10:
            return (int.from_bytes(raw[6:8], "little"),
                    int.from_bytes(raw[8:10], "little"))
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            if raw[12:16] == b"VP8X" and len(raw) >= 30:
                return (int.from_bytes(raw[24:27], "little") + 1,
                        int.from_bytes(raw[27:30], "little") + 1)
            if raw[12:16] == b"VP8 " and len(raw) >= 30:
                return (int.from_bytes(raw[26:28], "little") & 0x3FFF,
                        int.from_bytes(raw[28:30], "little") & 0x3FFF)
            return None
        if raw[:2] == b"\xff\xd8":          # JPEG: walk to the SOF marker
            i = 2
            while i + 9 < len(raw):
                if raw[i] != 0xFF:
                    i += 1
                    continue
                m = raw[i + 1]
                if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                    i += 2
                    continue
                seg = int.from_bytes(raw[i + 2:i + 4], "big")
                if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                    return (int.from_bytes(raw[i + 7:i + 9], "big"),
                            int.from_bytes(raw[i + 5:i + 7], "big"))
                if seg < 2:
                    return None
                i += 2 + seg
    except Exception:
        return None
    return None


def image_tokens(block):
    """(est_tokens, label, b64_chars) for an image content block."""
    src = block.get("source") if isinstance(block, dict) else None
    data = src.get("data") if isinstance(src, dict) else None
    b64 = len(data) if isinstance(data, str) else 0
    dims = _sniff_dims(data)
    if dims and dims[0] > 0 and dims[1] > 0:
        w, h = dims
        est = max(1, min(IMAGE_MAX_TOKENS, (w * h) // IMAGE_PX_PER_TOKEN))
        return est, f"{w}x{h}", b64
    return IMAGE_FLAT_TOKENS, "dimensions unreadable", b64


def _block_text_chars(b) -> int:
    """Token-bearing chars of one content sub-block (images excluded — they
    carry their own row; unknown shapes are counted by their JSON, which is
    the only honest floor we have for them)."""
    if isinstance(b, str):
        return len(b)
    if not isinstance(b, dict):
        return len(json.dumps(b, ensure_ascii=False))
    if b.get("type") == "image":
        return 0
    if isinstance(b.get("text"), str):
        return len(b["text"])
    return len(json.dumps(b, ensure_ascii=False))


def _result_text_chars(content) -> int:
    """Token-bearing chars of a tool_result `content` (string or sub-blocks),
    excluding any image sub-block."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(_block_text_chars(b) for b in content)
    return len(json.dumps(content, ensure_ascii=False))


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

    # Pass 3: rows. EVERY token-bearing block becomes a row — nothing is
    # folded into a bucket the UI cannot show or the operator cannot act on.
    rows, seen = [], {}
    sig_chars = img_b64_chars = 0
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
                dg=None, locked=False, tid=None, est=None, bidx=None,
                sub=None, extra=None):
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
            elif kind in ("result", "injection", "tool_use", "image"):
                default = "stub"
            else:                              # prompt / narration
                default = "keep"
            rw = {
                "id": rid, "kind": kind, "uuid": uid, "tid": tid,
                "name": name, "hint": hint, "refs": refs,
                "chars": chars,
                "est_tokens": est_tokens(chars) if est is None else int(est),
                "dup": dup, "dup_of": seen.get(dg) if dup else None,
                "turn": turn, "recent": recent, "bidx": bidx, "sub": sub,
                "locked": locked, "default": default, "ts": r.get("timestamp"),
            }
            if extra:
                rw.update(extra)
            rows.append(rw)

        def image_row(b, bidx, sub=None):
            """One row per image block, wherever it sits."""
            nonlocal img_b64_chars
            est, label, b64 = image_tokens(b)
            img_b64_chars += b64
            rid = f"i:{uid}#{bidx}" + (f".{sub}" if sub is not None else "")
            src = (b.get("source") or {}) if isinstance(b, dict) else {}
            row(rid, "image", b64, est=est, bidx=bidx, sub=sub,
                name=src.get("media_type") or "image", hint=label,
                dg=_digest(src.get("data") or rid),
                extra={"dims": label, "b64_chars": b64})

        if t == "assistant":
            narr = 0
            n_think = 0
            for bidx, b in enumerate(content if isinstance(content, list)
                                     else []):
                if not isinstance(b, dict):
                    row(f"o:{uid}#{bidx}", "other",
                        len(json.dumps(b, ensure_ascii=False)), locked=True,
                        bidx=bidx, name="unrecognised block")
                    continue
                bt = b.get("type")
                if bt == "text":
                    narr += len(b.get("text") or "")
                elif bt == "thinking":
                    # TEXT only — the signature is opaque provenance, not
                    # tokens, and counting it once inflated BEFORE by ~51K.
                    sig_chars += len(b.get("signature") or "")
                    rid = f"th:{uid}" if n_think == 0 else f"th:{uid}#{n_think}"
                    n_think += 1
                    row(rid, "thinking", len(b.get("thinking") or ""),
                        locked=True, bidx=bidx, name="thinking")
                elif bt == "tool_use":
                    inp = b.get("input")
                    inp = inp if isinstance(inp, dict) else {}
                    name = b.get("name") or "?"
                    row(f"x:{b.get('id')}", "tool_use",
                        len(json.dumps(inp, ensure_ascii=False)),
                        tid=b.get("id"), bidx=bidx, name=name,
                        hint=input_hint(name, inp), dg=_digest([name, inp]))
                elif bt == "image":
                    image_row(b, bidx)
                else:
                    row(f"o:{uid}#{bidx}", "other",
                        len(json.dumps(b, ensure_ascii=False)), locked=True,
                        bidx=bidx, name=str(bt or "block"))
            if isinstance(content, str):
                narr = len(content)
            if narr:
                row(f"a:{uid}", "narration", narr)
        else:   # user
            txt_chars = 0
            for bidx, b in enumerate(content if isinstance(content, list)
                                     else []):
                if not isinstance(b, dict):
                    txt_chars += _block_text_chars(b)
                    continue
                bt = b.get("type")
                if bt == "tool_result":
                    tid = b.get("tool_use_id")
                    meta = tool_uses.get(tid) or {}
                    body = b.get("content", "")
                    chars = _result_text_chars(body)
                    if meta.get("kmcp"):
                        row(f"t:{tid}", "kmcp", chars, tid=tid, bidx=bidx,
                            name=meta.get("kmcp"),
                            refs=_kmcp_refs(meta["kmcp"], meta.get("input", {})),
                            dg=_digest(body))
                    else:
                        row(f"t:{tid}", "result", chars, tid=tid, bidx=bidx,
                            name=meta.get("name", "?"),
                            hint=input_hint(meta.get("name", "?"),
                                            meta.get("input", {})),
                            dg=_digest(body))
                    for si, sb in enumerate(body if isinstance(body, list)
                                            else []):
                        if isinstance(sb, dict) and sb.get("type") == "image":
                            image_row(sb, bidx, sub=si)
                elif bt == "image":
                    image_row(b, bidx)
                elif isinstance(b.get("text"), str):
                    txt_chars += len(b["text"])
                else:
                    row(f"o:{uid}#{bidx}", "other",
                        len(json.dumps(b, ensure_ascii=False)), locked=True,
                        bidx=bidx, name=str(bt or "block"))
            if isinstance(content, str):
                txt_chars = len(content)
            if txt_chars:
                txt = _text_of(content)
                if _is_real_prompt(r, txt):
                    row(f"u:{uid}", "prompt", txt_chars)
                else:
                    row(f"s:{uid}", "injection", txt_chars,
                        hint=txt.lstrip()[:60], dg=_digest(txt))

    groups: dict = {}
    for rw in rows:
        g = groups.setdefault(rw["kind"], {"count": 0, "chars": 0,
                                           "est_tokens": 0})
        g["count"] += 1
        g["chars"] += rw["chars"]
        g["est_tokens"] += rw["est_tokens"]

    # THE INVARIANT: totals IS the sum of the rows. There is no fixed bucket
    # left to hide in — every source the count knows about is addressable.
    row_tokens = sum(rw["est_tokens"] for rw in rows)
    row_chars = sum(rw["chars"] for rw in rows)
    # What the raw bytes carried that the token count deliberately excludes.
    # Stated, never swallowed — this is the gap between file size and estimate.
    surface = context_surface(records)
    envelope = max(0, surface - (row_chars - img_b64_chars) - sig_chars
                   - img_b64_chars)

    return {
        "version_ok": not bad,
        "unsupported_versions": bad,
        "rows": rows,
        "groups": groups,
        "turns": n_turns,
        "recent_turns": RECENT_TURNS,
        # Residual is 0 by construction; kept as a named line so that if the
        # arithmetic ever drifts the UI shows it instead of absorbing it.
        "residual_tokens": 0,
        "fixed_chars": 0,        # retired bucket — kept so old clients parse
        "fixed_tokens": 0,
        "excluded": {"signature_chars": sig_chars,
                     "image_payload_chars": img_b64_chars,
                     "json_envelope_chars": envelope},
        "surface_chars": surface,
        "totals": {"chars": row_chars, "est_tokens": row_tokens},
        "floor": dict(FLOOR_TOKENS),
    }


def surface_tokens(records, bash_kmcp=None) -> int:
    """The honest BEFORE/AFTER measure — Σ manifest rows, by construction the
    same number the manifest shows (one accounting model, one answer)."""
    return build_manifest(records, bash_kmcp=bash_kmcp)["totals"]["est_tokens"]


# ─── Redaction (the validated edit class, both copies) ────────────────────────
def _breadcrumb(row) -> str:
    if row["kind"] == "kmcp":
        refs = row.get("refs") or []
        label = f"kmcp {refs[0]}" + (f" (+{len(refs)-1})" if len(refs) > 1 else "") \
            if refs else f"kmcp {row.get('name') or 'read'}"
    elif row["kind"] == "result":
        hint = row.get("hint") or ""
        label = f"{row.get('name') or 'tool'}{(' ' + hint) if hint else ''}"
    elif row["kind"] == "tool_use":
        hint = row.get("hint") or ""
        label = f"{row.get('name') or 'tool'} input" + \
            (f" {hint}" if hint else "")
    elif row["kind"] == "injection":
        label = "injected context"
    elif row["kind"] == "narration":
        label = "assistant narration"
    else:
        label = row["kind"]
    return f"[CR: {label} — {human(row['chars'])} elided]"


def _image_crumb(row) -> str:
    """The text block a stubbed image becomes. Says what was there and what it
    cost, so the model can ask for it back rather than silently lose it."""
    return (f"[image removed by CR: {row.get('dims') or 'unknown size'}, "
            f"~{row.get('est_tokens', 0)} tokens]")


def _stub_input(crumb: str) -> dict:
    """The replacement for an elided tool_use input. The block keeps its `id`
    and `name`, so the tool_use/tool_result pair still matches — only the
    input's payload is swapped for a breadcrumb (a tool_use INPUT is a
    free-form object; extra keys inside it are not the block-level extra keys
    the API rejects)."""
    return {"_cr_elided": crumb}


def _stub_image(content, rw, crumb) -> bool:
    """Swap an image block for a text breadcrumb, in the message content or
    inside a tool_result's sub-block list. Positional first, then a scan, so a
    manifest built against a slightly different copy still lands."""
    if not isinstance(content, list):
        return False
    bidx, sub = rw.get("bidx"), rw.get("sub")
    text_block = {"type": "text", "text": crumb}

    def _swap(lst, i):
        if isinstance(i, int) and 0 <= i < len(lst) \
                and isinstance(lst[i], dict) and lst[i].get("type") == "image":
            lst[i] = dict(text_block)
            return True
        for j, b in enumerate(lst):
            if isinstance(b, dict) and b.get("type") == "image":
                lst[j] = dict(text_block)
                return True
        return False

    if sub is None:
        return _swap(content, bidx)
    host = content[bidx] if isinstance(bidx, int) and 0 <= bidx < len(content) \
        else None
    if not (isinstance(host, dict) and host.get("type") == "tool_result"):
        host = next((b for b in content if isinstance(b, dict)
                     and b.get("type") == "tool_result"), None)
    if host is None:
        return False
    body = host.get("content")
    if not isinstance(body, list):
        # the parent tool_result was already stubbed — the image is gone with
        # it; that is the outcome asked for, not a failure.
        return True
    return _swap(body, sub)


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
    # Sub-block kinds first: an image inside a tool_result must be replaced
    # BEFORE its parent result collapses into a breadcrumb string, or the
    # locator has nothing left to aim at.
    ordered = sorted([r for r in stub_ids if isinstance(r, str)],
                     key=lambda r: 0 if (by_id.get(r) or {}).get("kind")
                     == "image" else 1)
    for rid in ordered:
        rw = by_id.get(rid)
        if rw is None or rw.get("locked"):
            ignored.append(rid)
            continue
        rec = by_uuid.get(rw["uuid"])
        if rec is None:
            ignored.append(rid)
            continue
        crumb = _image_crumb(rw) if rw["kind"] == "image" else _breadcrumb(rw)
        content = (rec.get("message") or {}).get("content")
        did = False
        if rw["kind"] == "image":
            did = _stub_image(content, rw, crumb)
        elif rw["kind"] == "tool_use":
            for b in content if isinstance(content, list) else []:
                if (isinstance(b, dict) and b.get("type") == "tool_use"
                        and b.get("id") == rw["tid"]):
                    b["input"] = _stub_input(crumb)     # id + name preserved
                    did = True
        elif rw["kind"] in ("result", "kmcp"):
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
    before_tok = manifest["totals"]["est_tokens"]
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
    after_tok = surface_tokens(records, bash_kmcp=bash_kmcp)
    return {
        "new_session": new_id,
        "path": str(dst),
        "before_chars": before, "after_chars": after,
        "before_tokens": before_tok, "after_tokens": after_tok,
        "saved_pct": round((before_tok - after_tok) / before_tok * 100, 1)
        if before_tok else 0,
        **stats,
    }
