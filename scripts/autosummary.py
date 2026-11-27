#!/usr/bin/env python3
"""claude-session-db autosummary: summarize finished Claude Code sessions off-session.

Stopgap (2026-11-26) so the owner never has to type /session-summary. Runs under
launchd (com.claude-session-db.autosummary). Per run:

  1. Find candidate main transcripts in ~/.claude/projects/*/<uuid>.jsonl:
     interactive (entrypoint "cli"), idle >= MIN_IDLE, last activity after SINCE,
     not open in a live process (~/.claude/sessions/<pid>.json), at least
     MIN_PROMPTS real user prompts, and not already in the local ledger.
     A session already summarized is a candidate again once it was resumed: at
     least MIN_DELTA_PROMPTS real prompts after its watermark (continuation pass).
  2. For up to MAX_PER_RUN of them (oldest first): render `csd digest --cr` to a
     file (`--since <watermark>` for a continuation), then run ONE headless
     `claude -p` as the /session-summary worker over that file. Pass N > 1 files a
     `Session (cont. N)` entry tagged delta-capture (skill/session-summary W2).
  3. Append the outcome to the ledger (JSONL). The ledger, not the csd archive,
     is the source of truth for "already done": the archive's mark-summarized
     stamp is best-effort while its ingest is stalled. Each ok record carries
     `pass` and `watermark` (the last transcript timestamp the pass covered); a
     kmcp session entry for the same session_id (a hand-run pass) moves the
     watermark to its ended_at.

DB-free: needs only the transcripts, csd digest (reads JSONL) and claude.
Docs: claude_session_db:reference/autosummary (kmcp). Installed copy: ~/.local/libexec/claude-session-db-autosummary (see launchd/claude-session-db-autosummary.wrapper).
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

HOME = Path.home()
PROJECTS = HOME / ".claude" / "projects"
LIVE_DIR = HOME / ".claude" / "sessions"
STATE = HOME / ".local" / "state" / "claude-session-db" / "autosummary"
WORK = STATE / "work"  # claude -p cwd; its own transcripts land in a project dir we skip
DIGESTS = STATE / "digests"
RUNS = STATE / "runs"
LEDGER = STATE / "ledger.jsonl"
LOCK = STATE / "run.lock"
CSD = "/Users/andrew/GitHub/claude-session-db/.venv/bin/csd"
CLAUDE = str(HOME / ".local" / "bin" / "claude")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
WATERMARK_SLACK_S = 1  # same slack as session_digest --since
REF_RE = re.compile(r"^[a-z0-9_]+:session/\S+$")
SKIP_PROJECT_DIRS = {str(WORK).replace("/", "-").replace(".", "-")}

DEFAULTS = dict(
    min_idle_min=120,
    since="2026-11-25T00:00:00Z",
    max_per_run=3,
    min_prompts=2,
    min_delta_prompts=1,
    model="sonnet",
    mode="capture",
    timeout_s=1500,
    max_attempts=2,
)

FULL_PROMPT = """SESSION-SUMMARY WORKER — you are the worker for /session-summary. Do not invoke that skill. You run unattended under launchd (com.claude-session-db.autosummary): no human will answer, so never ask a question; decide and report.
0. Load the kmcp tools with ToolSearch (query "select:mcp__claude_ai_kmcp__search,mcp__claude_ai_kmcp__get_entry,mcp__claude_ai_kmcp__get_entries,mcp__claude_ai_kmcp__get_entity_schema,mcp__claude_ai_kmcp__import_entries,mcp__claude_ai_kmcp__create_entry,mcp__claude_ai_kmcp__create_relationship,mcp__claude_ai_kmcp__update_entry,mcp__claude_ai_kmcp__patch_content"); ToolSearch waits for a connecting server. If they cannot be loaded, write nothing and reply only: RESULT: no-kmcp | stamp: skipped
   Then check for a duplicate: search kmcp (query "{sid}", entity_types ["session"]). If any hit's content.session_id equals {sid} exactly (get_entry to confirm), write nothing and reply only: RESULT: duplicate <app:path> | stamp: skipped
