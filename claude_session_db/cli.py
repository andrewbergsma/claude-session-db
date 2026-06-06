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
from .sync import SessionSync


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

_SWEEP_HEAD_SQL = """
    WITH latest AS (
        SELECT DISTINCT ON (tr.session_id)
               tr.session_id, tr.tldr, tr.is_error
        FROM tool_results tr
        JOIN messages m ON m.uuid = tr.message_uuid
        WHERE NOT m.is_sidechain
        ORDER BY tr.session_id, m.ts DESC NULLS LAST
    )
    SELECT o.project_name,
           to_char(o.modified_at, 'HH24:MI') AS at,
           round(extract(epoch FROM (now() - o.modified_at)) / 60)::int AS idle_min,
           o.message_count AS msgs,
           left(coalesce(latest.tldr, o.first_prompt, ''), 80) AS doing,
           coalesce(latest.is_error, false) AS is_error
    FROM v_session_overview o
    LEFT JOIN latest ON latest.session_id = o.session_id
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
    """
    dsn = ctx.obj["dsn"]
    if not no_ingest:
        sync = SessionSync(dsn=dsn, verbose=not quiet)
        stats = sync.sync_all()
        if not quiet:
            click.echo(stats)
    with SessionArchive(dsn) as a:
        rows = a.query(_SWEEP_HEAD_SQL, (window,))
    if not rows:
        click.echo(f"No sessions active in the last {window} min.")
        return
    click.echo(f"\nActive sessions (last {window} min · idle>{idle}m = quiesced):")
    for r in rows:
        idle_min = int(r["idle_min"] or 0)
        state = "·done" if idle_min >= idle else "live "
        err = " [ERR]" if r["is_error"] else ""
        click.echo(f"  {state} {r['at']} {idle_min:>4}m  "
                   f"{str(r['project_name'] or ''):<18.18} {r['msgs'] or 0:>4}msg  "
                   f"{r['doing']}{err}")


@main.command()
@click.pass_context
def stats(ctx: click.Context) -> None:
    """Show table row counts and database size."""
    with SessionArchive(ctx.obj["dsn"]) as a:
        s = a.statistics()
    width = max(len(k) for k in s)
    for k, v in s.items():
        click.echo(f"  {k:<{width}}  {v:>14,}" if isinstance(v, int) else f"  {k:<{width}}  {v:>14}")


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
