"""`dfm ask`."""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from dfm.config import ConfigError, ConfigOverrides, discover_repo_root
from dfm.orchestration.ask import ask as run_ask
from dfm.orchestration.runtime import Runtime

console = Console()


def ask(
    ctx: typer.Context,
    question: Annotated[str, typer.Argument(help="A question about this repository.")],
    repo: Annotated[Path | None, typer.Option("--repo", help="Repository root.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Answer a question about the repository from RepoCanon + RepoKG facts (no edits)."""
    overrides = ctx.find_root().obj
    root = discover_repo_root(repo or Path.cwd())
    try:
        rt = Runtime.create(
            root,
            request=question,
            mode="ask",
            overrides=overrides if isinstance(overrides, ConfigOverrides) else None,
        )
    except ConfigError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2) from exc

    async def run() -> None:
        try:
            answer = await run_ask(rt, question)
        except BaseException:
            await rt.close("failed")
            raise
        await rt.close("completed", answer.text[:200])
        if as_json:
            typer.echo(
                json.dumps(
                    {
                        "answer": answer.text,
                        "model": answer.model,
                        "route": answer.route.route.value,
                        "sources": answer.sources,
                        "degraded": answer.degraded,
                        "session": rt.session.id,
                    },
                    indent=2,
                )
            )
            return
        console.print(answer.text, markup=False)
        footer = f"{answer.sources} · model: {answer.model or 'none'} · session {rt.session.id}"
        console.print(f"\n[dim]{footer}[/dim]")

    asyncio.run(run())
