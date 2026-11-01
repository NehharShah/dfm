"""`dfm repo`: what DFM knows about this repository (RepoCanon, RepoKG)."""

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

import typer

from dfm.cli.common import JsonOption, RepoOption, console
from dfm.config import discover_repo_root, load_config, state_dirs
from dfm.integrations.repocanon import RepoCanonProvider, summary
from dfm.integrations.repokg import RepoKGProvider, graph_summary, module_for, neighborhood

repo_app = typer.Typer(help="Repository intelligence: RepoCanon facts and the RepoKG graph.")


def _print_mapping(data: dict[str, Any]) -> None:
    for key, value in data.items():
        if value in ([], {}, "", None):
            continue
        if isinstance(value, dict):
            console.print(f"[bold]{key}[/bold]")
            for k, v in value.items():  # pyright: ignore[reportUnknownVariableType]
                console.print(f"  {k}: {v}")
        elif isinstance(value, list):
            console.print(f"[bold]{key}[/bold]")
            for item in value:  # pyright: ignore[reportUnknownVariableType]
                console.print(f"  - {item}")
        else:
            console.print(f"[bold]{key}[/bold]: {value}")


def canon_provider() -> RepoCanonProvider:
    return RepoCanonProvider(cache_root=state_dirs().cache)


@repo_app.command("canon")
def canon(repo: RepoOption = None, as_json: JsonOption = False) -> None:
    """How this repository is built, tested and organized (RepoCanon)."""
    root = discover_repo_root(repo or Path.cwd())
    provider = canon_provider()
    state = provider.state(root)
    result = asyncio.run(provider.analyze(root))
    data = {
        **summary(result),
        "freshness": f"{state.value} -> fresh" if state.value != "fresh" else "fresh",
    }
    if as_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        _print_mapping(data)


def graph_provider() -> RepoKGProvider:
    config = load_config(None).config
    return RepoKGProvider(
        enabled=config.integrations.repokg.enabled,
        cache_root=state_dirs().cache,
        github=config.network.github == "allow",
    )


@repo_app.command("graph")
def graph(
    path: Annotated[str | None, typer.Argument(help="A file or module to centre on.")] = None,
    repo: RepoOption = None,
    as_json: JsonOption = False,
) -> None:
    """How this repository is connected (RepoKG): modules, edges, ops surface."""
    root = discover_repo_root(repo or Path.cwd())
    provider = graph_provider()
    kg = asyncio.run(provider.graph(root))
    if kg is None:
        console.print(f"RepoKG unavailable: {provider.last_error}")
        raise typer.Exit(1)
    if path is None:
        data: dict[str, Any] = graph_summary(kg)
    else:
        module = module_for(kg, path) or path
        data = dict(neighborhood(kg, module))
    if as_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        _print_mapping(data)


@repo_app.command("refresh")
def refresh(repo: RepoOption = None) -> None:
    """Re-analyze the repository now, ignoring caches."""
    root = discover_repo_root(repo or Path.cwd())
    asyncio.run(canon_provider().refresh(root, force=True))
    console.print("RepoCanon  refreshed")
    provider = graph_provider()
    kg = asyncio.run(provider.refresh(root, force=True))
    console.print(
        "RepoKG     refreshed"
        if kg is not None
        else f"RepoKG     unavailable: {provider.last_error}"
    )
