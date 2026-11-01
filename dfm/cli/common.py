"""What every `dfm` subcommand shares: the console, the common options, and the runtime
scaffolding (build it from the repository and the global flags; close it however the work ends).

Commands import from here instead of from each other.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from dfm.config import ConfigError, ConfigOverrides, discover_repo_root
from dfm.integrations.github import GitHubError
from dfm.orchestration.runtime import Runtime
from dfm.sessions import SessionStatus
from dfm.tools.errors import ToolError

console = Console()

RepoOption = Annotated[Path | None, typer.Option("--repo", help="Repository root.")]
JsonOption = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]
MaxSteps = Annotated[int, typer.Option("--max-steps", help="Coder steps before stopping.")]


def overrides_from(ctx: typer.Context) -> ConfigOverrides | None:
    """The global flags (`--offline`, `--local-models`) the root command stored on the context."""
    obj = ctx.find_root().obj
    return obj if isinstance(obj, ConfigOverrides) else None


def make_runtime(ctx: typer.Context, repo: Path | None, request: str, mode: str) -> Runtime:
    """A Runtime for the repository at `repo` (or the one around cwd); an invalid user config is
    exit code 2 with the reason, never a traceback."""
    root = discover_repo_root(repo or Path.cwd())
    try:
        return Runtime.create(root, request=request, mode=mode, overrides=overrides_from(ctx))
    except ConfigError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2) from exc


async def closing[T](
    rt: Runtime, work: Awaitable[T], *, status: Callable[[T], tuple[SessionStatus, str]]
) -> T:
    """Await `work`, then close the runtime with `status(result)` → (session status, summary).
    If the work raises or is cancelled the session closes as failed and the error propagates."""
    try:
        result = await work
    except BaseException:
        await rt.close("failed")
        raise
    session_status, summary = status(result)
    await rt.close(session_status, summary)
    return result


def execute(rt: Runtime, work: Callable[[], Awaitable[tuple[int, str]]]) -> None:
    """Run `work` (→ exit code, session summary) to completion and exit with its code.
    GitHub and tool errors are exit code 2 with a message; anything else propagates."""

    async def go() -> int:
        try:
            code, summary = await work()
        except (GitHubError, ToolError) as exc:
            await rt.close("failed", str(exc))
            console.print(f"[red]error:[/red] {exc}")
            return 2
        except BaseException:
            await rt.close("failed")
            raise
        await rt.close("completed" if code == 0 else "failed", summary)
        return code

    raise typer.Exit(asyncio.run(go()))
