"""`dfm diff`: the working-tree diff, and optionally its structural impact (RepoKG)."""

import asyncio
import subprocess
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from dfm.cli.repo_cmd import canon_provider, graph_provider
from dfm.config import discover_repo_root
from dfm.integrations.repokg.structure import (
    graph_at_ref_or_error,
    roles_from_canon,
    structural_diff,
)

console = Console()


def diff(
    structure: Annotated[
        bool, typer.Option("--structure", help="Compare the RepoKG graph with the base ref.")
    ] = False,
    base: Annotated[str, typer.Option("--base", help="Git ref to compare against.")] = "HEAD",
    repo: Annotated[Path | None, typer.Option("--repo", help="Repository root.")] = None,
) -> None:
    """Show what changed against a base ref (read-only)."""
    root = discover_repo_root(repo or Path.cwd())
    stat = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "diff", "--stat", base],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if stat.returncode != 0:
        console.print(f"git diff failed: {stat.stderr.strip()}")
        raise typer.Exit(1)
    console.print(stat.stdout.rstrip() or "No changes against " + base)
    if not structure:
        return
    provider = graph_provider()
    after = asyncio.run(provider.graph(root))
    before, why = asyncio.run(graph_at_ref_or_error(root, base))
    if after is None or before is None:
        reason = provider.last_error or (f"cannot build graph at {base}: {why}" if why else base)
        console.print(f"Structural diff unavailable: {reason}")
        raise typer.Exit(1)
    roles = roles_from_canon(asyncio.run(canon_provider().analyze(root)))
    console.print()
    console.print(structural_diff(before, after, roles).markdown(), markup=False)
