"""`dfm sessions`: sessions recorded for the current repository."""

from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from dfm.cli.common import console
from dfm.config import discover_repo_root, state_dirs
from dfm.sessions import SessionStore

sessions_app = typer.Typer(help="List DFM sessions for this repository.")


@sessions_app.callback(invoke_without_command=True)
def list_sessions(
    ctx: typer.Context,
    all_repos: Annotated[
        bool, typer.Option("--all", help="Sessions from every repository.")
    ] = False,
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    repo = None if all_repos else discover_repo_root(Path.cwd())
    summaries = [s for store in _stores() for s in store.list_sessions(repo)]
    summaries.sort(key=lambda s: s.id, reverse=True)
    if not summaries:
        console.print("no sessions")
        return
    table = Table(box=None)
    table.add_column("session", no_wrap=True)
    table.add_column("started", no_wrap=True)
    table.add_column("mode", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("request", overflow="fold")
    for s in summaries[:50]:
        request = s.request if len(s.request) <= 70 else s.request[:69] + "…"
        table.add_row(s.id, s.started_at[:16].replace("T", " "), s.mode, s.status, request)
    console.print(table)


def _stores() -> list[SessionStore]:
    base = state_dirs().sessions
    return [SessionStore(base), SessionStore(base / "private")]


def _locate(session_id: str) -> Path:
    for store in _stores():
        path = store.dir / f"{session_id}.jsonl"
        if path.exists():
            return path
    console.print(f"no session {session_id}")
    raise typer.Exit(1)


@sessions_app.command("export")
def export(
    session_id: Annotated[str, typer.Argument(help="Session id from `dfm sessions`.")],
) -> None:
    """Print a session trace (versioned JSONL, already redacted): the format Reflex reads."""
    typer.echo(_locate(session_id).read_text(), nl=False)


@sessions_app.command("validate")
def validate(
    session_id: Annotated[str, typer.Argument(help="Session id from `dfm sessions`.")],
) -> None:
    """Check a trace against the DFM session protocol v1."""
    from dfm.sessions.protocol import validate_file

    errors = validate_file(_locate(session_id))
    for error in errors:
        console.print(f"[red]{error}[/red]")
    if errors:
        raise typer.Exit(1)
    console.print("valid (DFM session protocol v1)")