1. Load the procedure: get_entry(application="claudecode", path="skill/session-summary", sections=["body"]), plus the entry's domain_refs_eager in ONE get_entries call (get_entries takes entries=[{{application, path}}, ...]; split each app:path ref into an object, never pass strings). Skip Step 0; start at "Worker ground rules", then Steps 1-9.
2. The conversation is ONLY the file {digest}. Read it. Do NOT open the raw transcript and do NOT run plain `csd digest`. Expand a stub only when it is load-bearing:
   {csd} digest {sid} --row <row-id>
   For any csd DB command (mark-summarized, query), first run in the same Bash call: set -a; source ~/.config/claude-session-db/env; set +a; unset DATABASE_URL
3. session_id = {sid}, character-for-character (it is also the digest header). Never your own id.
4. Trigger: uuid. {pass_line}. Degraded note: the csd archive ingest may lag, so this session may not be archived yet. If mark-summarized fails with "not found in archive", report it and do not retry. Overrides: none. project_path: {project_path}.
5. Return only the Step 9 report, and end it with exactly one line:
   RESULT: <session entry app:path | events-only | none> | stamp: <ok|failed|skipped>
"""


CAPTURE_PROMPT = """SESSION CAPTURE WORKER (csd autosummary, capture mode). You run unattended: never ask a question; decide and report. Your one job is to file a faithful record of ONE finished Claude Code session in kmcp. Out of scope here (a later pass owns them): searching for owner entries to patch, writing lessons, creating tasks, editing any existing entry. Keep it to about 5 turns.
1. In ONE turn: ToolSearch (query "select:mcp__claude_ai_kmcp__get_entity_schema,mcp__claude_ai_kmcp__import_entries,mcp__claude_ai_kmcp__list_entries") and Read the digest file {digest} in full (it is the whole conversation: user prompts and assistant narration verbatim; tool calls are one-line stubs; KMCP READ/WRITE lines are references). ToolSearch waits for a connecting server; if the kmcp tools still cannot be loaded, write nothing and reply only: RESULT: no-kmcp | stamp: skipped
2. In ONE turn: get_entity_schema for "session" and for "event" (two parallel calls). Compose to the live schema, never from memory.
3. Pick the application: the kmcp app the session's work belongs to (from cwd {project_path} and the content; e.g. controltech, controltech_code, infrastructure, claudecode, knowledge_mcp, p6_eppm, ai_services, elford). If several, the primary one.
4. Write everything in ONE import_entries call (dry_run first only if unsure; on_conflict "fail"), YAML with every scalar quoted:
   - ONE session entry at session/<start YYYY-MM-DD>/<kebab-topic>, title "Session: <specific title>", tags [topical..., "auto-summary", "capture-only"]. content: summary (2-5 sentences, specific: what was asked, what was done, what state it ended in), session_id "{sid}" (exactly), project_path, started_at/ended_at from the digest span line, errors_encountered [{{error, resolution}}] (real stumbles only, may be empty), follow_up (every open item, unanswered owner question and unconfirmed result, one line each; mark owner decisions "OWNER:"), tools_used, linked_entries (app:path refs the session READ for understanding, from KMCP READ lines), resume_context (object of categorized {{path, relevance}} lists: implementation/supporting/open_tasks; only paths that appear in the digest).
   - One event per concrete state change or owner decision that the digest shows actually happened (merged PR, deploy, config change, kmcp entry written, decision): event/<YYYY-MM-DD>/<kebab-slug> in the same app, with summary, event_type (schema enum), details, actor "claude-code", occurred_at, scope (array). Skip events for pure Q&A. Before writing events, list_entries(application=<same app>, entity_type="event", path_prefix="event/<date>") once per date (NO trailing slash: a trailing slash returns 0 results) and skip any event the session itself already filed (its KMCP WRITE lines show them).
   - relationships on each event document: see_also to the session path.
   Never invent facts: if something is only in an unexpanded stub, say so in the summary or leave it out.
