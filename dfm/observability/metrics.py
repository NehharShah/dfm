"""Per-session metrics.

`metric(name, value, **labels)` records into the current session's sink (a context
variable the Runtime sets); outside a session it is a no-op. When a session closes, its
trace and the recorded metrics are summarised into `<data>/metrics/<session>.json`:
latency per stage, model tokens, context size, tool calls, repair attempts and the
process memory peak. Everything stays on this machine: DFM has no remote telemetry
implementation at all, and no setting that would turn one on.
"""

from __future__ import annotations

import json
import resource
import sys
import time
from collections import defaultdict
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast


@dataclass
class MetricsSink:
    session_id: str
    started: float = field(default_factory=time.monotonic)
    points: list[dict[str, Any]] = field(default_factory=lambda: [])


_SINK: ContextVar[MetricsSink | None] = ContextVar("dfm_metrics", default=None)


def metric(name: str, value: float, **labels: Any) -> None:
    sink = _SINK.get()
    if sink is not None:
        sink.points.append(
            {
                "name": name,
                "value": float(value),
                "t": round(time.monotonic() - sink.started, 3),
                **labels,
            }
        )


def start(session_id: str) -> MetricsSink:
    """Begin collecting for a session in the current context (the Runtime's lifetime)."""
    sink = MetricsSink(session_id)
    _SINK.set(sink)
    return sink


def memory_peak_mb() -> float:
    """Peak resident memory of DFM and its finished children (ru_maxrss: bytes on macOS)."""
    scale = 1 if sys.platform == "darwin" else 1024
    own = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
    kids = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * scale
    return round(max(own, kids) / (1024 * 1024), 1)


def _stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {
        "count": len(values),
        "total_ms": round(sum(values), 1),
        "p50_ms": round(ordered[len(ordered) // 2], 1),
        "max_ms": round(ordered[-1], 1),
    }


def summarize(
    header: dict[str, Any], events: list[dict[str, Any]], sink: MetricsSink | None, wall_ms: float
) -> dict[str, Any]:
    """Session metrics from its trace (and explicit metric() points)."""
    model_ms: dict[str, list[float]] = defaultdict(list)
    command_ms: list[float] = []
    test_ms: list[float] = []
    counts: dict[str, int] = defaultdict(int)
    context_items = context_chars = 0
    test_started: list[int] = []
    for event in events:
        kind = str(event.get("type"))
        counts[kind] += 1
        if kind == "model_finished" and "latency_ms" in event:
            model_ms[str(event.get("model", "?"))].append(float(event["latency_ms"]))
        elif kind == "command_finished":
            if event.get("refused"):
                counts["refused"] += 1
            elif "duration_ms" in event:
                command_ms.append(float(event["duration_ms"]))
        elif kind == "test_started":
            test_started.append(len(command_ms))
        elif kind == "test_finished" and test_started:
            index = test_started.pop()
            test_ms.extend(command_ms[index:])
        elif kind == "context_selected":
            items = [
                cast(dict[str, Any], i)
                for i in cast(list[Any], event.get("items") or [])
                if isinstance(i, dict)
            ]
            context_items += len(items)
            context_chars += sum(
                int(i.get("chars", 0) or len(str(i.get("content", "")))) for i in items
            )
    points = sink.points if sink else []
    tokens_in = sum(p["value"] for p in points if p["name"] == "model.tokens_in")
    tokens_out = sum(p["value"] for p in points if p["name"] == "model.tokens_out")
    explicit: dict[str, float] = defaultdict(float)
    for p in points:
        if not p["name"].startswith("model.tokens"):
            explicit[p["name"]] += p["value"]
    return {
        "session": header.get("session_id") or header.get("id"),
        "mode": header.get("mode"),
        "repo": header.get("repo_name"),
        "wall_ms": round(wall_ms, 1),
        "memory_peak_mb": memory_peak_mb(),
        "stages": {
            "model": {name: _stats(values) for name, values in model_ms.items()},
            "commands": _stats(command_ms),
            "tests": _stats(test_ms),
        },
        "tokens": {"in": int(tokens_in), "out": int(tokens_out)},
        "context": {
            "selections": counts["context_selected"],
            "items": context_items,
            "chars": context_chars,
        },
        "tool_calls": {
            "commands": counts["command_started"],
            "refused": counts["refused"],
            "file_reads": counts["file_read"],
            "patches": counts["patch_finished"],
            "test_runs": counts["test_finished"],
        },
        "repairs": int(explicit.pop("agent.repairs", 0)),
        "steps": int(explicit.pop("agent.steps", 0)),
        "metrics": dict(explicit),
    }


def write(metrics_dir: Path, summary: dict[str, Any], sink: MetricsSink | None) -> Path:
    metrics_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = metrics_dir / f"{summary['session']}.json"
    payload = {**summary, "points": (sink.points if sink else [])[-2000:]}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1, default=str))
    tmp.replace(path)
    return path


def load(metrics_dir: Path, session_id: str) -> dict[str, Any] | None:
    try:
        return json.loads((metrics_dir / f"{session_id}.json").read_text())
    except (OSError, ValueError):
        return None
