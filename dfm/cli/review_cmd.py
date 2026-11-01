"""`dfm review` and `dfm security-review`: review the working-tree diff, a ref range,
or a diff on stdin (`--stdin`)."""

import asyncio
import json
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

import typer

from dfm.cli.common import JsonOption, RepoOption, console, make_runtime
from dfm.config import discover_repo_root
from dfm.orchestration.review import SEVERITY_ORDER, ReviewReport
from dfm.orchestration.review import review as run_review
from dfm.orchestration.runtime import Runtime
from dfm.orchestration.security_review import security_review as run_security_review

BaseOption = Annotated[str, typer.Option("--base", help="Compare against this ref.")]
StagedOption = Annotated[bool, typer.Option("--staged", help="Review staged changes only.")]
StdinOption = Annotated[
    bool, typer.Option("--stdin", help="Read the diff from stdin instead of running git.")
]


def _run(
    ctx: typer.Context,
    base: str,
    staged: bool,
    repo: Path | None,
    as_json: bool,
    request: str,
    reviewer: Callable[[Runtime, str, str], Awaitable[ReviewReport]],
    stdin: bool = False,
) -> None:
    root = discover_repo_root(repo or Path.cwd())
    # Only an explicit --stdin reads stdin: in CI stdin is rarely a TTY, and reading an empty
    # pipe there instead of `git diff --base` would report a false "no findings".
    if stdin:
        diff_text = sys.stdin.read()
    else:
        args = ["git", "-C", str(root), "diff", "--no-color", "--no-ext-diff"]
        args += ["--cached"] if staged else [base]
        out = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603
        if out.returncode != 0:
            console.print(
                f"[red]error:[/red] git diff failed: {out.stderr.strip()[:300]}", markup=True
            )
            raise typer.Exit(2)
        diff_text = out.stdout
    rt = make_runtime(ctx, root, request, "review")

    async def go() -> int:
        try:
            report = await reviewer(rt, diff_text, base)
        except BaseException:
            await rt.close("failed")
            raise
        await rt.close("completed", f"{len(report.findings)} findings")
        if as_json:
            typer.echo(
                json.dumps(
                    {
                        "findings": [f.__dict__ for f in report.findings],
                        "files": report.files,
                        "dropped": report.dropped,
                        "notes": report.notes,
                        "architecture": report.architecture,
                        "sections": report.sections,
                        "session": rt.session.id,
                    },
                    indent=2,
                )
            )
        else:
            console.print(report.render(), markup=False)
        worst = min((SEVERITY_ORDER[f.severity] for f in report.findings), default=9)
        return 1 if worst <= SEVERITY_ORDER["HIGH"] else 0

    raise typer.Exit(asyncio.run(go()))


def review(
    ctx: typer.Context,
    base: BaseOption = "HEAD",
    staged: StagedOption = False,
    repo: RepoOption = None,
    as_json: JsonOption = False,
    stdin: StdinOption = False,
) -> None:
    """Review a diff: deterministic checks, structure, and verified model findings."""
    _run(
        ctx,
        base,
        staged,
        repo,
        as_json,
        "review the diff",
        lambda rt, diff, ref: run_review(rt, diff, base=ref),
        stdin,
    )


def security_review(
    ctx: typer.Context,
    base: BaseOption = "HEAD",
    staged: StagedOption = False,
    repo: RepoOption = None,
    as_json: JsonOption = False,
    stdin: StdinOption = False,
) -> None:
    """Security review of a diff: checklists, removed auth checks, dependency changes,
    ops surface, and verified model findings. Exit 1 on HIGH or BLOCKER findings."""
    _run(
        ctx,
        base,
        staged,
        repo,
        as_json,
        "security review of the diff",
        lambda rt, diff, ref: run_security_review(rt, diff, base=ref),
        stdin,
    )
