"""`dfm models`: local coding models on this machine (discover, recommend, choose, evaluate)."""

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any, Literal

import typer
from rich.table import Table

from dfm.cli.common import console
from dfm.config import load_config, state_dirs
from dfm.config.writer import set_user_value
from dfm.models.mlx.discovery import ModelCandidate, choose, default_roots, discover, usable
from dfm.models.resources import ModelResourceManager

models_app = typer.Typer(help="Local MLX coding models: what is here, what fits, which DFM uses.")


def _candidates() -> list[ModelCandidate]:
    return discover(default_roots(data_dir=state_dirs().data))


def _find(name_or_path: str, candidates: list[ModelCandidate]) -> ModelCandidate | None:
    path = Path(name_or_path).expanduser()
    for c in candidates:
        if c.path == path or c.path.resolve() == path.resolve():
            return c
    matches = [c for c in candidates if name_or_path.lower() in c.name.lower()]
    return matches[0] if len(matches) == 1 else None


@models_app.callback(invoke_without_command=True)
def list_models(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """List local models with size, memory need at the configured context, and fit."""
    if ctx.invoked_subcommand is not None:
        return
    config = load_config(None).config
    context = config.models.primary.context
    resources = ModelResourceManager()
    budget = resources.budget_gb()
    roles = {config.models.primary.model: "primary", config.models.fallback.model: "small"}
    rows: list[dict[str, Any]] = []
    for c in _candidates():
        role = roles.get(str(c.path), "")
        if (
            str(c.path) == config.models.primary.model
            and str(c.path) == config.models.fallback.model
        ):
            role = "primary+small"
        rows.append(
            {
                "name": c.name,
                "path": str(c.path),
                "type": c.model_type,
                "quant": f"{c.quant_bits}-bit {c.quant_mode or ''}".strip()
                if c.quant_bits
                else (c.quant_mode or "-"),
                "size_gb": round(c.size_gb, 1),
                "memory_gb": round(c.memory_gb(context), 1),
                "moe": f"{c.active_experts}/{c.experts}" if c.experts else "",
                "usable": usable(c),
                "fits_now": c.memory_gb(context) <= budget,
                "role": role,
            }
        )
    if as_json:
        typer.echo(
            json.dumps(
                {"budget_gb": round(budget, 1), "context": context, "models": rows}, indent=2
            )
        )
        return
    if not rows:
        console.print(
            "No local MLX models found (Hugging Face cache, LM Studio, DFM models dir). Run `dfm setup`."
        )
        return
    table = Table(box=None)
    for column in (
        "name",
        "type",
        "quant",
        "size GB",
        f"mem@{context // 1024}k",
        "MoE",
        "usable",
        "fits now",
        "role",
    ):
        table.add_column(
            column, no_wrap=True, min_width=14 if column == "name" else None, overflow="fold"
        )
    for r in rows:
        table.add_row(
            r["name"],
            r["type"],
            r["quant"],
            str(r["size_gb"]),
            str(r["memory_gb"]),
            r["moe"],
            "yes" if r["usable"] else "no",
            "yes" if r["fits_now"] else "no",
            r["role"],
        )
    console.print(table)
    console.print(f"[dim]memory budget now: {budget:.1f} GB (available minus headroom)[/dim]")


@models_app.command("recommend")
def recommend(
    apply: Annotated[bool, typer.Option("--apply", help="Write the choice to the config.")] = False,
) -> None:
    """Pick dfm-primary (largest that fits) and dfm-small for this machine."""
    config = load_config(None).config
    resources = ModelResourceManager()
    budget = resources.budget_gb()
    primary, small = choose(_candidates(), budget, config.models.primary.context)
    if primary is None or small is None:
        console.print(
            f"No usable local model fits {budget:.1f} GB at context {config.models.primary.context}."
        )
        raise typer.Exit(1)
    console.print(
        f"dfm-primary: {primary.describe()} (~{primary.memory_gb(config.models.primary.context):.1f} GB)"
    )
    console.print(f"dfm-small:   {small.describe()}")
    if apply:
        set_user_value("models.primary.model", str(primary.path))
        path = set_user_value("models.fallback.model", str(small.path))
        console.print(f"written to {path}")


@models_app.command("use")
def use(
    role: Annotated[Literal["primary", "small"], typer.Argument(help="primary or small")],
    model: Annotated[str, typer.Argument(help="Name (substring) or path from `dfm models`.")],
) -> None:
    """Choose which local model DFM uses for a role."""
    candidate = _find(model, _candidates())
    if candidate is None:
        console.print(f"No unique local model matches {model!r}; see `dfm models`.")
        raise typer.Exit(1)
    if not usable(candidate):
        console.print(
            f"{candidate.name} is not usable with the installed mlx-lm (type {candidate.model_type})."
        )
        raise typer.Exit(1)
    key = "models.primary.model" if role == "primary" else "models.fallback.model"
    path = set_user_value(key, str(candidate.path))
    console.print(f"dfm-{role} = {candidate.name}  ({path})")


@models_app.command("eval")
def evaluate_model(
    model: Annotated[str, typer.Argument(help="Name or path from `dfm models`.")],
) -> None:
    """Load a model and run DFM's three-task coding check (local, offline)."""
    from dfm.config import DFMConfig
    from dfm.execution import ExecutionEnvironment
    from dfm.models.mlx.evaluate import evaluate
    from dfm.models.mlx.model import LocalModelSpec, MLXModel

    candidate = _find(model, _candidates())
    if candidate is None:
        console.print(f"No unique local model matches {model!r}.")
        raise typer.Exit(1)
    env = ExecutionEnvironment.create(Path.cwd(), DFMConfig(), session_id="models-eval")
    spec = LocalModelSpec("small", candidate.path, candidate.name, context=16384)
    result = asyncio.run(evaluate(MLXModel(spec, env, resources=ModelResourceManager())))
    env.close()
    console.print(
        f"{result.model}: {len(result.passed)}/{len(result.passed) + len(result.failed)} checks passed in {result.seconds}s"
    )
    console.print(
        f"  passed: {', '.join(result.passed) or '-'}   failed: {', '.join(result.failed) or '-'}"
    )
    console.print(f"  load {result.load_s}s, {result.tokens_per_s} tok/s, peak {result.peak_gb} GB")
    for error in result.errors:
        console.print(f"  [yellow]{error}[/yellow]")
