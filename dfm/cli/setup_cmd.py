"""`dfm setup`."""

import asyncio
import sys
from typing import Annotated

import typer
from rich.console import Console

from dfm.orchestration.setup import setup as run_setup

console = Console()
_STYLE = {"ok": "green", "changed": "cyan", "warn": "yellow", "fail": "red", "skip": "dim"}


def setup(
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Accept the recommended choices.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Check and recommend; write nothing.")
    ] = False,
    evaluate: Annotated[
        bool, typer.Option("--eval", help="Load the chosen coder and run the coding check.")
    ] = False,
) -> None:
    """Prepare this machine for DFM: models, integrations, config, offline checks."""

    def confirm(question: str) -> bool:
        if yes:
            return True
        if not sys.stdin.isatty():
            return False
        return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}

    report = asyncio.run(run_setup(confirm=confirm, dry_run=dry_run, evaluate_models=evaluate))
    for step in report.steps:
        hint = (
            f"  [dim]→ {step.action}[/dim]"
            if step.action and step.status in {"warn", "fail", "skip"}
            else ""
        )
        console.print(
            f"[{_STYLE[step.status]}]{step.status:>7}[/] {step.name:<12} {step.detail}{hint}"
        )
    raise typer.Exit(0 if report.ok else 1)
