"""`dfm ask`."""

import asyncio
import json
from typing import Annotated

import typer

from dfm.cli.common import JsonOption, RepoOption, closing, console, make_runtime
from dfm.orchestration.ask import ask as run_ask


def ask(
    ctx: typer.Context,
    question: Annotated[str, typer.Argument(help="A question about this repository.")],
    repo: RepoOption = None,
    as_json: JsonOption = False,
) -> None:
    """Answer a question about the repository from RepoCanon + RepoKG facts (no edits)."""
    rt = make_runtime(ctx, repo, question, "ask")

    async def run() -> None:
        answer = await closing(
            rt, run_ask(rt, question), status=lambda a: ("completed", a.text[:200])
        )
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
