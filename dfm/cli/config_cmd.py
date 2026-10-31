"""`dfm config`: inspect the effective configuration and where it comes from."""

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from dfm.config import ConfigError, ConfigOverrides, LoadedConfig, discover_repo_root, load_config
from dfm.config.loader import flatten
from dfm.config.paths import state_dirs

config_app = typer.Typer(help="Show the effective configuration and its file locations.")
console = Console()
err = Console(stderr=True)

RepoOption = Annotated[
    Path | None,
    typer.Option("--repo", help="Repository root (default: nearest ancestor with .git)."),
]


def _overrides(ctx: typer.Context) -> ConfigOverrides:
    root = ctx.find_root().obj
    return root if isinstance(root, ConfigOverrides) else ConfigOverrides()


def _load(ctx: typer.Context, repo: Path | None) -> LoadedConfig:
    root = discover_repo_root(repo or Path.cwd())
    try:
        loaded = load_config(root, _overrides(ctx))
    except ConfigError as exc:
        err.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    for warning in loaded.warnings:
        err.print(f"[yellow]warning:[/yellow] {warning}")
    return loaded


@config_app.command("show")
def show(
    ctx: typer.Context,
    repo: RepoOption = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Print every effective setting with the layer that set it."""
    loaded = _load(ctx, repo)
    values = flatten(loaded.config.model_dump(mode="json"))
    if as_json:
        payload = {
            "config": loaded.config.model_dump(mode="json"),
            "sources": dict(loaded.sources),
            "warnings": list(loaded.warnings),
        }
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    table = Table("setting", "value", "source", box=None, pad_edge=False)
    for key, value in values.items():
        table.add_row(key, json.dumps(value), loaded.sources.get(key, "default"))
    console.print(table)


@config_app.command("path")
def path(ctx: typer.Context, repo: RepoOption = None) -> None:
    """Print the config files DFM reads and its state directory."""
    loaded = _load(ctx, repo)
    dirs = state_dirs()

    def status(p: Path) -> str:
        return "exists" if p.exists() else "not found"

    typer.echo(f"user config  {loaded.user_path}  ({status(loaded.user_path)})")
    if loaded.repo_path is not None:
        typer.echo(f"repo config  {loaded.repo_path}  ({status(loaded.repo_path)})")
    typer.echo(f"state        {dirs.data}  ({status(dirs.data)})")
