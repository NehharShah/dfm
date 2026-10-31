"""`dfm status` and `dfm profile`: recent sessions and their metrics."""

import json
from pathlib import Path
from typing import Annotated, Any, cast

import typer
from rich.console import Console

from dfm.config import discover_repo_root, state_dirs
from dfm.observability import load
from dfm.processes import ProcessRegistry
from dfm.sessions import SessionStore

console = Console()


def _store() -> SessionStore:
    dirs = state_dirs()
    return SessionStore(dirs.sessions)


def _private_store() -> SessionStore:
    return SessionStore(state_dirs().sessions / "private")


def status(
    repo: Annotated[Path | None, typer.Option("--repo", help="Repository root.")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Sessions to show.")] = 5,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Recent DFM sessions for this repository, their metrics, and what DFM still runs."""
    root = discover_repo_root(repo or Path.cwd())
    dirs = state_dirs()
    sessions = [*_store().list_sessions(root), *_private_store().list_sessions(root)]
    sessions.sort(key=lambda s: s.updated_at, reverse=True)
    rows: list[dict[str, Any]] = []
    for s in sessions[:limit]:
        m = load(dirs.data / "metrics", s.id) or {}
        rows.append(
            {
                "session": s.id,
                "mode": s.mode,
                "status": s.status,
                "request": s.request[:70],
                "wall_ms": m.get("wall_ms"),
                "memory_peak_mb": m.get("memory_peak_mb"),
                "tool_calls": cast(dict[str, Any], m.get("tool_calls") or {}).get("commands"),
            }
        )
    running = [r for r in ProcessRegistry(dirs.processes).records()]
    if as_json:
        typer.echo(json.dumps({"sessions": rows, "processes": len(running)}, indent=2))
        return
    if not rows:
        console.print("No DFM sessions for this repository yet.")
    for r in rows:
        timing = f"{r['wall_ms'] / 1000:.1f}s" if r["wall_ms"] else "-"
        console.print(
            f"{r['session']}  {r['mode']:<10} {r['status']:<11} {timing:>7}  {r['request']}",
            markup=False,
        )
    console.print(
        f"[dim]{len(running)} DFM-managed process(es) recorded · `dfm profile SESSION` for details[/dim]"
    )


def profile(
    session: Annotated[str | None, typer.Argument(help="Session id (default: the latest).")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Latency per stage, tokens, context size, tool calls, repairs and memory for a session."""
    dirs = state_dirs()
    if session is None:
        latest = sorted(
            [*_store().list_sessions(), *_private_store().list_sessions()],
            key=lambda s: s.updated_at,
        )
        if not latest:
            console.print("No sessions yet.")
            raise typer.Exit(1)
        session = latest[-1].id
    data = load(dirs.data / "metrics", session)
    if data is None:
        console.print(
            f"[red]error:[/red] no metrics for session {session} (it may still be running)"
        )
        raise typer.Exit(1)
    data.pop("points", None)
    if as_json:
        typer.echo(json.dumps(data, indent=2))
        return
    console.print(
        f"session {data['session']} ({data.get('mode')}, {data.get('status')}): {data['wall_ms'] / 1000:.1f}s wall, peak {data['memory_peak_mb']} MB"
    )
    stages = data["stages"]
    for name, stats in stages["model"].items():
        console.print(
            f"  model {name}: {stats['count']} calls, {stats['total_ms'] / 1000:.1f}s (p50 {stats['p50_ms']:.0f} ms, max {stats['max_ms']:.0f} ms)",
            markup=False,
        )
    for label in ("commands", "tests"):
        stats = stages[label]
        if stats["count"]:
            console.print(
                f"  {label}: {stats['count']}, {stats['total_ms'] / 1000:.1f}s (p50 {stats['p50_ms']:.0f} ms)",
                markup=False,
            )
    tokens, context, tools = data["tokens"], data["context"], data["tool_calls"]
    console.print(
        f"  tokens: {tokens['in']} in / {tokens['out']} out · context: {context['items']} items, {context['chars']} chars"
    )
    console.print(
        f"  tool calls: {tools['commands']} commands ({tools['refused']} refused), {tools['file_reads']} reads, {tools['patches']} patches, {tools['test_runs']} test runs"
    )
    console.print(f"  steps: {data['steps']} · repairs: {data['repairs']}")
