#!/usr/bin/env python3
"""claude-session-db autosummary: summarize finished Claude Code sessions off-session.

Stopgap (2026-11-26) so the owner never has to type /session-summary. Runs under
launchd (com.claude-session-db.autosummary). Per run:

  1. Find candidate main transcripts in ~/.claude/projects/*/<uuid>.jsonl:
     interactive (entrypoint "cli"), idle >= MIN_IDLE, last activity after SINCE,
     not open in a live process (~/.claude/sessions/<pid>.json), at least
     MIN_PROMPTS real user prompts, and not already in the local ledger.
  2. For up to MAX_PER_RUN of them (oldest first): render `csd digest --cr` to a
     file, then run ONE headless `claude -p` as the /session-summary worker
     (claudecode:skill/session-summary, Steps 1-9) over that file.
  3. Append the outcome to the ledger (JSONL). The ledger, not the csd archive,
     is the source of truth for "already done": the archive's mark-summarized
     stamp is best-effort while its ingest is stalled.

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
SKIP_PROJECT_DIRS = {str(WORK).replace("/", "-").replace(".", "-")}

DEFAULTS = dict(
    min_idle_min=120,
    since="2026-11-25T00:00:00Z",
    max_per_run=3,
    min_prompts=2,
    model="opus",
    timeout_s=1500,
    max_attempts=2,
)

PROMPT = """SESSION-SUMMARY WORKER — you are the worker for /session-summary. Do not invoke that skill. You run unattended under launchd (com.claude-session-db.autosummary): no human will answer, so never ask a question; decide and report.
0. If the mcp__claude_ai_kmcp__* tools are not available to you, write nothing and reply only: RESULT: no-kmcp | stamp: skipped
   Then check for a duplicate: search kmcp (query "{sid}", entity_types ["session"]). If any hit's content.session_id equals {sid} exactly (get_entry to confirm), write nothing and reply only: RESULT: duplicate <app:path> | stamp: skipped
1. Load the procedure: get_entry(application="claudecode", path="skill/session-summary", sections=["body"]), plus the entry's domain_refs_eager in ONE get_entries call (get_entries takes entries=[{{application, path}}, ...]; split each app:path ref into an object, never pass strings). Skip Step 0; start at "Worker ground rules", then Steps 1-9.
2. The conversation is ONLY the file {digest}. Read it. Do NOT open the raw transcript and do NOT run plain `csd digest`. Expand a stub only when it is load-bearing:
   {csd} digest {sid} --row <row-id>
   For any csd DB command (mark-summarized, query), first run in the same Bash call: set -a; source ~/.config/claude-session-db/env; set +a; unset DATABASE_URL
3. session_id = {sid}, character-for-character (it is also the digest header). Never your own id.
4. Trigger: uuid. Pass 1: full. Degraded note: the csd archive ingest may lag, so this session may not be archived yet. If mark-summarized fails with "not found in archive", report it and do not retry. Overrides: none. project_path: {project_path}.
5. Return only the Step 9 report, and end it with exactly one line:
   RESULT: <session entry app:path | events-only | none> | stamp: <ok|failed|skipped>
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


def inspect_transcript(path: Path) -> tuple[bool, int, str]:
    """Return (interactive, real_prompt_count, cwd) from one pass over the JSONL."""
    interactive = False
    prompts = 0
    cwd = ""
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
                return False, 0, ""
            if not cwd and rec.get("cwd"):
                cwd = rec["cwd"]
            if rec.get("type") == "user" and not rec.get("isMeta") and not rec.get("isSidechain"):
                c = (rec.get("message") or {}).get("content")
                if isinstance(c, str) and c.strip() and not c.lstrip().startswith("<"):
                    prompts += 1
    return interactive, prompts, cwd


def candidates(cfg, ledger) -> list[tuple[float, str, Path, str]]:
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
        recs = ledger.get(sid, [])
        if any(r.get("status") in ("ok", "skip") for r in recs):
            continue
        if sum(1 for r in recs if r.get("status") == "failed") >= cfg.max_attempts:
            continue
        interactive, prompts, cwd = inspect_transcript(path)
        if not interactive or prompts < cfg.min_prompts:
            append_ledger({"session_id": sid, "status": "skip",
                           "reason": "not-interactive" if not interactive else f"prompts={prompts}",
                           "mtime": mtime})
            continue
        found.append((mtime, sid, path, cwd))
    found.sort()
    return found


KCLI = str(HOME / ".local" / "bin" / "knowledge-cli")


def _kcli(tool: str, args: dict):
    env = {**os.environ, "KNOWLEDGE_ALLOW_UNAUTH_LOCAL": "1"}  # local trusted read-only lookups
    r = subprocess.run([KCLI, "call", tool, json.dumps(args)], capture_output=True,
                       text=True, timeout=60, env=env)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout)[-300:])
    return json.loads(r.stdout)


def existing_summary(sid: str) -> str | None:
    """Cheap pre-check (no model): a kmcp session entry whose session_id is exactly sid.

    Returns its app:path, or None. Raises if kmcp is unreachable (caller then lets the
    worker do the check itself)."""
    hits = _kcli("search", {"query": sid, "entity_types": ["session"], "detail": "minimal", "limit": 8})
    for h in hits.get("results", []):
        e = _kcli("get_entry", {"application": h["application"], "path": h["path"],
                                "entity_type": "session", "sections": ["session_id"]})
        stored = (e.get("content") or {}).get("session_id") or ""
        if str(stored).strip() == sid:
            return f"{h['application']}:{h['path']}"
    return None


