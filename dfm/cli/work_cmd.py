"""`dfm plan`, `dfm implement` and `dfm resume`."""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from dfm.config import ConfigError, ConfigOverrides, discover_repo_root
from dfm.orchestration.agent import outcome_payload
from dfm.orchestration.implement import ImplementOptions
from dfm.orchestration.implement import implement as run_implement
from dfm.orchestration.plan import plan as run_plan
from dfm.orchestration.runtime import Runtime
from dfm.routing import is_resume_request
from dfm.tools.errors import ToolError

console = Console()

RepoOption = Annotated[Path | None, typer.Option("--repo", help="Repository root.")]
JsonOption = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]


def _runtime(ctx: typer.Context, repo: Path | None, request: str, mode: str) -> Runtime:
    overrides = ctx.find_root().obj
    root = discover_repo_root(repo or Path.cwd())
    try:
        return Runtime.create(
            root,
            request=request,
            mode=mode,
            overrides=overrides if isinstance(overrides, ConfigOverrides) else None,
        )
    except ConfigError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2) from exc


def plan(
    ctx: typer.Context,
    task: Annotated[str, typer.Argument(help="What you want done.")],
    repo: RepoOption = None,
    as_json: JsonOption = False,
) -> None:
    """Plan a change without editing anything (tests run in a read-only sandbox)."""
    rt = _runtime(ctx, repo, task, "plan")

    async def go() -> None:
        try:
            result = await run_plan(rt, task)
        except BaseException:
            await rt.close("failed")
            raise
        await rt.close("completed", "plan written")
        if as_json:
            typer.echo(json.dumps({**result.to_dict(), "session": rt.session.id}, indent=2))
        else:
            console.print(result.render(), markup=False)
            console.print(
                f'\n[dim]session {rt.session.id} · run it: dfm implement --from-plan {rt.session.id} "{task}"[/dim]'
            )

    asyncio.run(go())


def implement(
    ctx: typer.Context,
    task: Annotated[str, typer.Argument(help="What you want done.")],
    repo: RepoOption = None,
    from_plan: Annotated[
        str | None, typer.Option("--from-plan", help="Session id of a saved plan.")
    ] = None,
    worktree: Annotated[
        bool, typer.Option("--worktree", help="Work in an isolated worktree; get a patch.")
    ] = False,
    max_steps: Annotated[
        int, typer.Option("--max-steps", help="Coder steps before stopping.")
    ] = 25,
    allow_generated: Annotated[
        bool,
        typer.Option("--allow-generated", help="Allow editing generated files directly."),
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """Make the change, run the checks, and report only what was verified."""
    if is_resume_request(task) and from_plan is None and not worktree:
        resume(ctx, None, repo, max_steps)  # "resume what we were doing yesterday"
        return
    rt = _runtime(ctx, repo, task, "implement")

    async def go() -> int:
        try:
            result = await run_implement(
                rt,
                task,
                ImplementOptions(
                    from_plan=from_plan,
                    worktree=worktree,
                    max_steps=max_steps,
                    allow_generated_edits=allow_generated,
                ),
            )
        except ToolError as exc:
            await rt.close("failed", str(exc))
            console.print(f"[red]error:[/red] {exc}")
            return 2
        except BaseException:
            await rt.close("failed")
            raise
        outcome = result.outcome
        await rt.close(outcome.session_status, outcome.summary)
        if as_json:
            payload = outcome_payload(outcome) | {
                "patch": str(result.patch_path) if result.patch_path else None
            }
            typer.echo(json.dumps(payload, indent=2))
        else:
            console.print(outcome.report(), markup=False)
            console.print(f"\n[dim]session {rt.session.id}[/dim]")
        return 0 if outcome.status == "completed" else 1

    raise typer.Exit(asyncio.run(go()))


def resume(
    ctx: typer.Context,
    session: Annotated[
        str | None, typer.Argument(help="Session id (default: the latest interrupted one).")
    ] = None,
    repo: RepoOption = None,
    max_steps: Annotated[
        int, typer.Option("--max-steps", help="Coder steps before stopping.")
    ] = 25,
) -> None:
    """Continue an interrupted session after checking what changed since."""
    from dfm.orchestration.resume import resume as run_resume

    rt = _runtime(ctx, repo, "resume", "resume")

    async def go() -> int:
        try:
            result = await run_resume(rt, session, max_steps=max_steps)
        except ToolError as exc:
            await rt.close("aborted", str(exc))
            console.print(f"[red]error:[/red] {exc}")
            return 2
        except BaseException:
            await rt.close("failed")
            raise
        console.print(result.summary, markup=False)
        outcome = result.outcome
        assert outcome is not None  # noqa: S101
        await rt.close(outcome.session_status, outcome.summary)
        console.print()
        console.print(outcome.report(), markup=False)
        return 0 if outcome.status == "completed" else 1

    raise typer.Exit(asyncio.run(go()))