5. Stamp (best effort): one Bash call: set -a; source ~/.config/claude-session-db/env; set +a; unset DATABASE_URL; {csd} mark-summarized {sid} --app <app> --path <session path> --pass 1. If it fails, do not retry.
Reply with at most 5 lines, the last exactly: RESULT: <app:session path | none> | stamp: <ok|failed|skipped>
"""


DELTA_PROMPT = """SESSION CAPTURE WORKER, CONTINUATION PASS {n} (csd autosummary, capture mode). You run unattended: never ask a question; decide and report. ONE Claude Code session was already captured up to {since} (latest earlier pass: {prior}); it was then resumed. Your one job is to file a faithful record of ONLY the work after {since}. Out of scope here (a later pass owns them): searching for owner entries to patch, writing lessons, creating tasks, editing any existing entry. Keep it to about 5 turns.
1. In ONE turn: ToolSearch (query "select:mcp__claude_ai_kmcp__search,mcp__claude_ai_kmcp__get_entity_schema,mcp__claude_ai_kmcp__import_entries,mcp__claude_ai_kmcp__list_entries") and Read the digest file {digest} in full. It holds ONLY the records after {since}; its "delta span:" line is the window. User prompts and assistant narration are verbatim, tool calls are one-line stubs, KMCP READ/WRITE lines are references. ToolSearch waits for a connecting server; if the kmcp tools still cannot be loaded, write nothing and reply only: RESULT: no-kmcp | stamp: skipped
2. In ONE turn: get_entity_schema for "session" and for "event", and search kmcp (query "{sid}", entity_types ["session"], detail "full"). If a session entry other than {prior} has content.session_id exactly "{sid}" and an ended_at after the window start, a later pass already exists: write nothing and reply only: RESULT: duplicate <app:path> | stamp: skipped
3. If the window holds no substantive work (only acknowledgements, a closing remark, a question answered with nothing done or decided), write nothing and reply only: RESULT: none | stamp: skipped
4. Application: the app of {prior}, unless the window's work clearly belongs to another kmcp app (cwd {project_path}).
5. Write everything in ONE import_entries call (dry_run first only if unsure; on_conflict "fail"), YAML with every scalar quoted:
   - ONE session entry at session/<window END YYYY-MM-DD>/<kebab-topic of the new work> (never {prior}'s path), title "Session (cont. {n}): <specific title of the new work>", tags [topical..., "auto-summary", "capture-only", "delta-capture"]. content: summary (2-5 sentences about the window only; name {prior} as the earlier pass), session_id "{sid}" (exactly), project_path, started_at/ended_at from the digest's delta span line (the window, not the whole session), errors_encountered [{{error, resolution}}] (real stumbles in the window, may be empty), follow_up (every open item, unanswered owner question and unconfirmed result, one line each; mark owner decisions "OWNER:"), tools_used, linked_entries ("{prior}" plus app:path refs READ in the window), resume_context (object of categorized {{path, relevance}} lists: implementation/supporting/open_tasks; only paths in the digest). No session-wide metrics.
   - relationships on the session document: see_also to {prior}, description "continues {prior}, pass {n} of this session" (skip it if the prior is "unknown").
   - One event per concrete state change or owner decision that the window shows actually happened (merged PR, deploy, config change, kmcp entry written, decision): event/<YYYY-MM-DD>/<kebab-slug> in the same app, with summary, event_type (schema enum), details, actor "claude-code", occurred_at, scope (array), relationships see_also to the new session path. Skip events for pure Q&A. Before writing events, list_entries(application=<same app>, entity_type="event", path_prefix="event/<date>") once per date (NO trailing slash: a trailing slash returns 0 results) and skip any event already filed (the window's KMCP WRITE lines show them).
   Never invent facts: if something is only in an unexpanded stub, say so in the summary or leave it out.
6. Stamp (best effort): one Bash call: set -a; source ~/.config/claude-session-db/env; set +a; unset DATABASE_URL; {csd} mark-summarized {sid} --app <app> --path <session path> --pass {n}. If it fails, do not retry.
Reply with at most 5 lines, the last exactly: RESULT: <app:session path | duplicate app:path | none> | stamp: <ok|failed|skipped>
"""


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def log(msg: str) -> None:
    print(f"{now_utc().strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}", flush=True)


def read_ledger() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    if LEDGER.exists():
        for line in LEDGER.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            out.setdefault(rec.get("session_id", ""), []).append(rec)
    return out


def append_ledger(rec: dict) -> None:
    rec = {"ts": now_utc().isoformat(), **rec}
    with LEDGER.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def live_session_ids() -> set[str]:
    ids: set[str] = set()
    for f in LIVE_DIR.glob("*.json"):
        try:
            pid = int(f.stem)
            os.kill(pid, 0)
        except (ValueError, ProcessLookupError):
            continue
        except PermissionError:
            pass
        try:
            ids.add(json.loads(f.read_text()).get("sessionId", ""))
        except (OSError, json.JSONDecodeError):
            continue
    return ids


def parse_ts(s) -> dt.datetime | None:
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


class Info(NamedTuple):
    interactive: bool
    prompts: int       # real user prompts in the whole transcript
    new_prompts: int   # real user prompts after `since` (== prompts when since is None)
    cwd: str
    last_ts: str       # timestamp of the last record (the watermark a pass covers)


def inspect_transcript(path: Path, since: dt.datetime | None = None) -> Info:
    """One pass over the JSONL: interactivity, real prompt counts, cwd, last timestamp."""
    cutoff = since + dt.timedelta(seconds=WATERMARK_SLACK_S) if since else None
    interactive = False
    prompts = new_prompts = 0
    cwd = last_ts = ""
    with path.open("rb") as fh:
        for raw in fh:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            ep = rec.get("entrypoint")
            if ep == "cli":
                interactive = True
            elif ep and ep != "cli" and not interactive:
                # first entrypoint seen is not interactive (sdk-cli etc.)
                return Info(False, 0, 0, "", "")
            if not cwd and rec.get("cwd"):
                cwd = rec["cwd"]
            if rec.get("timestamp"):
                last_ts = rec["timestamp"]
            if rec.get("type") == "user" and not rec.get("isMeta") and not rec.get("isSidechain"):
                c = (rec.get("message") or {}).get("content")
                if isinstance(c, str) and c.strip() and not c.lstrip().startswith("<"):
                    prompts += 1
                    ts = parse_ts(rec.get("timestamp"))
                    if cutoff is None or (ts is not None and ts > cutoff):
                        new_prompts += 1
    return Info(interactive, prompts, new_prompts, cwd, last_ts)


class Job(NamedTuple):
    sid: str
    path: Path
    cwd: str
    mtime: float
    n: int              # pass number; 1 = full session
    since: str | None   # watermark the window opens at (pass N > 1)
    prior: str | None   # app:path of the latest earlier pass, if known
    last_ts: str


def _pass(rec: dict) -> int:
    try:
        return int(rec.get("pass") or 1)
    except (TypeError, ValueError):
        return 1


def _ref(rec: dict) -> str | None:
    res = str(rec.get("result") or "").removeprefix("duplicate ").strip()
    return res if REF_RE.match(res) else None


def plan(cfg, sid: str, path: Path, recs: list[dict], via: str = "hourly") -> Job | None:
    """Decide what, if anything, this transcript needs: pass 1, a continuation, or nothing."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    oks = [r for r in recs if r.get("status") == "ok"]
    if not oks:
        skips = [r for r in recs if r.get("status") == "skip"]
        if any(r.get("reason") == "not-interactive" for r in skips):
            return None
        if skips and mtime <= (skips[-1].get("mtime") or 0) + 1:
            return None  # too few prompts last time and nothing new since
        if sum(1 for r in recs if r.get("status") == "failed" and _pass(r) == 1) >= cfg.max_attempts:
            return None
        info = inspect_transcript(path)
        if not info.interactive or info.prompts < cfg.min_prompts:
            append_ledger({"session_id": sid, "status": "skip",
                           "reason": "not-interactive" if not info.interactive else f"prompts={info.prompts}",
                           "mtime": mtime, "via": via})
            return None
        return Job(sid, path, info.cwd, mtime, 1, None, None, info.last_ts)
    # Already summarized: a continuation is due only if the session was resumed and
    # gained real prompts past the watermark of its latest pass.
    latest = max(oks, key=lambda r: (parse_ts(r.get("watermark") or r.get("ts")) or
                                     dt.datetime.min.replace(tzinfo=dt.timezone.utc)))
    since = latest.get("watermark") or latest.get("ts")
    since_dt = parse_ts(since)
    if since_dt is None or mtime <= since_dt.timestamp() + WATERMARK_SLACK_S:
        return None
    n = max(_pass(r) for r in oks) + 1
    if sum(1 for r in recs if r.get("status") == "failed" and _pass(r) == n) >= cfg.max_attempts:
        return None
    info = inspect_transcript(path, since_dt)
    if not info.interactive or info.new_prompts < cfg.min_delta_prompts:
        return None
    prior = next((_ref(r) for r in reversed(oks) if _ref(r)), None)
    return Job(sid, path, info.cwd, mtime, n, since, prior, info.last_ts)


def candidates(cfg, ledger) -> list[Job]:
    since = dt.datetime.fromisoformat(cfg.since.replace("Z", "+00:00")).timestamp()
    cutoff = time.time() - cfg.min_idle_min * 60
    live = live_session_ids()
    found = []
    for path in PROJECTS.glob("*/*.jsonl"):
        sid = path.stem
        if not UUID_RE.match(sid) or path.parent.name in SKIP_PROJECT_DIRS:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < since or mtime > cutoff or sid in live:
            continue
        job = plan(cfg, sid, path, ledger.get(sid, []))
        if job:
            found.append(job)
    found.sort(key=lambda j: j.mtime)
    return found


KCLI = str(HOME / ".local" / "bin" / "knowledge-cli")


def _kcli(tool: str, args: dict):
    env = {**os.environ, "KNOWLEDGE_ALLOW_UNAUTH_LOCAL": "1"}  # local trusted read-only lookups
    r = subprocess.run([KCLI, "call", tool, json.dumps(args)], capture_output=True,
                       text=True, timeout=60, env=env)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout)[-300:])
    return json.loads(r.stdout)


