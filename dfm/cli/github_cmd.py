"""`dfm issue|implement-issue|pr|review-pr|fix-pr|comments N`: GitHub via gh, cache-backed."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from dfm.cli.work_cmd import _runtime  # pyright: ignore[reportPrivateUsage]
from dfm.integrations.github import GitHubError
from dfm.orchestration.agent import TaskOutcome, outcome_payload
from dfm.orchestration.github import fix_pr, github_for, implement_issue, post_replies, review_pr
from dfm.orchestration.implement import ImplementOptions
from dfm.orchestration.review import SEVERITY_ORDER
from dfm.orchestration.runtime import Runtime
from dfm.tools.errors import ToolError

console = Console()

Number = Annotated[int, typer.Argument(help="Issue or pull request number.")]
RepoOption = Annotated[Path | None, typer.Option("--repo", help="Repository root.")]
Offline = Annotated[
    bool, typer.Option("--offline", help="Use the local cache only; do not contact GitHub.")
]
JsonOption = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]
MaxSteps = Annotated[int, typer.Option("--max-steps", help="Coder steps before stopping.")]


def _execute(rt: Runtime, work: Callable[[], Awaitable[tuple[int, str]]]) -> None:
    """Run `work` (→ exit code, session summary); GitHub/tool errors exit 2 with a message."""

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


def _print_outcome(outcome: TaskOutcome, as_json: bool, extra: dict[str, object]) -> int:
    if as_json:
        typer.echo(json.dumps(outcome_payload(outcome) | extra, indent=2))
    else:
        console.print(outcome.report(), markup=False)
    return 0 if outcome.status == "completed" else 1


def issue(
    ctx: typer.Context,
    number: Number,
    repo: RepoOption = None,
    offline: Offline = False,
    as_json: JsonOption = False,
) -> None:
    """Show a GitHub issue (live, or from the cache when GitHub is unavailable)."""
    rt = _runtime(ctx, repo, f"show issue #{number}", "github")

    async def work() -> tuple[int, str]:
        cached = await github_for(rt).issue(number, refresh=not offline)
        i = cached.value
        if as_json:
            typer.echo(json.dumps({**i.__dict__, "source": cached.source}, indent=2))
        else:
            console.print(f"#{i.number} {i.title} [{i.state}] by {i.author}", markup=False)
            console.print(f"[dim]{cached.staleness()} · {i.url}[/dim]")
            console.print(i.body, markup=False)
        return 0, f"issue #{number}"

    _execute(rt, work)


def implement_issue_cmd(
    ctx: typer.Context,
    number: Number,
    repo: RepoOption = None,
    offline: Offline = False,
    max_steps: MaxSteps = 25,
    as_json: JsonOption = False,
) -> None:
    """Implement a GitHub issue in the working tree; works offline from the cache."""
    rt = _runtime(ctx, repo, f"implement issue #{number}", "implement")

    async def work() -> tuple[int, str]:
        cached, result = await implement_issue(
            rt, number, ImplementOptions(max_steps=max_steps), refresh=not offline
        )
        console.print(f"[dim]issue #{number}: {cached.staleness()}[/dim]")
        code = _print_outcome(result.outcome, as_json, {"issue": number, "source": cached.source})
        return code, result.outcome.summary

    _execute(rt, work)


def pr(
    ctx: typer.Context,
    number: Number,
    repo: RepoOption = None,
    offline: Offline = False,
    as_json: JsonOption = False,
) -> None:
    """Show a pull request with its files and check results."""
    rt = _runtime(ctx, repo, f"show PR #{number}", "github")

    async def work() -> tuple[int, str]:
        gh = github_for(rt)
        cached = await gh.pr(number, refresh=not offline)
        checks = await gh.checks(number, refresh=not offline)
        p = cached.value
        if as_json:
            payload = {
                **p.__dict__,
                "checks": [c.__dict__ for c in checks.value],
                "source": cached.source,
            }
            typer.echo(json.dumps(payload, indent=2))
            return 0, f"PR #{number}"
        console.print(
            f"#{p.number} {p.title} [{p.state}] {p.head_ref} → {p.base_ref}", markup=False
        )
        console.print(f"[dim]{cached.staleness()} · {p.url}[/dim]")
        for path in p.files:
            console.print(f"  {path}", markup=False)
        for check in checks.value:
            mark = "✘" if check.failed else ("✔" if check.bucket == "pass" else "·")
            console.print(f"  {mark} {check.workflow} / {check.name}: {check.state}", markup=False)
        return 0, f"PR #{number}"

    _execute(rt, work)


def comments(
    ctx: typer.Context,
    number: Number,
    repo: RepoOption = None,
    offline: Offline = False,
    as_json: JsonOption = False,
) -> None:
    """List a pull request's inline review comments."""
    rt = _runtime(ctx, repo, f"PR #{number} comments", "github")

    async def work() -> tuple[int, str]:
        cached = await github_for(rt).comments(number, refresh=not offline)
        if as_json:
            typer.echo(json.dumps([c.__dict__ for c in cached.value], indent=2))
        else:
            console.print(f"[dim]{cached.staleness()}[/dim]")
            for c in cached.value:
                console.print(f"{c.path}:{c.line} ({c.author})\n  {c.body}", markup=False)
        return 0, f"{len(cached.value)} comments"

    _execute(rt, work)


