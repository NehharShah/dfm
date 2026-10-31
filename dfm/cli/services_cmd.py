"""`dfm services`: the local services this repository needs, what runs, what DFM owns."""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from dfm.cli.work_cmd import _runtime  # pyright: ignore[reportPrivateUsage]
from dfm.services import ServiceError, ServiceManager, detect

console = Console()


def services(
    ctx: typer.Context,
    repo: Annotated[Path | None, typer.Option("--repo", help="Repository root.")] = None,
    start: Annotated[
        bool, typer.Option("--start", help="Start the missing ones (asks first).")
    ] = False,
    stop_owned: Annotated[
        bool, typer.Option("--stop-owned", help="Stop services DFM started.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """List known, running and DFM-owned local services (Postgres, Redis, devnets, ...)."""
    rt = _runtime(ctx, repo, "services", "services")
    manager = ServiceManager(rt.env, rt.pm, rt.policy, rt.dirs.data, on_event=rt.event)

    async def go() -> int:
        detection = detect(rt.repo)
        rows: list[dict[str, object]] = []
        code = 0
        for spec in detection.services:
            row: dict[str, object] = {
                "kind": spec.kind,
                "name": spec.name,
                "port": spec.port,
                "source": spec.source,
                "healthy": await manager.healthy(spec),
            }
            if start and not row["healthy"]:
                try:
                    handle = await manager.ensure(spec)
                except ServiceError as exc:
                    row["error"] = str(exc)
                    code = 1
                else:
                    row["healthy"] = True
                    row["started"] = handle.method
            rows.append(row)
        owned = manager.owned()
        if stop_owned:
            for handle in owned:
                await manager.release(handle)
        payload = {
            "services": rows,
            "owned": [
                {"kind": h.spec.kind, "port": h.spec.port, "method": h.method} for h in owned
            ],
            "stopped": len(owned) if stop_owned else 0,
            "skipped": detection.skipped,
        }
        await rt.close("completed", f"{len(rows)} services")
        if as_json:
            typer.echo(json.dumps(payload, indent=2))
            return code
        if not rows:
            console.print(
                "No services detected (compose files, .env.example URLs, Reflex practices)."
            )
        for row in rows:
            state = "running" if row["healthy"] else "not running"
            extra = f" started via {row['started']}" if "started" in row else ""
            err = f"  [red]{row['error']}[/red]" if "error" in row else ""
            console.print(
                f"{row['kind']:<12} :{row['port']:<6} {state}{extra}  [dim]{row['source']}[/dim]{err}"
            )
        for handle in owned:
            console.print(
                f"[dim]owned by DFM: {handle.spec.kind}:{handle.spec.port} ({handle.method})[/dim]"
            )
        for note in detection.skipped:
            console.print(f"[dim]{note}[/dim]")
        return code

    raise typer.Exit(asyncio.run(go()))