def existing_summaries(sid: str) -> list[tuple[str, str]]:
    """Cheap pre-check (no model): kmcp session entries whose session_id is exactly sid.

    Returns [(app:path, watermark)], watermark = the entry's ended_at (else its
    updated_at), oldest first. Raises if kmcp is unreachable (caller then lets the
    worker do the check itself)."""
    hits = _kcli("search", {"query": sid, "entity_types": ["session"], "detail": "minimal", "limit": 10})
    out = []
    for h in hits.get("results", []):
        e = _kcli("get_entry", {"application": h["application"], "path": h["path"],
                                "entity_type": "session", "sections": ["session_id", "ended_at"],
                                "include_relationships": False})
        content = e.get("content") or {}
        if str(content.get("session_id") or "").strip() != sid:
            continue
        wm = content.get("ended_at") or e.get("updated_at") or ""
        if parse_ts(wm):
            out.append((f"{h['application']}:{h['path']}", str(wm)))
    out.sort(key=lambda x: parse_ts(x[1]))
    return out


def reconcile_with_kmcp(cfg, job: Job) -> Job | None:
    """Fold passes written outside this job (a hand-run /session-summary) into the plan.

    A kmcp entry for this session that ends after the job's window start is recorded as
    an ok pass and moves the watermark; returns the job still due, or None."""
    try:
        found = existing_summaries(job.sid)
    except Exception as exc:  # kmcp down or CLI drift: the worker does its own check
        log(f"precheck unavailable for {job.sid}: {exc}")
        return job
    start = parse_ts(job.since)
    known = {_ref(r) for r in read_ledger().get(job.sid, []) if r.get("status") == "ok"}
    later = [(ref, wm) for ref, wm in found
             if ref not in known and (start is None or parse_ts(wm) > start)]
    if not later:
        return job
    ref, wm = later[-1]
    n_done = max(job.n - 1 + len(later), len(found))
    append_ledger({"session_id": job.sid, "status": "ok", "result": f"duplicate {ref}",
                   "stamp": "skipped", "cost_usd": 0, "note": "precheck",
                   "pass": n_done, "watermark": wm})
    log(f"skip  {job.sid} pass {job.n} already in kmcp: {ref} (to {wm})")
    info = inspect_transcript(job.path, parse_ts(wm))
    if info.new_prompts < cfg.min_delta_prompts:
        return None
    log(f"      {job.sid} has {info.new_prompts} prompt(s) after {wm}: continuation pass {n_done + 1}")
    return job._replace(n=n_done + 1, since=wm, prior=ref, last_ts=info.last_ts, cwd=info.cwd or job.cwd)


