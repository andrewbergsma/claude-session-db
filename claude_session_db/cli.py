"""csd — claude-session-db CLI (Postgres archive front-end).

Parses Claude Code session JSONL transcripts into the `claude_sessions` Postgres
archive and provides analytic queries over it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import click

from .postgres import SessionArchive, resolve_dsn
from .reconcile import GROW_SLACK_DEFAULT, mark_summarized, reconcile, resolve_kmcp_dsn
from .sweepguard import DEFAULT_MAX_AGE_S, SweepGuard
from .sync import SessionSync
from . import angles as angles_mod
from . import session_mgmt as mgmt
from . import summarize as ph4
from . import usage as ug

# Launchd StartInterval for the sweep (seconds). The watcher flags a heartbeat
# older than STALE_INTERVALS of these as a stall. Keep in sync with the plist.
SWEEP_INTERVAL_S = 300
STALE_INTERVALS = 3

# Launchd StartInterval for the phase-4 summarize timer. Slower than the sweep:
# each tick can spend minutes of local-LLM time per session. Keep in sync with
# com.claude-session-db.summarize.plist.
SUMMARIZE_INTERVAL_S = 1800
# A summarize run older than this is treated as wedged and its lock reclaimable.
# Generous: limit×LLM-timeout plus slack.
SUMMARIZE_MAX_AGE_S = int(os.environ.get("CSD_SUMMARIZE_MAX_AGE_S", "3300"))


def _load_dotenv() -> None:
    """Best-effort load of a local `.env` (repo root or cwd) into os.environ.

    Stdlib-only, no dependency. Existing environment variables always win, so a
    shell-exported DSN overrides the file. Lines are `KEY=VALUE`; `#` comments and
    blanks are skipped; surrounding quotes on the value are stripped.
    """
    for base in (Path(__file__).resolve().parent.parent, Path.cwd()):
        env_path = base / ".env"
        if not env_path.is_file():
            continue
        try:
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
        except OSError:
            pass


def _redact(dsn: str) -> str:
    """Hide the password in a DSN for display."""
    parts = urlsplit(dsn)
    if parts.password:
        netloc = parts.netloc.replace(f":{parts.password}@", ":***@")
        parts = parts._replace(netloc=netloc)
    return urlunsplit(parts)


@click.group()
@click.option("--dsn", envvar="CSD_DATABASE_URL", default=None,
              help="Postgres DSN (default: derived from $DATABASE_URL → claude_sessions).")
@click.pass_context
def main(ctx: click.Context, dsn: str | None) -> None:
    """Claude Code session archive (Postgres)."""
    _load_dotenv()
    ctx.ensure_object(dict)
    ctx.obj["dsn"] = resolve_dsn(dsn)


@main.command()
@click.option("--rebuild", is_flag=True, help="Drop and rebuild the schema from scratch.")
@click.option("--force", is_flag=True, help="Re-sync all files regardless of mtime.")
@click.option("-q", "--quiet", is_flag=True, help="Suppress per-file progress.")
@click.pass_context
def ingest(ctx: click.Context, rebuild: bool, force: bool, quiet: bool) -> None:
    """Incrementally sync session JSONL into the archive (mtime-based)."""
    sync = SessionSync(dsn=ctx.obj["dsn"], verbose=not quiet)
    stats = sync.sync_all(force=force, rebuild=rebuild)
    click.echo(stats)


@main.command(name="backfill-subagents")
@click.pass_context
def backfill_subagents(ctx: click.Context) -> None:
    """One-shot: materialize child session rows for already-ingested sidechains.

    Historic ingests inserted sidechain MESSAGES under the parent session but
    never created child session rows (sessions.is_subagent was dead). This
    derives one child row per (parent, agent_id) from rows already archived —
    meta.json sidecars where still on disk, else the Agent tool_result join —
    then recomputes aggregates. Idempotent; safe to re-run. Ongoing ingest keeps
    child rows current from here on.
    """
    from .sync import backfill_subagent_sessions
    with SessionArchive(ctx.obj["dsn"]) as a:
        a.initialize()
        res = backfill_subagent_sessions(a, log=click.echo)
    click.echo(f"backfilled {res['children']} child sessions "
               f"({res['meta_hits']} named via meta.json, "
               f"{res['result_hits']} via Agent-result join)")


# Idle threshold (minutes) above which a session is treated as quiesced ("done").
# Validated 2026-06-06: at 10m only 1.03% of genuine intra-session pauses exceed
# it. See claudecode:task/claude-session-db/validate/quiescence-threshold.
QUIESCE_MIN_DEFAULT = 10

# Activity counts replace the old `doing` tldr — the latest tool-result head line
# was almost always noise (raw MCP JSON, git plumbing, system-reminders, even
# leaked secrets). tool_use_count / error_count are precomputed on `sessions` at
# ingest (the recompute-aggregates pass), so this reads them straight off
# v_session_overview — no tool_results scan, no join.
_SWEEP_HEAD_SQL = """
    SELECT o.project_name,
           to_char(o.modified_at, 'HH24:MI') AS at,
           round(extract(epoch FROM (now() - o.modified_at)) / 60)::int AS idle_min,
           o.message_count AS msgs,
           coalesce(o.tool_use_count, 0) AS tool_calls,
           coalesce(o.error_count, 0) AS errors
    FROM v_session_overview o
    WHERE NOT o.is_subagent
      AND o.modified_at > now() - make_interval(mins => %s)
    ORDER BY o.modified_at DESC
