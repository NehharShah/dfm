"""`dfm inspect-log FILE`: streaming summaries of large logs and JSONL."""

import json
import re
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from dfm.execution.environment import DEFAULT_DENIED_HOME_PATHS
from dfm.tools.logs import Query, emit_line, inspect, parse_fields, parse_window, tail_lines

console = Console()


def inspect_log(
    file: Annotated[
        Path, typer.Argument(help="Log or JSONL file (any size).", exists=True, dir_okay=False)
    ],
    head: Annotated[int, typer.Option("--head", help="First N matching lines.")] = 0,
    tail: Annotated[int, typer.Option("--tail", help="Last N (matching) lines.")] = 0,
    grep: Annotated[
        str | None, typer.Option("--grep", help="Only lines matching this regex.")
    ] = None,
    fields: Annotated[
        list[str] | None,
        typer.Option(
            "--field", "--jq-like", help="JSONL filter FIELD=VALUE (dotted paths); repeatable."
        ),
    ] = None,
    window: Annotated[
        str | None, typer.Option("--window", help="Time window START..END (ISO).")
    ] = None,
    sample: Annotated[
        int, typer.Option("--sample", help="N random matching lines (reservoir).")
    ] = 0,
    cluster: Annotated[
        bool, typer.Option("--cluster/--no-cluster", help="Group errors by template.")
    ] = True,
    stacks: Annotated[
        bool, typer.Option("--stacks/--no-stacks", help="Group stack traces.")
    ] = True,
    top: Annotated[int, typer.Option("--top", help="Clusters and stacks to show.")] = 10,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Head, tail, grep, field filters, time windows, sampling, error clusters and stack
    groups, in one streaming pass with bounded memory. Every printed line is redacted."""
    real = file.resolve()
    home = Path.home().resolve()
    if any(real.is_relative_to(home / denied) for denied in DEFAULT_DENIED_HOME_PATHS):
        console.print(f"[red]error:[/red] {file} is in a protected location")
        raise typer.Exit(2)
    since, until = parse_window(window) if window else (None, None)
    try:
        query = Query(re.compile(grep) if grep else None, parse_fields(fields or []), since, until)
    except (re.error, ValueError) as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2) from exc
    fast_tail = tail and not query.active and not head and not sample
    report = inspect(
        real,
        query,
        head=head,
        sample=sample,
        cluster=cluster,
        stacks=stacks,
        keep_tail=0 if fast_tail else tail,
    )
    if fast_tail:
        report.tail.extend((0, line) for line in tail_lines(real, tail))
    if as_json:
        payload = {
            "file": str(file),
            "size": report.size,
            "lines": report.lines,
            "matched": report.matched,
            "levels": dict(report.levels),
            "first_ts": report.first_ts.isoformat() if report.first_ts else None,
            "last_ts": report.last_ts.isoformat() if report.last_ts else None,
            "clusters": [
                {"template": emit_line(c.template), "count": c.count, "first_line": c.first_line}
                for c in sorted(report.clusters.values(), key=lambda c: -c.count)[:top]
            ],
            "stacks": [
                {"signature": emit_line(s.template), "count": s.count, "first_line": s.first_line}
                for s in sorted(report.stacks.values(), key=lambda s: -s.count)[:top]
            ],
            "lines_out": [
                {"line": n, "text": emit_line(t)} for n, t in [*report.selected, *report.tail]
            ],
        }
        typer.echo(json.dumps(payload, indent=2))
        return
    console.print(report.render(top=top), markup=False, highlight=False)
