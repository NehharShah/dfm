"""The `dfm` command: the Typer app, global options and subcommand registration."""

import asyncio
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from dfm import __version__
from dfm.cli.ask_cmd import ask
from dfm.cli.benchmark_cmd import benchmark
from dfm.cli.ci_cmd import ci
from dfm.cli.config_cmd import config_app
from dfm.cli.diff_cmd import diff
from dfm.cli.doctor_cmd import doctor
from dfm.cli.github_cmd import (
    comments,
    fix_pr_cmd,
    implement_issue_cmd,
    issue,
    pr,
    review_pr_cmd,
)
from dfm.cli.logs_cmd import inspect_log
from dfm.cli.memory_cmd import memory_app
from dfm.cli.models_cmd import models_app
from dfm.cli.processes_cmd import processes_app
from dfm.cli.repo_cmd import repo_app
from dfm.cli.review_cmd import review, security_review
from dfm.cli.services_cmd import services
from dfm.cli.sessions_cmd import sessions_app
from dfm.cli.setup_cmd import setup
from dfm.cli.status_cmd import profile, status
from dfm.cli.work_cmd import implement, plan, resume
from dfm.config import ConfigError, ConfigOverrides, discover_repo_root
from dfm.tools.git.hardening import harden_process

app = typer.Typer(
    name="dfm",
    help="Developer Foundation Model: local, privacy-first engineering agent.",
    no_args_is_help=False,
    add_completion=False,
)
app.add_typer(config_app, name="config")
app.add_typer(processes_app, name="processes")
app.add_typer(sessions_app, name="sessions")
app.add_typer(repo_app, name="repo")
app.add_typer(models_app, name="models")
app.add_typer(memory_app, name="memory")
app.command("doctor")(doctor)
app.command("diff")(diff)
app.command("ask")(ask)
app.command("plan")(plan)
app.command("implement")(implement)
app.command("resume")(resume)
app.command("review")(review)
app.command("security-review")(security_review)
app.command("issue")(issue)
app.command("implement-issue")(implement_issue_cmd)
app.command("pr")(pr)
app.command("review-pr")(review_pr_cmd)
app.command("fix-pr")(fix_pr_cmd)
app.command("comments")(comments)
app.command("ci")(ci)
app.command("services")(services)
app.command("inspect-log")(inspect_log)
app.command("status")(status)
app.command("profile")(profile)
app.command("benchmark")(benchmark)
app.command("setup")(setup)
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"dfm {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
    offline: Annotated[
        bool, typer.Option("--offline", help="No network at all, for any purpose.")
    ] = False,
    local_models: Annotated[
        bool,
        typer.Option(
            "--local-models",
            help="No remote model inference; engineering networking follows policy (default).",
        ),
    ] = False,
) -> None:
    """Run `dfm` in a repository to start an interactive session."""
    ctx.obj = ConfigOverrides(offline=offline, local_models=local_models)
    if ctx.invoked_subcommand is not None:
        return
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        console.print("dfm: the interactive session needs a terminal. The commands:\n")
        typer.echo(ctx.get_help())
        return
    _interactive(ctx.obj, None)


@app.command("chat")
def chat(
    ctx: typer.Context,
    repo: Annotated[Path | None, typer.Option("--repo", help="Repository root.")] = None,
) -> None:
    """Start an interactive session (what `dfm` alone does in a terminal)."""
    overrides = ctx.find_root().obj
    _interactive(overrides if isinstance(overrides, ConfigOverrides) else None, repo)


def _interactive(overrides: ConfigOverrides | None, repo: Path | None) -> None:
    from dfm.interactive.repl import run  # prompt_toolkit loads only for the session

    try:
        code = asyncio.run(run(discover_repo_root(repo or Path.cwd()), overrides))
    except ConfigError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2) from exc
    raise typer.Exit(code)


def main() -> None:
    harden_process()  # every git process DFM itself starts ignores repository fsmonitor commands
    app()