"""


@main.command()
@click.option("--window", type=int, default=120,
              help="Minutes: show sessions modified within this window (default 120).")
@click.option("--idle", type=int, default=QUIESCE_MIN_DEFAULT,
              help=f"Minutes idle after which a session is 'quiesced' (default {QUIESCE_MIN_DEFAULT}).")
@click.option("--no-ingest", is_flag=True, help="Skip ingest; observe only.")
@click.option("-q", "--quiet", is_flag=True, help="Suppress per-file ingest progress.")
@click.pass_context
def sweep(ctx: click.Context, window: int, idle: int, no_ingest: bool, quiet: bool) -> None:
    """Ingest fresh sessions, then print a live status head of active sessions.

    Phases 1-3 of the live-session sweep: incremental ingest (which now also
    derives the tldr/error_class siblings at write time), then a read-only
    observability head — one line per recently-active session, labelled live vs
    quiesced and carrying the tldr of its latest tool activity. The phase-4
    roll-up (LLM summaries) is intentionally NOT wired here.

    RELIABILITY: a liveness guard (PID + age) self-aborts if a prior run is still
    live and fresh, and reclaims a stale/wedged lock so a hung predecessor can
    never silently starve every launchd tick. A heartbeat is written on every
    completion (ok/error); `csd sweep-health` (or any mtime watcher) surfaces a
    stalled sweep. See lessons launchd-per-label-hang-silent-starvation and
    csd-sweep-idle-in-transaction-lock-convoy.
    """
    dsn = ctx.obj["dsn"]
    guard = SweepGuard()
    res = guard.acquire()
    if not res.acquired:
        # A live, fresh prior run already holds the lock. Fail FAST and LOUD
        # instead of piling on (launchd would otherwise just defer to it).
        click.echo(f"sweep: {res.reason}", err=True)
        guard.heartbeat(ok=True, detail=f"skipped: {res.reason}")
        return
    if res.reclaimed_stale:
        click.echo(f"sweep: {res.reason}", err=True)

    try:
        _run_sweep(ctx, dsn, window, idle, no_ingest, quiet)
    except Exception as exc:  # noqa: BLE001 — surface ANY failure as a signal
        guard.heartbeat(ok=False, detail=f"{type(exc).__name__}: {exc}")
        click.echo(f"sweep: FAILED — {type(exc).__name__}: {exc}", err=True)
        guard.release()
        raise SystemExit(1)
    else:
        guard.heartbeat(ok=True)
    finally:
        guard.release()


def _run_sweep(ctx: click.Context, dsn: str, window: int, idle: int,
               no_ingest: bool, quiet: bool) -> None:
    """The actual sweep body, wrapped by the liveness guard in `sweep()`."""
    if not no_ingest:
        # verbose=False: suppress the "Found N files" preamble + per-file lines;
        # the one-line summary below carries the only signal worth keeping.
        sync = SessionSync(dsn=dsn, verbose=False)
        stats = sync.sync_all()
        if not quiet:
            click.echo(stats.oneline())
    with SessionArchive(dsn) as a:
        rows = a.query(_SWEEP_HEAD_SQL, (window,))
    if not rows:
        click.echo(f"No sessions active in the last {window} min.")
        return

    def render(r) -> str:
        idle_min = int(r["idle_min"] or 0)
        state = "·done" if idle_min >= idle else "live "
        errors = int(r["errors"] or 0)
        act = f"{int(r['tool_calls'] or 0):>3} tools"
        if errors:
            act += f", {errors} err"
        return (f"  {state} {r['at']} {idle_min:>4}m  "
                f"{str(r['project_name'] or ''):<18.18} {r['msgs'] or 0:>4}msg  {act}")

    live = [r for r in rows if int(r["idle_min"] or 0) < idle]
    done = [r for r in rows if int(r["idle_min"] or 0) >= idle]
    # Foreground live sessions; collapse the quiesced flood to a count. If nothing
    # is live, still show the few most-recent done rows so the head isn't empty.
    shown_done = [] if live else done[:5]
    click.echo(f"\nActive sessions (last {window} min · idle>{idle}m = quiesced):")
    for r in live + shown_done:
        click.echo(render(r))
    remaining = len(done) - len(shown_done)
    if remaining > 0:
        click.echo(f"  … +{remaining} quiesced (run `csd recent` to list)")


@main.command(name="sweep-health")
@click.option("--stale-intervals", type=int, default=STALE_INTERVALS,
              help=f"Flag a stall if the heartbeat is older than this many "
                   f"{SWEEP_INTERVAL_S}s intervals (default {STALE_INTERVALS}).")
@click.pass_context
def sweep_health(ctx: click.Context, stale_intervals: int) -> None:
    """Report sweep liveness: heartbeat age, last outcome, and any held lock.

    The cheap external watcher for the launchd timer — DB-free, so it still works
    when the archive itself is wedged. Exit 0 = healthy, 1 = STALE or last run
    errored, 2 = no heartbeat yet. Wire into a monitor (or eyeball it) instead of
    discovering a hang hours later by hand.
    """
    guard = SweepGuard()
    threshold = stale_intervals * SWEEP_INTERVAL_S
    age, hb = guard.staleness()

    # Held-lock report (a long-held lock is itself a hang signal).
    lock = guard._read_lock()
    if lock:
        import time as _t
        lpid = lock.get("pid")
        lage = _t.time() - float(lock.get("started_at", 0) or 0)
        click.echo(f"lock: held by pid={lpid}, age={lage:.0f}s "
                   f"(max {guard.max_age_s}s before reclaimable)")
    else:
        click.echo("lock: free")

    if age is None:
        click.echo("heartbeat: NONE — sweep has never recorded a completion")
        raise SystemExit(2)

    when = ""
    if hb and hb.get("ok") is False:
        when = f" — last run ERRORED: {hb.get('detail', '')[:200]}"
    status = "STALE" if age > threshold else "ok"
    click.echo(f"heartbeat: {status} (age={age:.0f}s, threshold={threshold}s, "
               f"last_ok={hb.get('ok')}){when}")
    if status == "STALE" or (hb and hb.get("ok") is False):
        raise SystemExit(1)


@main.command()
@click.option("--exact", is_flag=True,
              help="Exact count(*) per table (full scans, slower); default uses fast catalog estimates.")
@click.pass_context
def stats(ctx: click.Context, exact: bool) -> None:
    """Show table row counts and database size."""
    with SessionArchive(ctx.obj["dsn"]) as a:
        s = a.statistics(exact=exact)
    width = max(len(k) for k in s)
    for k, v in s.items():
        click.echo(f"  {k:<{width}}  {v:>14,}" if isinstance(v, int) else f"  {k:<{width}}  {v:>14}")
    if not exact:
        click.echo("  (row counts are catalog estimates; pass --exact for precise counts)")


@main.command()
@click.argument("n", type=int, default=20)
@click.pass_context
def recent(ctx: click.Context, n: int) -> None:
    """Show the N most recently modified sessions."""
    sql = """
        SELECT to_char(modified_at, 'YYYY-MM-DD HH24:MI') AS modified,
               project_name, message_count AS msgs, total_output_tokens AS out_tok,
               left(coalesce(title, first_prompt, ''), 60) AS title
        FROM v_session_overview
        WHERE NOT is_subagent
        ORDER BY modified_at DESC NULLS LAST
        LIMIT %s
    """
    with SessionArchive(ctx.obj["dsn"]) as a:
        rows = a.query(sql, (n,))
    for r in rows:
        click.echo(f"{r['modified']}  {str(r['project_name'] or ''):<22.22} "
                   f"{r['msgs'] or 0:>4} msg {r['out_tok'] or 0:>8,}t  {r['title']}")


@main.command()
@click.argument("sql")
@click.option("--csv", "as_csv", is_flag=True, help="Output as CSV.")
@click.pass_context
def query(ctx: click.Context, sql: str, as_csv: bool) -> None:
    """Run an ad-hoc SQL query against the archive."""
    with SessionArchive(ctx.obj["dsn"]) as a:
        rows = a.query(sql)
    if not rows:
        click.echo("(no rows)")
        return
    cols = list(rows[0].keys())
    if as_csv:
        import csv
        w = csv.DictWriter(sys.stdout, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    else:
        click.echo(" | ".join(cols))
        for r in rows:
            click.echo(" | ".join(str(r[c]) for c in cols))


@main.command()
@click.pass_context
def views(ctx: click.Context) -> None:
    """List available analytic views."""
    sql = """SELECT viewname FROM pg_views WHERE schemaname='public'
             AND left(viewname, 2) = 'v_' ORDER BY viewname"""
    with SessionArchive(ctx.obj["dsn"]) as a:
        rows = a.query(sql)
    for r in rows:
        click.echo(r["viewname"])


@main.command(name="reconcile-summaries")
@click.option("--kmcp-dsn", envvar="KMCP_DATABASE_URL", default=None,
              help="Knowledge DB DSN (default: archive DSN with dbname swapped to 'knowledge').")
@click.option("--grow-slack", type=int, default=GROW_SLACK_DEFAULT,
              help=f"Messages a summarized session may grow before flipping back "
                   f"to pending (default {GROW_SLACK_DEFAULT} — absorbs the tail "
                   f"of a self-run /session-summary).")
@click.pass_context
def reconcile_summaries(ctx: click.Context, kmcp_dsn: str | None, grow_slack: int) -> None:
    """Classify every archived session as summarized / not_required / pending.

    The pre-LLM gate for phase-4 roll-ups: cross-checks kmcp `session` entries
    by session_id (truth from the ledger, not the summarizer's report), then
    applies the empty / meta_run / trivial heuristics. Idempotent; re-run any
    time. `csd unsummarized` serves the pending residue to the sweep.
    """
    import psycopg
    dsn = ctx.obj["dsn"]
    with SessionArchive(dsn) as a:
        try:
            if a.ensure_gate_objects():
                click.echo("schema self-heal: created gate objects")
        except psycopg.errors.LockNotAvailable:
            a.connect().rollback()
            raise click.ClickException(
                "schema self-heal is blocked on a lock held by a concurrent "
                "session — retry in a moment, or run `csd ingest` once to settle "
                "the schema first."
            )
        stats = reconcile(a.connect(), resolve_kmcp_dsn(dsn, kmcp_dsn),
                          grow_slack, log=click.echo)
    click.echo(stats.summary())


@main.command()
@click.option("-n", "--limit", type=int, default=50, help="Max rows (default 50).")
@click.pass_context
def unsummarized(ctx: click.Context, limit: int) -> None:
    """List pending sessions — the phase-4 work queue (newest first).

    Replaces the recent-by-mtime walk, which was ~80% already-summarized.
    Run `csd reconcile-summaries` first to refresh the classification.
    """
    sql = """
        SELECT session_id, to_char(modified_at, 'YYYY-MM-DD HH24:MI') AS modified,
               project_name, message_count AS msgs, tool_use_count AS tools,
               coalesce(reason, '') AS reason,
               left(coalesce(title, first_prompt, ''), 60) AS title
        FROM v_unsummarized LIMIT %s
    """
    with SessionArchive(ctx.obj["dsn"]) as a:
        rows = a.query(sql, (limit,))
    if not rows:
        click.echo("No pending sessions — the queue is drained.")
        return
    for r in rows:
        flag = f" [{r['reason']}]" if r["reason"] else ""
        click.echo(f"{r['session_id']}  {r['modified']}  "
                   f"{str(r['project_name'] or ''):<18.18} {r['msgs']:>4}msg "
                   f"{r['tools']:>4}tool{flag}  {r['title']}")


def _summarize_guard() -> SweepGuard:
    return SweepGuard(max_age_s=SUMMARIZE_MAX_AGE_S,
                      lock_name="summarize.lock",
                      heartbeat_name="summarize.heartbeat")


@main.command()
@click.option("-n", "--limit", type=int, default=ph4.DEFAULT_LIMIT,
              help=f"Max sessions to roll up this run (default {ph4.DEFAULT_LIMIT}).")
@click.option("--min-idle", type=int, default=ph4.DEFAULT_MIN_IDLE_S,
              help=f"Seconds a session must be quiescent before roll-up "
                   f"(default {ph4.DEFAULT_MIN_IDLE_S}).")
@click.option("--model", default=ph4.DEFAULT_MODEL,
              help=f"Ollama model (default {ph4.DEFAULT_MODEL}; env CSD_SUMMARIZE_MODEL).")
@click.option("--ollama-url", default=ph4.DEFAULT_OLLAMA_URL,
              help="Ollama endpoint (env CSD_OLLAMA_URL).")
@click.option("--session", "only_session", default=None,
              help="Roll up only this session_id (must be in the pending queue).")
@click.option("--kmcp-dsn", default=None,
              help="Knowledge DB DSN (default: archive DSN with db=knowledge).")
@click.option("--dry-run", is_flag=True, help="List what would be summarized; no LLM, no writes.")
@click.pass_context
def summarize(ctx: click.Context, limit: int, min_idle: int, model: str,
              ollama_url: str, only_session: str | None, kmcp_dsn: str | None,
              dry_run: bool) -> None:
    """Phase-4 roll-up: digest → local LLM → kmcp session entry (unattended).

    Drains the reconcile gate's PENDING queue through the canonical off-session
    path (session_digest → Ollama JSON mode → verified kmcp write →
    mark-summarized watermark). Never resumes a session, never replays a raw
    transcript. Run `csd reconcile-summaries` first for a fresh queue; a small
    default limit lets the launchd timer drain the backlog gradually.
    """
    dsn = ctx.obj["dsn"]
    guard = _summarize_guard()
    res = guard.acquire()
    if not res.acquired:
        click.echo(f"summarize: {res.reason}", err=True)
        guard.heartbeat(ok=True, detail=f"skipped: {res.reason}")
        return
    if res.reclaimed_stale:
        click.echo(f"summarize: {res.reason}", err=True)
    try:
        with SessionArchive(dsn) as a:
            stats = ph4.run_summarize(
                a.connect(), dsn, limit=limit, min_idle_s=min_idle, model=model,
                ollama_url=ollama_url, only_session=only_session,
                dry_run=dry_run, kmcp_dsn=kmcp_dsn, log=click.echo)
    except Exception as exc:  # noqa: BLE001 — surface ANY failure as a signal
        guard.heartbeat(ok=False, detail=f"{type(exc).__name__}: {exc}")
        click.echo(f"summarize: FAILED — {type(exc).__name__}: {exc}", err=True)
        guard.release()
        raise SystemExit(1)
    else:
        # Per-session failures are contained (recorded in summarize_attempts);
        # the heartbeat only goes not-ok when the RUN itself broke.
        guard.heartbeat(ok=True, detail=stats.summary().splitlines()[0])
    finally:
        guard.release()
    click.echo(stats.summary())


@main.command(name="summarize-health")
@click.option("--stale-intervals", type=int, default=STALE_INTERVALS,
              help=f"Flag a stall if the heartbeat is older than this many "
                   f"{SUMMARIZE_INTERVAL_S}s intervals (default {STALE_INTERVALS}).")
@click.pass_context
def summarize_health(ctx: click.Context, stale_intervals: int) -> None:
    """Report phase-4 summarize liveness (DB-free; mirrors sweep-health).

    Exit 0 = healthy, 1 = STALE or last run errored, 2 = no heartbeat yet.
    """
    guard = _summarize_guard()
    threshold = stale_intervals * SUMMARIZE_INTERVAL_S
    age, hb = guard.staleness()

    lock = guard._read_lock()
    if lock:
        import time as _t
        lage = _t.time() - float(lock.get("started_at", 0) or 0)
        click.echo(f"lock: held by pid={lock.get('pid')}, age={lage:.0f}s "
                   f"(max {guard.max_age_s}s before reclaimable)")
    else:
        click.echo("lock: free")

    if age is None:
        click.echo("heartbeat: NONE — summarize has never recorded a completion")
        raise SystemExit(2)

    when = ""
    if hb and hb.get("ok") is False:
        when = f" — last run ERRORED: {hb.get('detail', '')[:200]}"
    status = "STALE" if age > threshold else "ok"
    click.echo(f"heartbeat: {status} (age={age:.0f}s, threshold={threshold}s, "
               f"last_ok={hb.get('ok')}){when}")
    if status == "STALE" or (hb and hb.get("ok") is False):
        raise SystemExit(1)


@main.command(name="mark-summarized")
@click.argument("session_id")
@click.option("--app", "application", required=True, help="kmcp application of the summary entry.")
@click.option("--path", required=True, help="kmcp path of the summary entry.")
@click.pass_context
def mark_summarized_cmd(ctx: click.Context, session_id: str, application: str, path: str) -> None:
    """Stamp a session summarized at its current message-count watermark.

    Call after a VERIFIED kmcp write (the entry row exists). kmcp session
    entries store neither message_count nor leaf uuid, so csd stamps the
    re-eval watermark itself at summarize time.
    """
    with SessionArchive(ctx.obj["dsn"]) as a:
        a.initialize()
        row = mark_summarized(a.connect(), session_id, application, path)
    click.echo(f"summarized  {row['session_id']}  watermark={row['message_count_at_summary']}msg  "
               f"-> {row['kmcp_application']}:{row['kmcp_path']}")


def _fmt_idle(s: int | None) -> str:
    if s is None:
        return "?"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600}h"


def _fmt_agents(r: dict) -> str:
    n = r.get("agents_total") or 0
    if not n:
        return ""
    out = str(n)
    if r.get("agents_running"):
        out += f"·{r['agents_running']}run"
    if r.get("agents_failed"):
        out += f"·{r['agents_failed']}fail"
    return out


def _fmt_delta(d: dict | None) -> str:
    if d is None:
        return ""
    k = d["class"]
    if k == "none":
        return "none"
    if k == "unknown":
        return f"unknown ({d.get('note', '')})"
    body = f"{d['records']}rec {d['prompts']}p {d['tool_calls']}t"
    if k == "real":
        sig = d["signals"][0] if d["signals"] else ""
        return f"REAL {body}  {sig}"
    return {"confirmation_only": "confirm-only",
            "auto_compaction_only": "compaction-only"}.get(k, k) + f" {body}"


def _angles_sessions(ctx: click.Context, kmcp_dsn: str | None, window_days: int,
                     live_min: int, with_delta: bool, as_json: bool) -> None:
    """The session-management lens: open-thread inventory + delta verdicts."""
    try:
        resolved_kmcp = resolve_kmcp_dsn(ctx.obj["dsn"], kmcp_dsn)
    except Exception:
        resolved_kmcp = None  # watermark kmcp-fallback degrades to unknown
    rows = mgmt.inventory(ctx.obj["dsn"], resolved_kmcp,
                          window_days=window_days, live_min=live_min,
                          with_delta=with_delta)
    if as_json:
        import json as _json
        click.echo(_json.dumps(rows, indent=2, default=str))
        return
    if not rows:
        click.echo(f"No main sessions active in the last {window_days}d.")
        return
    hdr = (f"{'VERDICT':<10} {'SESSION':<8} {'PROJECT':<20.20} {'BRANCH':<22.22} "
           f"{'LAST-ACT':<12} {'IDLE':>6} {'MSGS':>5} {'AGENTS':>8}  "
           f"{'SUMMARY':<12} DELTA")
    click.echo(hdr)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        state = r["state"] or "—"
        if r["reason"]:
            state += f"/{r['reason']}"
        last = (r["last_ts"].astimezone().strftime("%m-%d %H:%M")
                if r["last_ts"] else "?")
        color = {"LIVE": "green", "OPEN-delta": "red",
                 "OPEN": "yellow", "OPEN?": "yellow"}.get(r["verdict"])
        verdict = click.style(f"{r['verdict']:<10}", fg=color) if color \
            else f"{r['verdict']:<10}"
        click.echo(f"{verdict} {r['session_id'][:8]:<8} "
                   f"{str(r['project_name'] or ''):<20.20} "
                   f"{str(r['git_branch'] or '—'):<22.22} {last:<12} "
                   f"{_fmt_idle(r['idle_s']):>6} {r['message_count'] or 0:>5} "
                   f"{_fmt_agents(r):>8}  "
                   f"{state:<12} {_fmt_delta(r['delta'])}")
    tally = " · ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    click.echo(f"\n{len(rows)} sessions ({tally})  ·  "
               "delta detail: csd angles digest <id> --delta")


@main.command()
@click.argument("spec", nargs=-1)
@click.option("--session", "session_id", default=None,
              help="Target session UUID (default: newest transcript for the cwd).")
@click.option("--turn", type=int, default=-1,
              help="Which turn: -1 = latest user prompt (default), -2 = prior, ...")
@click.option("--model", default=angles_mod.DEFAULT_MODEL,
              help=f"Probe model (default {angles_mod.DEFAULT_MODEL}; env CSD_ANGLES_MODEL).")
@click.option("--ollama-url", default=angles_mod.DEFAULT_OLLAMA_URL,
              help="Ollama endpoint (env CSD_OLLAMA_URL).")
@click.option("--kmcp-dsn", default=None,
              help="Knowledge DB DSN for the knowledge angle / delta watermark "
                   "(default: archive DSN with db=knowledge).")
@click.option("--no-probes", is_flag=True,
              help="Deterministic angles only — skip LLM probes and retrieval.")
@click.option("--window-days", type=int, default=mgmt.WINDOW_DAYS_DEFAULT,
              help=f"(sessions) Inventory window in days; 0 = all "
                   f"(default {mgmt.WINDOW_DAYS_DEFAULT}).")
@click.option("--live-min", type=int, default=mgmt.LIVE_MIN_DEFAULT,
              help=f"(sessions) Minutes since last message under which a session "
                   f"is LIVE (default {mgmt.LIVE_MIN_DEFAULT}).")
@click.option("--no-delta", is_flag=True,
              help="(sessions) Skip delta-after-summary detection (no transcript reads).")
@click.option("--json", "as_json", is_flag=True,
              help="(sessions) Emit the raw inventory as JSON.")
@click.option("--delta", "delta_mode", is_flag=True,
              help="(digest) Render only the post-summary-watermark tail.")
@click.option("--head", type=int, default=None,
              help=f"(digest) First N records to keep (default {mgmt.DIGEST_HEAD_DEFAULT} "
                   "when neither --full nor --delta).")
@click.option("--tail", type=int, default=None,
              help=f"(digest) Last N records to keep (default {mgmt.DIGEST_TAIL_DEFAULT}).")
@click.option("--full", "full_digest", is_flag=True,
              help="(digest) No head/tail window — the whole transcript (can be huge).")
@click.option("--result-head", type=int, default=200,
              help="(digest) Chars of each tool_result to keep (default 200).")
@click.option("--full-inputs", is_flag=True,
              help="(digest) Verbatim tool_use inputs instead of one-field hints.")
@click.pass_context
def angles(ctx: click.Context, spec: tuple[str, ...], session_id: str | None,
           turn: int, model: str, ollama_url: str, kmcp_dsn: str | None,
           no_probes: bool, window_days: int, live_min: int, no_delta: bool,
           as_json: bool, delta_mode: bool, head: int | None, tail: int | None,
           full_digest: bool, result_head: int, full_inputs: bool) -> None:
    """Pull-based turn mining: one-line ID-addressable headlines for one turn.

    Fire right after an agent response lands (e.g. `! csd angles` inside a
    Claude Code session). SPEC is either an angle subset (`csd angles
    files,errors,knowledge`) or one of the keyword forms:

    \b
      csd angles show ID          detail behind a mined headline
      csd angles sessions         session-management lens: open-thread
                                  inventory + delta-after-summary verdicts
      csd angles digest REF       per-session digest (REF = session id or
                                  unique prefix; --delta for the post-summary
                                  tail, --head/--tail/--full for windowing)

    No SPEC runs every angle on the latest turn. Reads transcripts / archive
    read-only; nothing is written to kmcp — curation happens in the
    operator's next message.

    Design: claudecode:design/turn-angles-context-cockpit
    """
    if spec and spec[0] == "show":
        if len(spec) < 2:
            click.echo("usage: csd angles show ID", err=True)
            sys.exit(2)
        click.echo(angles_mod.show_item(spec[1]))
        return
    if spec and spec[0] == "sessions":
        _angles_sessions(ctx, kmcp_dsn, window_days, live_min,
                         with_delta=not no_delta, as_json=as_json)
        return
    if spec and spec[0] == "digest":
        if len(spec) < 2:
            click.echo("usage: csd angles digest SESSION_REF "
                       "[--delta|--head N|--tail N|--full]", err=True)
            sys.exit(2)
        try:
            resolved_kmcp = resolve_kmcp_dsn(ctx.obj["dsn"], kmcp_dsn)
        except Exception:
            resolved_kmcp = None
        try:
            click.echo(mgmt.digest_for(
                spec[1], dsn=ctx.obj["dsn"], kmcp_dsn=resolved_kmcp,
                delta=delta_mode, head=head, tail=tail, full=full_digest,
                result_head=result_head, full_inputs=full_inputs), nl=False)
        except ValueError as exc:
            click.echo(f"digest: {exc}", err=True)
            sys.exit(1)
        return
    wanted = None
    if spec:
        wanted = [a.strip() for chunk in spec for a in chunk.split(",") if a.strip()]
        unknown = [a for a in wanted if a not in angles_mod.ANGLE_SPECS]
        if unknown:
            click.echo(f"unknown angle(s): {', '.join(unknown)} "
                       f"(have: {', '.join(angles_mod.ANGLE_SPECS)})", err=True)
            sys.exit(2)
    try:
        resolved_kmcp = resolve_kmcp_dsn(ctx.obj["dsn"], kmcp_dsn)
    except Exception:
        resolved_kmcp = None  # knowledge angle degrades to unavailable
    try:
        click.echo(angles_mod.run_angles(
            cwd=os.getcwd(), angles=wanted, session_id=session_id, turn=turn,
            model=model, base_url=ollama_url, kmcp_dsn=resolved_kmcp,
            no_probes=no_probes))
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"angles: {exc}", err=True)
        sys.exit(1)


@main.command(name="angles-watch")
@click.option("--window", type=int, default=1800,
              help="Transcript mtime window in seconds to count a session live (default 1800).")
@click.option("--model", default=angles_mod.DEFAULT_MODEL,
              help=f"Probe model (default {angles_mod.DEFAULT_MODEL}; env CSD_ANGLES_MODEL).")
@click.option("--ollama-url", default=angles_mod.DEFAULT_OLLAMA_URL,
              help="Ollama endpoint (env CSD_OLLAMA_URL).")
@click.option("--kmcp-dsn", default=None,
              help="Knowledge DB DSN for the knowledge angle (default: archive DSN with db=knowledge).")
@click.option("--no-probes", is_flag=True,
              help="Deterministic angles only — skip LLM probes and retrieval.")
@click.pass_context
def angles_watch(ctx: click.Context, window: int, model: str, ollama_url: str,
                 kmcp_dsn: str | None, no_probes: bool) -> None:
    """Headless miner: keep the angles state dir warm for every live session.

    Watches every live transcript under ~/.claude/projects and re-mines a
    session's latest turn whenever its JSONL settles (~8s debounce), writing
    headlines + detail to the angles state dir. Mining runs through a
    single-worker queue so concurrent sessions never stampede Ollama.

    Serves nothing. Readers pick the results up off disk: `csd angles show ID`,
    or the session console's angle rail.

    Design: claudecode:design/turn-angles-context-cockpit (ambient surface).
    """
    from .angles_watch import run_watch
    try:
        resolved_kmcp = resolve_kmcp_dsn(ctx.obj["dsn"], kmcp_dsn)
    except Exception:
        resolved_kmcp = None
    try:
        run_watch(window_s=window, model=model, base_url=ollama_url,
                  kmcp_dsn=resolved_kmcp, no_probes=no_probes)
    except KeyboardInterrupt:
        click.echo("angles-watch: stopped", err=True)


@main.command(name="angles-serve")
@click.option("--host", default="0.0.0.0", help="Bind address (default 0.0.0.0 — LAN).")
@click.option("--port", type=int, default=8791, help="Port (default 8791).")
@click.option("--window", type=int, default=1800,
              help="Transcript mtime window in seconds to count a session live (default 1800).")
@click.option("--model", default=angles_mod.DEFAULT_MODEL,
              help=f"Probe model (default {angles_mod.DEFAULT_MODEL}; env CSD_ANGLES_MODEL).")
@click.option("--ollama-url", default=angles_mod.DEFAULT_OLLAMA_URL,
              help="Ollama endpoint (env CSD_OLLAMA_URL).")
@click.option("--kmcp-dsn", default=None,
              help="Knowledge DB DSN for the knowledge angle (default: archive DSN with db=knowledge).")
@click.option("--no-probes", is_flag=True,
              help="Deterministic angles only — skip LLM probes and retrieval.")
@click.pass_context
def angles_serve(ctx: click.Context, host: str, port: int, window: int,
                 model: str, ollama_url: str, kmcp_dsn: str | None,
                 no_probes: bool) -> None:
    """Ambient multi-session angles dashboard (LAN, no auth — trusted network only).

    Watches every live transcript under ~/.claude/projects, re-mines a
    session's latest turn whenever its JSONL settles (~8s debounce), and
    serves one row per session: direction, files, errors, kmcp writes, token
    burn — each headline's detail one click away. Probes run through a
    single-worker queue so concurrent sessions never stampede Ollama.

    Design: claudecode:design/turn-angles-context-cockpit (ambient surface).
    """
    from .angles_web import serve
    try:
        resolved_kmcp = resolve_kmcp_dsn(ctx.obj["dsn"], kmcp_dsn)
    except Exception:
        resolved_kmcp = None
    serve(host=host, port=port, window_s=window, model=model,
          base_url=ollama_url, kmcp_dsn=resolved_kmcp, no_probes=no_probes,
          csd_dsn=ctx.obj["dsn"])


@main.command(name="console")
@click.option("--host", default="127.0.0.1",
              help="Bind address (default 127.0.0.1; use 0.0.0.0 for LAN).")
@click.option("--port", type=int, default=4462, help="Port (default 4462).")
@click.option("--token", default=None,
              help="Shared secret for non-loopback binds (env CSD_CONSOLE_TOKEN; "
                   "generated if unset).")
@click.option("--no-auth", is_flag=True,
              help="Serve a non-loopback bind with NO auth. This exposes "
                   "unauthenticated code execution — see the command help.")
@click.option("--kmcp-dsn", default=None,
              help="Knowledge DB DSN for angle curation writes "
                   "(default: archive DSN with db=knowledge).")
@click.pass_context
def console(ctx: click.Context, host: str, port: int, token: str | None,
            no_auth: bool, kmcp_dsn: str | None) -> None:
    """Reply-capable session console: chat + kmcp reads + angle rail.

    Renders each session's transcript as a chronological event stream with the
    kmcp context it loaded inline, plus the latest turn's angle headlines read
    off the state dir (run `csd angles-watch` to keep them fresh). Answer
    resumes the session; Fork branches it at a chosen message.

    SECURITY. This is not a read-only surface: `/api/answer` and `/api/fork`
    spawn `claude -p --resume` with caller-supplied text in a caller-supplied
    cwd, and the transcripts it serves are verbatim. On 127.0.0.1 that is fine.
    On any other bind a token is REQUIRED (auto-generated and printed at start;
    pin it with CSD_CONSOLE_TOKEN) — because without one, anyone who can reach
    the port can run code as you. `--no-auth` opts out; it is never the default,
    and the port should still never leave a trusted LAN.

    Design: claudecode:design/turn-angles-context-cockpit (conversation surface).
    """
    from .console.server import serve

    if no_auth and host != "127.0.0.1":
        click.confirm(
            f"--no-auth on {host}:{port} exposes unauthenticated code execution "
            "to every host on the network. Proceed?", abort=True)
    try:
        resolved_kmcp = resolve_kmcp_dsn(ctx.obj["dsn"], kmcp_dsn)
    except Exception:
        resolved_kmcp = None
    serve(host=host, port=port, token=token, no_auth=no_auth,
          kmcp_dsn=resolved_kmcp)


@main.command(name="dsn")
@click.pass_context
def show_dsn(ctx: click.Context) -> None:
    """Print the resolved connection target (password redacted)."""
    click.echo(_redact(ctx.obj["dsn"]))


@main.command(name="open")
@click.pass_context
def open_psql(ctx: click.Context) -> None:
    """Open an interactive shell on the archive (pgcli if available, else psql)."""
    dsn = ctx.obj["dsn"]
    tool = shutil.which("pgcli") or shutil.which("psql")
    if not tool:
        click.echo("Neither pgcli nor psql found on PATH.", err=True)
        sys.exit(1)
    os.execvp(tool, [tool, dsn])


def _render_usage(rows: list[dict], cost: dict | None) -> None:
    """Human-readable dual-account quota report."""
    for row in rows:
        tag = "  ● active" if row.get("active") else ""
        name = row.get("label") or row.get("email") or "account"
        plan = ug.TIER_LABELS.get(row.get("tier") or "", row.get("tier") or "?")
        click.echo(f"\n{click.style(name, bold=True)}  ({row.get('email') or '?'} · {plan}){tag}")
        if row.get("error"):
            click.echo(f"    {click.style('✗ ' + row['error'], fg='red')}")
            continue
        u = row.get("usage") or {}

        def line(caption: str, entry: dict | None) -> None:
            if not entry:
                click.echo(f"    {caption:<8} (n/a)")
                return
            pct = entry.get("percent") or 0
            mark = ug._SEVERITY_MARK.get(entry.get("severity", "normal"), "")
            fg = {"warning": "yellow", "critical": "red", "exceeded": "red"}.get(
                entry.get("severity"), None)
            bar = click.style(ug._bar(pct), fg=fg) if fg else ug._bar(pct)
            click.echo(f"    {caption:<8} {bar} {pct:>3.0f}%{mark}   resets {ug._fmt_reset(entry.get('resets_at'))}")

        line("5-hour", u.get("five_hour"))
        line("weekly", u.get("weekly"))
        for sc in u.get("scoped") or []:
            if (sc.get("percent") or 0) > 0:
                line(sc.get("name", "scoped")[:8], sc)
    if cost is not None:
        click.echo(f"\n{click.style('local cost', dim=True)} (all accounts, commingled): "
                   f"${cost['week']:.2f} last 7d · ${cost['today']:.2f} today")
    click.echo()


@main.group(invoke_without_command=True)
@click.option("--json", "as_json", is_flag=True, help="Emit the raw report as JSON.")
@click.option("--no-cost", is_flag=True, help="Skip the local Postgres cost aggregate.")
@click.pass_context
def usage(ctx: click.Context, as_json: bool, no_cost: bool) -> None:
    """Report Claude Max quota across all vaulted accounts (default action).

    Bare `csd usage` polls every vaulted account's live quota. Use the
    subcommands to manage the vault and switch the active account.
    """
    if ctx.invoked_subcommand is not None:
        return
    vault = ug.load_vault()
    rows: list[dict] = []
    seen_emails: set[str] = set()

    # 1) Active account via the LIVE (authoritative, current) refresh token.
    live = ug.read_live_oauth()
    if live and live.get("refreshToken"):
        row = ug.poll(live["refreshToken"], vault, is_active=True,
                      known_tier=live.get("rateLimitTier") or None)
        rows.append(row)
        if row.get("email"):
            seen_emails.add(row["email"])

    # 2) Remaining vaulted (inactive) accounts.
    for acct in vault["accounts"]:
        if acct.get("email") and acct["email"] in seen_emails:
            continue
        if not acct.get("refresh_token"):
            continue
        rows.append(ug.poll(acct["refresh_token"], vault, is_active=False,
                            label=acct.get("label"), known_tier=acct.get("rate_limit_tier")))
        if acct.get("email"):
            seen_emails.add(acct["email"])

    ug.save_vault(vault)  # persist rotated refresh tokens

    if not rows:
        click.echo("No accounts to report. Log into an account and run "
                   "`csd usage add-account` to vault it.", err=True)
        return

    cost = None if no_cost else ug.local_aggregate_cost(ctx.obj["dsn"])
    if as_json:
        import json as _json
        click.echo(_json.dumps({"accounts": rows, "local_cost": cost}, indent=2, default=str))
    else:
        _render_usage(rows, cost)


@usage.command("add-account")
@click.argument("label", required=False)
def usage_add_account(label: str | None) -> None:
    """Vault the currently logged-in account (run once per account).

    Reads the live keychain credential, refreshes it to confirm validity and learn
    the account identity, and stores its (rotating) refresh token in the 0600
    vault. LABEL defaults to the account's email local-part.
    """
    live = ug.read_live_oauth()
    if not live or not live.get("refreshToken"):
        click.echo("No live Claude credentials found (keychain/`~/.claude/.credentials.json`). "
                   "Log in with Claude Code first.", err=True)
        sys.exit(1)
    vault = ug.load_vault()
    try:
        tok = ug.refresh(live["refreshToken"])
    except ug.UsageError as e:
        click.echo(f"Could not validate the live account: {e}", err=True)
        sys.exit(1)
    tier = live.get("rateLimitTier") or None
    if not tier:
        try:
            tier = ((ug.fetch_profile(tok["access_token"]).get("organization") or {})
                    .get("rate_limit_tier"))
        except ug.UsageError:
            tier = None
    acct = ug.upsert_from_refresh(vault, tok, label=label, tier=tier)
    # We just rotated the live refresh token — write it back so the live login survives.
    ug.write_live_oauth(ug._oauth_from_token_response(tok, live))
    ug.save_vault(vault)
    click.echo(f"Vaulted '{acct['label']}' ({acct.get('email')} · "
               f"{ug.TIER_LABELS.get(tier or '', tier or '?')}). "
               f"{len(vault['accounts'])} account(s) now tracked.")


@usage.command("use")
@click.argument("label")
def usage_use(label: str) -> None:
    """Switch the active account to LABEL (replaces the interactive /login swap).

    Refreshes the vaulted account and writes its fresh creds into the live
    keychain + mirror file, so the next Claude Code session runs as that account.
    """
    vault = ug.load_vault()
    acct = ug._find_account(vault, label)
    if not acct:
        names = ", ".join(a.get("label", "?") for a in vault["accounts"]) or "(none)"
        click.echo(f"No vaulted account '{label}'. Known: {names}", err=True)
        sys.exit(1)
    try:
        tok = ug.refresh(acct["refresh_token"])
    except ug.UsageError as e:
        click.echo(f"Refresh failed for '{label}': {e}", err=True)
        sys.exit(1)
    ug.upsert_from_refresh(vault, tok, label=acct.get("label"),
                           tier=acct.get("rate_limit_tier"))
    changed = ug.write_live_oauth(ug._oauth_from_token_response(tok, {
        "rateLimitTier": acct.get("rate_limit_tier", ""),
    }))
    ug.save_vault(vault)
    click.echo(f"Active account is now '{acct['label']}' ({acct.get('email')}). "
               f"Wrote: {', '.join(changed) or 'nothing'}. Restart Claude Code to pick it up.")


@usage.command("list")
def usage_list() -> None:
    """List vaulted accounts (no network)."""
    vault = ug.load_vault()
    if not vault["accounts"]:
        click.echo("(no accounts vaulted — run `csd usage add-account`)")
        return
    for a in vault["accounts"]:
        tier = ug.TIER_LABELS.get(a.get("rate_limit_tier") or "", a.get("rate_limit_tier") or "?")
        click.echo(f"  {a.get('label'):<16} {a.get('email') or '?':<28} {tier}")


@usage.command("remove")
@click.argument("label")
def usage_remove(label: str) -> None:
    """Forget a vaulted account (does not touch the live login)."""
    vault = ug.load_vault()
    acct = ug._find_account(vault, label)
    if not acct:
        click.echo(f"No vaulted account '{label}'.", err=True)
        sys.exit(1)
    vault["accounts"] = [a for a in vault["accounts"] if a is not acct]
    ug.save_vault(vault)
    click.echo(f"Removed '{label}'. {len(vault['accounts'])} account(s) remain.")


if __name__ == "__main__":
    main()