def summarize(cfg, sid: str, path: Path, cwd: str) -> tuple[bool, bool]:
    """Run one worker. Returns (keep_going, ran_worker); keep_going False on a usage limit."""
    try:
        dup = existing_summary(sid)
    except Exception as exc:  # kmcp down or CLI drift: fall through to the worker's own check
        log(f"precheck unavailable for {sid}: {exc}")
        dup = None
    if dup:
        append_ledger({"session_id": sid, "status": "ok", "result": f"duplicate {dup}",
                       "stamp": "skipped", "cost_usd": 0, "note": "precheck"})
        log(f"skip  {sid} already summarized: {dup}")
        return True, False
    digest = DIGESTS / f"{sid}-p1.txt"
    d = subprocess.run([CSD, "digest", sid, "--cr", "--out", str(digest)],
                       capture_output=True, text=True, timeout=300)
    if d.returncode != 0:
        append_ledger({"session_id": sid, "status": "failed", "stage": "digest",
                       "error": (d.stderr or d.stdout)[-400:]})
        return True, False
    accounting = (d.stderr.strip().splitlines() or [""])[-1]
    prompt = PROMPT.format(sid=sid, digest=digest, csd=CSD, project_path=cwd or "unknown")
    (RUNS / f"{sid}.prompt").write_text(prompt)
    log(f"start {sid} ({path.parent.name}) {accounting}")
    append_ledger({"session_id": sid, "status": "started", "mtime": path.stat().st_mtime,
                   "accounting": accounting})
    t0 = time.time()
    try:
        r = subprocess.run(
            [CLAUDE, "-p", prompt, "--model", cfg.model,
             "--permission-mode", "bypassPermissions",
             "--disallowedTools", "Edit", "Write", "NotebookEdit", "Agent", "WebFetch", "WebSearch",
             "--output-format", "json"],
            cwd=WORK, capture_output=True, text=True, timeout=cfg.timeout_s,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        append_ledger({"session_id": sid, "status": "failed", "stage": "claude", "error": "timeout",
                       "secs": round(time.time() - t0)})
        return True, True
    (RUNS / f"{sid}.json").write_text(r.stdout)
    (RUNS / f"{sid}.err").write_text(r.stderr)
    secs = round(time.time() - t0)
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        out = {}
    text = out.get("result") or ""
    m = re.search(r"^RESULT:\s*(.+?)\s*\|\s*stamp:\s*(\S+)", text, re.M)
    rec = {"session_id": sid, "secs": secs, "exit": r.returncode,
           "cost_usd": out.get("total_cost_usd"), "turns": out.get("num_turns"),
           "worker_session": out.get("session_id")}
    limit_hit = bool(re.search(r"usage limit|rate limit|spend limit|429", text + r.stderr, re.I))
    if r.returncode == 0 and not out.get("is_error") and m and m.group(1) != "no-kmcp":
        rec.update(status="ok", result=m.group(1), stamp=m.group(2))
    else:
        rec.update(status="failed", stage="claude",
                   result=m.group(1) if m else None,
                   error=(text or r.stderr)[-400:])
    append_ledger(rec)
    log(f"done  {sid} {rec['status']} {rec.get('result')} stamp={rec.get('stamp')} {secs}s cost={rec['cost_usd']}")
    if m and m.group(1) == "no-kmcp":
        log("kmcp tools unavailable to claude -p; stopping run")
        return False, True
    return (not limit_hit), True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for k, v in DEFAULTS.items():
        ap.add_argument("--" + k.replace("_", "-"), type=type(v), default=v)
    ap.add_argument("--dry-run", action="store_true", help="list candidates, run nothing")
    ap.add_argument("--only", help="summarize this one session id (ignores since/idle filters)")
    cfg = ap.parse_args()

    for d in (STATE, WORK, DIGESTS, RUNS):
        d.mkdir(parents=True, exist_ok=True)
    lock = LOCK.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("previous run still active; exiting")
        return 0

    ledger = read_ledger()
    if cfg.only:
        paths = list(PROJECTS.glob(f"*/{cfg.only}.jsonl"))
        if not paths:
            log(f"no transcript for {cfg.only}")
            return 1
        _, _, cwd = inspect_transcript(paths[0])
        work = [(0.0, cfg.only, paths[0], cwd)]
    else:
        work = candidates(cfg, ledger)
    log(f"candidates={len(work)} max_per_run={cfg.max_per_run} model={cfg.model}")
    if cfg.dry_run:
        for mtime, sid, path, _ in work:
            print(f"  {dt.datetime.fromtimestamp(mtime):%m-%d %H:%M}  {sid}  {path.parent.name}")
        return 0
    ran = 0
    for _, sid, path, cwd in work:
        if ran >= cfg.max_per_run:
            break
        keep_going, ran_worker = summarize(cfg, sid, path, cwd)
        ran += ran_worker
        if not keep_going:
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
