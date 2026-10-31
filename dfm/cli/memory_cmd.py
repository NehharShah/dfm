"""`dfm memory`: what Reflex learned from past sessions (read-only)."""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from dfm.cli.work_cmd import _runtime  # pyright: ignore[reportPrivateUsage]
from dfm.config import discover_repo_root

memory_app = typer.Typer(
    help="Reflex practices and skills relevant to this repository (read-only)."
)
console = Console()
RepoOption = Annotated[Path | None, typer.Option("--repo", help="Repository root.")]
JsonOption = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]


def _show(ctx: typer.Context, repo: Path | None, task: str | None, as_json: bool) -> None:
    root = discover_repo_root(repo or Path.cwd())
    rt = _runtime(ctx, root, task or "memory practices", "memory")

    async def go() -> None:
        practices = await rt.reflex.practices(rt.repo, task)
        await rt.close("completed")
        if as_json:
            typer.echo(json.dumps([p.__dict__ for p in practices], indent=2))
            return
        if not practices:
            console.print(f"No practices found ({rt.reflex.last_error or 'nothing recorded yet'}).")
            return
        for p in practices:
            console.print(f"- {p.line()}", markup=False)
        console.print(
            "[dim]historical evidence from Reflex; never overrides your instructions or test results[/dim]"
        )

    asyncio.run(go())


@memory_app.command("practices")
def practices(ctx: typer.Context, repo: RepoOption = None, as_json: JsonOption = False) -> None:
    """How work is done in this repository, learned from past sessions."""
    _show(ctx, repo, None, as_json)


@memory_app.command("similar")
def similar(
    ctx: typer.Context,
    task: Annotated[str, typer.Argument(help="A task description.")],
    repo: RepoOption = None,
    as_json: JsonOption = False,
) -> None:
    """Practices relevant to a task."""
    _show(ctx, repo, task, as_json)


@memory_app.command("skills")
def skills(
    ctx: typer.Context,
    task: Annotated[str | None, typer.Argument(help="Rank by relevance to this task.")] = None,
    repo: RepoOption = None,
) -> None:
    """Approved Reflex skills DFM can use."""
    root = discover_repo_root(repo or Path.cwd())
    rt = _runtime(ctx, root, "memory skills", "memory")
    found = rt.reflex.matching_skills(task, limit=10, min_score=1) if task else rt.reflex.skills()
    asyncio.run(rt.close("completed"))
    if not found:
        console.print(
            "No approved Reflex skills"
            + (" match." if task else " (approve suggestions with `reflex approve`).")
        )
        return
    for skill in found:
        console.print(f"- {skill.name}: {skill.description[:120]}", markup=False)
