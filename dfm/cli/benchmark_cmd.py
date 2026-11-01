"""`dfm benchmark`: ablation benchmarks over local fixture tasks."""

import asyncio
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer

from dfm.benchmark import ABLATIONS, BY_ID, TASKS, run_suite, table, write_results
from dfm.cli.common import console
from dfm.config import state_dirs


def benchmark(
    tasks: Annotated[
        str | None, typer.Option("--tasks", help="Comma-separated task ids (default: all).")
    ] = None,
    ablation: Annotated[
        list[str] | None, typer.Option("--ablation", help=f"Repeatable: {', '.join(ABLATIONS)}.")
    ] = None,
    repeat: Annotated[int, typer.Option("--repeat", help="Runs per task and ablation.")] = 1,
    max_steps: Annotated[int, typer.Option("--max-steps", help="Coder steps per run.")] = 20,
    reference: Annotated[
        bool,
        typer.Option(
            "--reference", help="Replay reference solutions (checks the harness, no model)."
        ),
    ] = False,
    out: Annotated[Path | None, typer.Option("--out", help="Results directory.")] = None,
    model: Annotated[
        Path | None,
        typer.Option(
            "--model",
            help="Local MLX model directory: the only coder for every step (primary and small).",
        ),
    ] = None,
    cooldown: Annotated[
        float,
        typer.Option(
            "--cooldown", help="Seconds to pause after each run that used a model (heat)."
        ),
    ] = 30.0,
    list_tasks: Annotated[bool, typer.Option("--list", help="List the tasks and exit.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print results as JSON.")] = False,
) -> None:
    """Run every task in a fresh copy, offline, under each ablation; write JSON + a markdown table."""
    if list_tasks:
        for t in TASKS:
            needs = f" (needs {', '.join(t.needs)})" if t.needs else ""
            console.print(f"{t.id:<24} {t.title}{needs}", markup=False)
        return
    try:
        chosen = [BY_ID[name.strip()] for name in tasks.split(",")] if tasks else list(TASKS)
    except KeyError as exc:
        console.print(f"[red]error:[/red] unknown task {exc}; see `dfm benchmark --list`")
        raise typer.Exit(2) from exc
    ablations = ablation or ["coder", "full"]
    unknown = [a for a in ablations if a not in ABLATIONS]
    if unknown:
        console.print(
            f"[red]error:[/red] unknown ablation {unknown[0]!r} (known: {', '.join(ABLATIONS)})"
        )
        raise typer.Exit(2)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    out_dir = out or state_dirs().data / "benchmarks" / stamp
    # one model in both slots: the router's small-coder routes cannot switch to another model
    pinned = str(model.expanduser()) if model else None
    settings = {"models.primary.model": pinned, "models.fallback.model": pinned} if pinned else None
    results = asyncio.run(
        run_suite(
            chosen,
            ablations,
            workdir=out_dir / "runs",
            reference=reference,
            repeat=repeat,
            max_steps=max_steps,
            settings=settings,
            cooldown_s=0.0 if reference else cooldown,
        )
    )
    json_path, md_path = write_results(
        results,
        out_dir,
        {
            "started": stamp,
            "ablations": ablations,
            "reference": reference,
            "repeat": repeat,
            "model": str(model) if model else None,
            "cooldown_s": 0.0 if reference else cooldown,
        },
    )
    if as_json:
        typer.echo(json.dumps([asdict(r) for r in results], indent=2))
    else:
        console.print(table(results), markup=False)
        console.print(f"[dim]{json_path} · {md_path}[/dim]")
    failed = sum(r.status in {"fail", "error", "invalid"} for r in results)
    raise typer.Exit(1 if failed and reference else 0)