def summarize(cfg, job: Job) -> tuple[bool, bool]:
    """Run one worker. Returns (keep_going, ran_worker); keep_going False on a usage limit."""
    job = reconcile_with_kmcp(cfg, job)
    if job is None:
        return True, False
    sid, path, cwd, n = job.sid, job.path, job.cwd, job.n
    digest = DIGESTS / f"{sid}-p{n}.txt"
    cmd = [CSD, "digest", sid, "--cr", "--out", str(digest)]
    if n > 1:
        cmd[4:4] = ["--since", job.since]
    d = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if d.returncode != 0:
        append_ledger({"session_id": sid, "status": "failed", "stage": "digest", "pass": n,
                       "error": (d.stderr or d.stdout)[-400:]})
        return True, False
    accounting = (d.stderr.strip().splitlines() or [""])[-1]
    fmt = dict(sid=sid, digest=digest, csd=CSD, project_path=cwd or "unknown", n=n,
               since=job.since, prior=job.prior or "unknown")
    if cfg.mode == "capture":
        prompt = (CAPTURE_PROMPT if n == 1 else DELTA_PROMPT).format(**fmt)
    else:
        pass_line = "Pass 1: full" if n == 1 else f"Pass {n}: delta since {job.since}, prior {job.prior or 'unknown'}"
        prompt = FULL_PROMPT.format(pass_line=pass_line, **fmt)
    (RUNS / f"{sid}-p{n}.prompt").write_text(prompt)
    log(f"start {sid} pass {n} ({path.parent.name}) {accounting}")
    append_ledger({"session_id": sid, "status": "started", "mtime": path.stat().st_mtime,
                   "accounting": accounting, "mode": cfg.mode, "model": cfg.model,
                   "pass": n, "since": job.since, "watermark": job.last_ts})
    t0 = time.time()
    try:
        r = subprocess.run(
            # --tools keeps ToolSearch so the claude.ai connectors stay deferred (naming
            # only Read,Bash loads every connector's schemas: 201K base context, measured
            # 2026-11-26); --disable-slash-commands drops the skills listing.
            [CLAUDE, "-p", prompt, "--model", cfg.model,
             "--permission-mode", "bypassPermissions",
             "--tools", "Read,Bash,ToolSearch", "--disable-slash-commands",
             "--output-format", "json"],
            cwd=WORK, capture_output=True, text=True, timeout=cfg.timeout_s,
            stdin=subprocess.DEVNULL, env={**os.environ, "CSD_AUTOSUMMARY": "1"},
        )
    except subprocess.TimeoutExpired:
        append_ledger({"session_id": sid, "status": "failed", "stage": "claude", "error": "timeout",
                       "pass": n, "secs": round(time.time() - t0)})
        return True, True
    (RUNS / f"{sid}-p{n}.json").write_text(r.stdout)
    (RUNS / f"{sid}-p{n}.err").write_text(r.stderr)
    secs = round(time.time() - t0)
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        out = {}
    text = out.get("result") or ""
    m = re.search(r"^RESULT:\s*(.+?)\s*\|\s*stamp:\s*(\S+)", text, re.M)
    rec = {"session_id": sid, "pass": n, "secs": secs, "exit": r.returncode,
           "cost_usd": out.get("total_cost_usd"), "turns": out.get("num_turns"),
           "worker_session": out.get("session_id")}
    limit_hit = bool(re.search(r"usage limit|rate limit|spend limit|429", text + r.stderr, re.I))
    if r.returncode == 0 and not out.get("is_error") and m and m.group(1) != "no-kmcp":
        # "none" (nothing substantive in the window) is ok too: the watermark still advances.
        rec.update(status="ok", result=m.group(1), stamp=m.group(2), watermark=job.last_ts)
    else:
        rec.update(status="failed", stage="claude",
                   result=m.group(1) if m else None,
                   error=(text or r.stderr)[-400:])
    append_ledger(rec)
    log(f"done  {sid} pass {n} {rec['status']} {rec.get('result')} stamp={rec.get('stamp')} {secs}s cost={rec['cost_usd']}")
    if m and m.group(1) == "no-kmcp":
        log("kmcp tools unavailable to claude -p; stopping run")
        return False, True
    return (not limit_hit), True


