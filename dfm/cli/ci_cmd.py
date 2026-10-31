"""`dfm ci`: reproduce and fix a failing CI run locally."""

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from dfm.cli.github_cmd import _execute  # pyright: ignore[reportPrivateUsage]
from dfm.cli.work_cmd import _runtime  # pyright: ignore[reportPrivateUsage]
from dfm.orchestration.agent import outcome_payload
from dfm.orchestration.ci import ci as run_ci

console = Console()


def ci(
    ctx: typer.Context,
    run: Annotated[
        int | None,
        typer.Option("--run", help="Workflow run id (default: latest failure on this branch)."),
    ] = None,
    log: Annotated[
        Path | None,
        typer.Option(
            "--log", help="Use this log file (gh run view --log-failed format) instead of GitHub."
        ),
    ] = None,
    repo: Annotated[Path | None, typer.Option("--repo", help="Repository root.")] = None,
    offline: Annotated[
        bool, typer.Option("--offline", help="Use cached GitHub data only.")
    ] = False,
    max_steps: Annotated[
        int, typer.Option("--max-steps", help="Coder steps before stopping.")
    ] = 25,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Find the failing CI step, reproduce it locally, and fix it only if it fails here too.

    Never re-runs or cancels remote workflows."""
    rt = _runtime(ctx, repo, "fix CI", "ci")

    async def work() -> tuple[int, str]:
        result = await run_ci(
            rt, run_id=run, log_path=log, max_steps=max_steps, refresh=not offline
        )
        outcome = result.outcome
        if as_json:
            payload = outcome_payload(outcome) | {
                "job": result.step.job,
                "step": result.step.title,
                "reproduced": result.reproduction.command is not None,
                "notes": result.notes,
            }
            typer.echo(json.dumps(payload, indent=2))
        else:
            console.print(f"CI failure: {result.step.job} / {result.step.title}", markup=False)
            for note in result.notes:
                console.print(f"[dim]{note}[/dim]")
            console.print(outcome.report(), markup=False)
        return (0 if outcome.status == "completed" else 1), outcome.summary

    _execute(rt, work)
