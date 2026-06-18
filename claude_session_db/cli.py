"""csd — claude-session-db CLI (Postgres archive front-end).

Parses Claude Code session JSONL transcripts into the `claude_sessions` Postgres
archive and provides analytic queries over it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit

import click

from .postgres import SessionArchive, resolve_dsn
from .reconcile import GROW_SLACK_DEFAULT, mark_summarized, reconcile, resolve_kmcp_dsn
from .sweepguard import DEFAULT_MAX_AGE_S, SweepGuard
from .sync import SessionSync

# Launchd StartInterval for the sweep (seconds). The watcher flags a heartbeat
# older than STALE_INTERVALS of these as a stall. Keep in sync with the plist.
SWEEP_INTERVAL_S = 300
STALE_INTERVALS = 3


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


if __name__ == "__main__":
    main()
