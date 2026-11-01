"""`dfm processes`: list and stop processes DFM started."""

import asyncio
from typing import Annotated

import typer
from rich.table import Table

from dfm.cli.common import console
from dfm.config import state_dirs
from dfm.processes import NotOwned, ProcessManager, ProcessRegistry, listening_ports
from dfm.processes.manager import pid_alive

processes_app = typer.Typer(help="List and stop processes DFM started (never anyone else's).")


def _registry() -> ProcessRegistry:
    return ProcessRegistry(state_dirs().processes)


@processes_app.callback(invoke_without_command=True)
def list_processes(ctx: typer.Context) -> None:
    """List DFM-owned background processes."""
    if ctx.invoked_subcommand is not None:
        return
    registry = _registry()
    records = registry.records()
    if not records:
        console.print("no DFM-owned processes")
        return
    table = Table("id", "name", "pid", "state", "ports", "session", "command", box=None)
    for record in records:
        live = registry.is_live(record)
        if not live:
            state = "exited"
        elif pid_alive(record.owner_pid):
            state = "running"
        else:
            state = "orphaned"
        ports = ",".join(map(str, listening_ports(record.pgid))) if live else ""
        table.add_row(
            record.id,
            record.name,
            str(record.pid),
            state,
            ports,
            record.session_id,
            " ".join(record.argv),
        )
    console.print(table)


@processes_app.command("stop")
def stop(process_id: Annotated[str, typer.Argument(help="Id from `dfm processes`.")]) -> None:
    """Stop one DFM-owned process and its process group."""
    try:
        asyncio.run(ProcessManager(_registry()).stop(process_id))
    except NotOwned as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"stopped {process_id}")


@processes_app.command("prune")
def prune() -> None:
    """Forget records of processes that already exited."""
    gone = _registry().prune()
    console.print(f"pruned {len(gone)} record(s)")
