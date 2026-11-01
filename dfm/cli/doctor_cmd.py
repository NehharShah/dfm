"""`dfm doctor`."""

import json
from typing import Annotated

import typer
from rich.table import Table

from dfm.cli.common import console
from dfm.config import ConfigError, load_config
from dfm.doctor import Status, as_json, exit_code, run_checks

_STYLE = {Status.OK: "green", Status.WARN: "yellow", Status.FAIL: "red", Status.SKIP: "dim"}


def doctor(
    ctx: typer.Context,
    as_json_output: Annotated[
        bool, typer.Option("--json", help="Machine-readable output.")
    ] = False,
) -> None:
    """Check this machine: models, integrations, tools, policy. Read-only and offline."""
    overrides = ctx.find_root().obj
    try:
        loaded = load_config(None, overrides)
    except ConfigError:
        loaded = None  # run_checks reports it
    results = run_checks(loaded=loaded)
    if as_json_output:
        typer.echo(json.dumps(as_json(results), indent=2))
        raise typer.Exit(exit_code(results))
    table = Table(box=None, show_header=False)
    table.add_column("group", style="dim", no_wrap=True)
    table.add_column("check", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("detail", overflow="fold")
    for r in results:
        detail = r.detail + (
            f"  [dim]→ {r.remedy}[/dim]" if r.remedy and r.status is not Status.OK else ""
        )
        table.add_row(r.group, r.name, f"[{_STYLE[r.status]}]{r.status.value}[/]", detail)
    console.print(table)
    raise typer.Exit(exit_code(results))
