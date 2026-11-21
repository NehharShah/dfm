"""Baselines: what a benchmark run came to, kept so a later DFM version can be compared with it.

A baseline holds, per task and ablation, the outcomes, how many steps, output tokens, repairs
and memory the runs took, and the environment they ran in (DFM version, machine, model and its
quantization, repeat count, cooldown, seed rule). `compare` reports an outcome that got worse
(a regression: the exit status of `dfm benchmark --compare`) and a cost that grew by half or
more (drift: reported, not failed, because step and token counts move with sampling).
"""

from __future__ import annotations

import json
import platform
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any, cast

from dfm.benchmark.runner import OUTCOMES, RunResult
from dfm.models.mlx.discovery import describe_dir
from dfm.models.resources import ModelResourceManager

SCHEMA = 1
# best first: a lower rank is a worse outcome
_RANK = {name: len(OUTCOMES) - i for i, name in enumerate(OUTCOMES)}
DRIFT_FACTOR = 1.5


def environment(model: Path | None = None, **settings: Any) -> dict[str, Any]:
    """Where and with what the runs happened: enough to tell two baselines are comparable."""
    try:
        version = metadata.version("dfm-agent")
    except metadata.PackageNotFoundError:
        version = "unknown"
    env: dict[str, Any] = {
        "dfm_version": version,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        **settings,
    }
    try:
        hw = ModelResourceManager().hardware()
        env["hardware"] = {"chip": hw.chip, "memory_gb": round(hw.memory_gb), "macos": hw.macos}
    except (OSError, ValueError):
        env["hardware"] = None
    env["model"] = _model_info(model)
    return env


def _model_info(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    info: dict[str, Any] = {"path": str(path), "name": path.name}
    candidate = describe_dir(path)
    if candidate is not None:
        info.update(
            type=candidate.model_type,
            quantization=(
                f"{candidate.quant_bits}-bit {candidate.quant_mode or ''}".strip()
                if candidate.quant_bits
                else "none"
            ),
            size_gb=round(candidate.size_gb, 2),
            max_context=candidate.max_context,
        )
    return info


def cells(results: Sequence[RunResult]) -> dict[str, dict[str, Any]]:
    """Per `task|ablation`: the runs that reached the check, summarized."""
    grouped: dict[str, list[RunResult]] = {}
    for r in results:
        if r.outcome:
            grouped.setdefault(f"{r.task}|{r.ablation}", []).append(r)
    out: dict[str, dict[str, Any]] = {}
    for key, runs in grouped.items():
        counts: dict[str, int] = {}
        for r in runs:
            counts[r.outcome] = counts.get(r.outcome, 0) + 1
        n = len(runs)
        out[key] = {
            "runs": n,
            "outcomes": counts,
            "worst": min((r.outcome for r in runs), key=lambda o: _RANK.get(o, 0)),
            "steps": round(sum(r.steps for r in runs) / n, 1),
            "tokens_out": round(sum(r.tokens_out for r in runs) / n),
            "repairs": round(sum(r.repairs for r in runs) / n, 1),
            "wall_s": round(sum(r.wall_ms for r in runs) / n / 1000, 1),
            "memory_peak_mb": round(max(r.memory_peak_mb for r in runs)),
        }
    return dict(sorted(out.items()))


def make(results: Sequence[RunResult], meta: Mapping[str, Any]) -> dict[str, Any]:
    return {"schema": SCHEMA, "meta": dict(meta), "cells": cells(results)}


def save(baseline: Mapping[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baseline, indent=1, sort_keys=True) + "\n")
    return path


def load(path: Path) -> dict[str, Any]:
    data: Any = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or "cells" not in data:  # pyright: ignore[reportUnknownMemberType]
        raise ValueError(f"{path} is not a benchmark baseline (schema {SCHEMA})")
    return cast(dict[str, Any], data)


@dataclass
class Comparison:
    regressions: list[str] = field(default_factory=lambda: [])
    drift: list[str] = field(default_factory=lambda: [])
    improvements: list[str] = field(default_factory=lambda: [])
    not_compared: list[str] = field(default_factory=lambda: [])

    @property
    def ok(self) -> bool:
        return not self.regressions

    def report(self) -> str:
        lines: list[str] = []
        for title, items in (
            ("regressions", self.regressions),
            ("drift", self.drift),
            ("improvements", self.improvements),
            ("not compared", self.not_compared),
        ):
            if items:
                lines += [f"{title}:", *(f"  {item}" for item in items)]
        return "\n".join(lines) or "no differences"


def compare(baseline: Mapping[str, Any], current: Mapping[str, Any]) -> Comparison:
    result = Comparison()
    base_cells: Mapping[str, Mapping[str, Any]] = baseline["cells"]
    cur_cells: Mapping[str, Mapping[str, Any]] = current["cells"]
    for key, now in cur_cells.items():
        if now["worst"] == "violation":
            result.regressions.append(f"{key}: violation (always a regression)")
    for key, before in base_cells.items():
        now = cur_cells.get(key)
        if now is None:
            result.not_compared.append(f"{key}: not run here (skipped or no coder)")
            continue
        was, is_ = before["worst"], now["worst"]
        if is_ != "violation" and _RANK.get(is_, 0) < _RANK.get(was, 0):
            result.regressions.append(f"{key}: {was} -> {is_}")
        elif _RANK.get(is_, 0) > _RANK.get(was, 0):
            result.improvements.append(f"{key}: {was} -> {is_}")
        for metric, slack in (("steps", 1), ("tokens_out", 200), ("repairs", 1)):
            if now[metric] > before[metric] * DRIFT_FACTOR + slack:
                result.drift.append(f"{key}: {metric} {before[metric]} -> {now[metric]}")
    result.not_compared += [
        f"{key}: not in the baseline" for key in cur_cells if key not in base_cells
    ]
    return result