def review_pr_cmd(
    ctx: typer.Context,
    number: Number,
    repo: RepoOption = None,
    offline: Offline = False,
    as_json: JsonOption = False,
) -> None:
    """Review a pull request's diff (PR head checked out in a DFM worktree when possible)."""
    rt = _runtime(ctx, repo, f"review PR #{number}", "review")

    async def work() -> tuple[int, str]:
        result = await review_pr(rt, number, refresh=not offline)
        report = result.report
        report.notes = result.notes + report.notes
        if as_json:
            payload = {
                "findings": [f.__dict__ for f in report.findings],
                "dropped": report.dropped,
                "notes": report.notes,
            }
            typer.echo(json.dumps(payload, indent=2))
        else:
            console.print(report.render(), markup=False)
        worst = min((SEVERITY_ORDER[f.severity] for f in report.findings), default=9)
        return (1 if worst <= SEVERITY_ORDER["HIGH"] else 0), f"{len(report.findings)} findings"

    _execute(rt, work)


def fix_pr_cmd(
    ctx: typer.Context,
    number: Number,
    repo: RepoOption = None,
    offline: Offline = False,
    max_steps: MaxSteps = 25,
    post_replies_: Annotated[
        bool,
        typer.Option("--post-replies", help="Post the drafted replies (each one asks first)."),
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """Triage review comments, fix the valid ones in a worktree (a patch), draft replies.

    Never pushes; prints the commands for you to apply and push."""
    rt = _runtime(ctx, repo, f"fix PR #{number}", "implement")

    async def work() -> tuple[int, str]:
        fixed = await fix_pr(rt, number, ImplementOptions(max_steps=max_steps), refresh=not offline)
        posted = await post_replies(rt, fixed) if post_replies_ else []
        outcome = fixed.result.outcome if fixed.result else None
        if as_json:
            payload: dict[str, object] = {
                "pr": number,
                "threads": [
                    {"location": t.location, "verdict": t.verdict, "reasoning": t.reasoning}
                    for t in fixed.triaged
                ],
                "replies": [
                    {"location": f"{r.thread.path}:{r.thread.line}", "body": r.body}
                    for r in fixed.replies
                ],
                "patch": str(fixed.result.patch_path)
                if fixed.result and fixed.result.patch_path
                else None,
                "next_steps": fixed.next_steps(),
                "posted": posted,
                "notes": fixed.notes,
            }
            if outcome is not None:
                payload["outcome"] = outcome_payload(outcome)
            typer.echo(json.dumps(payload, indent=2))
        else:
            for t in fixed.triaged:
                console.print(f"{t.verdict:>8}  {t.location}  {t.reasoning}", markup=False)
            if outcome is not None:
                console.print()
                console.print(outcome.report(), markup=False)
            for r in fixed.replies:
                console.print(
                    f"\nDraft reply ({r.thread.path}:{r.thread.line}):\n  {r.body}", markup=False
                )
            for line in posted:
                console.print(f"[dim]{line}[/dim]")
            if fixed.next_steps():
                console.print("\nTo apply and push (DFM does not push):")
                for step in fixed.next_steps():
                    console.print(f"  {step}", markup=False)
        ok = outcome is None or outcome.status == "completed"
        return (0 if ok else 1), outcome.summary if outcome else "triage only"

    _execute(rt, work)
