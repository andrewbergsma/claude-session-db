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