def on_close(cfg, sid: str) -> int:
    """Called (detached) by the SessionEnd hook. The ledger and the hourly run are the
    safety net: anything skipped or failed here is retried by the fallback schedule.
    A session closed again after a resume gets a continuation pass for the new work."""
    if not UUID_RE.match(sid or ""):
        return 0
    # The hook fires while the process is still exiting; wait for its pid file to go.
    for _ in range(60):
        if sid not in live_session_ids():
            break
        time.sleep(2)
    else:
        log(f"on-close {sid}: still live after 120 s (resumed elsewhere?); leaving it to the hourly run")
        return 0
    time.sleep(5)  # let the final transcript writes land
    paths = [p for p in PROJECTS.glob(f"*/{sid}.jsonl") if p.parent.name not in SKIP_PROJECT_DIRS]
    if not paths:
        return 0  # never prompted: Claude Code writes no transcript
    path = paths[0]
    if not plan(cfg, sid, path, read_ledger().get(sid, []), via="on-close"):
        return 0
    lock = LOCK.open("w")
    deadline = time.time() + 1800
    while True:  # wait behind an hourly run or another close instead of dropping this one
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() > deadline:
                log(f"on-close {sid}: lock busy for 30 min; leaving it to the hourly run")
                return 0
            time.sleep(15)
    job = plan(cfg, sid, path, read_ledger().get(sid, []), via="on-close")  # re-plan under the lock
    if not job:
        return 0
    log(f"on-close {sid} pass {job.n} ({path.parent.name})")
    if cfg.dry_run:
        log(f"on-close {sid}: dry run, would summarize pass {job.n}")
        return 0
    summarize(cfg, job)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for k, v in DEFAULTS.items():
        ap.add_argument("--" + k.replace("_", "-"), type=type(v), default=v)
    ap.add_argument("--dry-run", action="store_true", help="list candidates, run nothing")
    ap.add_argument("--only", help="summarize this one session id (ignores since/idle filters); "
                                   "a summarized session gets its next continuation pass")
    ap.add_argument("--on-close", metavar="SID",
                    help="SessionEnd-hook entry: wait for SID's process to exit, then run its "
                         "pass 1 or its next continuation pass if one is due")
    cfg = ap.parse_args()

    for d in (STATE, WORK, DIGESTS, RUNS):
        d.mkdir(parents=True, exist_ok=True)
    if cfg.on_close:
        return on_close(cfg, cfg.on_close)
    lock = LOCK.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("previous run still active; exiting")
        return 0

    ledger = read_ledger()
    if cfg.only:
        paths = [p for p in PROJECTS.glob(f"*/{cfg.only}.jsonl") if p.parent.name not in SKIP_PROJECT_DIRS]
        if not paths:
            log(f"no transcript for {cfg.only}")
            return 1
        job = plan(cfg, cfg.only, paths[0], ledger.get(cfg.only, []), via="only")
        if not job:
            log(f"nothing due for {cfg.only} (see the ledger)")
            return 0
        work = [job]
    else:
        work = candidates(cfg, ledger)
    log(f"candidates={len(work)} max_per_run={cfg.max_per_run} model={cfg.model}")
    if cfg.dry_run:
        for j in work:
            what = "pass 1" if j.n == 1 else f"pass {j.n} since {j.since}"
            print(f"  {dt.datetime.fromtimestamp(j.mtime):%m-%d %H:%M}  {j.sid}  {j.path.parent.name}  {what}")
        return 0
    ran = 0
    for job in work:
        if ran >= cfg.max_per_run:
            break
        keep_going, ran_worker = summarize(cfg, job)
        ran += ran_worker
        if not keep_going:
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
