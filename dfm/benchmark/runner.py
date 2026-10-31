"""Run benchmark tasks under ablations and report.

    for each task, ablation and repeat:
      fresh git copy of the fixture → its check must FAIL first (else the task is invalid)
      → DFM runs offline with the ablation's integrations switched off
      → the task's own check runs again, sandboxed (the model's edits are untrusted code)
      → score: success = check passes; unnecessary edits = changed lines outside the
        expected files; plus DFM's session metrics (latency, tokens, context, memory, tools)

Results are JSON plus a markdown comparison table. Nothing uses the network.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from dfm.benchmark.tasks import TASKS, BenchTask
from dfm.config import ConfigOverrides, DFMConfig, state_dirs
from dfm.execution import ExecutionEnvironment
from dfm.models.base import GenerateRequest, GenerateResult, Model, ModelHealth
from dfm.observability import load
from dfm.orchestration.ci import ci
from dfm.orchestration.implement import ImplementOptions, implement
from dfm.orchestration.runtime import Runtime
from dfm.policy import Policy
from dfm.processes import ProcessManager, ProcessRegistry
from dfm.security.sandbox import Sandbox
from dfm.tools.errors import ToolError
from dfm.tools.shell.runner import CommandRunner

_OFF = {
    "integrations.repocanon.enabled": False,
    "integrations.repokg.enabled": False,
    "integrations.reflex.enabled": False,
    "models.apple.enabled": False,
}
ABLATIONS: dict[str, dict[str, Any]] = {
    "coder": dict(_OFF),
    "+canon": {**_OFF, "integrations.repocanon.enabled": True},
    "+kg": {**_OFF, "integrations.repocanon.enabled": True, "integrations.repokg.enabled": True},
    "+reflex": {
        **_OFF,
        "integrations.repocanon.enabled": True,
        "integrations.repokg.enabled": True,
        "integrations.reflex.enabled": True,
    },
    "+apple": {},  # every integration on (as configured), Apple FM routing included
    "full": {},  # the developer's configuration as it is
}


_NO_CODER = ("coding model failed", "no coding model")
_CANNOT_RUN = (
    "command not found",
    "could not choose a version",
    "No such file or directory: '",
    "check refused:",
)


class ReferenceCoder:
    """Replays a task's reference solution: tests the harness and the fixtures, not a model."""

    name = "reference"

    def __init__(self, actions: Sequence[Mapping[str, Any]]) -> None:
        self.actions = [dict(a) for a in actions]

    async def healthcheck(self) -> ModelHealth:
        return ModelHealth(self.name, True, "replay")

    async def generate(self, request: GenerateRequest) -> GenerateResult:
        step = self.actions.pop(0) if len(self.actions) > 1 else self.actions[0]
        return GenerateResult(json.dumps(step), self.name, 0)

    async def stream(self, request: GenerateRequest) -> AsyncIterator[str]:
        yield ""


@dataclass
class RunResult:
    task: str
    ablation: str
    repeat: int
    status: str  # pass | fail | skipped | invalid | error
    detail: str = ""
    dfm_status: str | None = None
    changed_files: list[str] = field(default_factory=lambda: [])
    expected_touched: bool = False
    unnecessary_lines: int = 0
    wall_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    context_chars: int = 0
    memory_peak_mb: float = 0.0
    tool_calls: int = 0
    repairs: int = 0
    steps: int = 0
    models: list[str] = field(default_factory=lambda: [])  # models that actually answered
    seed: int = 0  # sampling seed: rerun a cell with the same seed to replay it


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-c",
            "user.email=bench@dfm.invalid",
            "-c",
            "user.name=DFM bench",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout


def run_seed(task: str, ablation: str, repeat: int) -> int:
    """A stable seed per cell, so the same command samples the same way."""
    return int.from_bytes(hashlib.sha256(f"{task}/{ablation}/{repeat}".encode()).digest()[:4])


def materialize(task: BenchTask, root: Path) -> Path:
    repo = root / "repo"
    if repo.exists():
        shutil.rmtree(repo)
    for rel, content in task.files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    if task.setup is not None:
        task.setup(repo)
    # build outputs and lockfiles toolchains create on first run are not the model's edits
    (repo / ".gitignore").write_text(
        "target/\nCargo.lock\n.pytest_cache/\n__pycache__/\nnode_modules/\nout/\ncache/\n"
    )
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    return repo


async def run_check(repo: Path, argv: Sequence[str], scratch: Path) -> tuple[bool, str]:
    """The task's own check, sandboxed and offline, exactly like DFM runs tests."""
    env = ExecutionEnvironment.create(
        repo,
        DFMConfig(),
        session_id=f"check-{int(time.time() * 1000)}",
        overrides=ConfigOverrides(offline=True),
    )
    runner = CommandRunner(
        env,
        ProcessManager(ProcessRegistry(scratch / "procs")),
        Policy(DFMConfig()),
        wrap=Sandbox().wrap,
        proposed_by="benchmark",
    )
    try:
        outcome = await runner.run(list(argv), timeout=900)
    finally:
        env.close()
    if outcome.result is None:
        return False, f"check refused: {outcome.verdicts[-1].reason}"
    return outcome.result.exit_code == 0, (outcome.result.stdout + outcome.result.stderr)[-1500:]


def score_diff(repo: Path, expected: frozenset[str]) -> tuple[list[str], int, bool]:
    """(changed files, changed lines outside `expected`, whether an expected file changed)."""
    _git(repo, "add", "-A", "--intent-to-add")
    changed: list[str] = []
    outside = 0
    for line in _git(repo, "diff", "--numstat", "HEAD").splitlines():
        added, removed, path = line.split("\t", 2)
        changed.append(path)
        if path not in expected:
            outside += (int(added) if added.isdigit() else 0) + (
                int(removed) if removed.isdigit() else 0
            )
    return changed, outside, bool(expected & set(changed))


async def run_task(
    task: BenchTask,
    ablation: str,
    *,
    workdir: Path,
    coder: Model | None = None,
    repeat: int = 1,
    max_steps: int = 20,
    settings: Mapping[str, Any] | None = None,
) -> RunResult:
    result = RunResult(task.id, ablation, repeat, "error")
    missing = [tool for tool in task.needs if shutil.which(tool) is None]
    if missing:
        result.status, result.detail = "skipped", f"needs {', '.join(missing)}"
        return result
    root = workdir / f"{task.id}-{ablation.lstrip('+')}-{repeat}"
    root.mkdir(parents=True, exist_ok=True)
    repo = materialize(task, root)
    passed, baseline = await run_check(repo, task.check, root)
    if passed:
        result.status, result.detail = "invalid", "the check passes before any change"
        return result
    if any(sign in baseline for sign in _CANNOT_RUN):
        # failing because the toolchain cannot run is not the task's failure: nothing to measure
        result.status, result.detail = (
            "skipped",
            f"check cannot run here: {baseline.strip()[-160:]}",
        )
        return result
    result.seed = run_seed(task.id, ablation, repeat)
    overrides = ConfigOverrides(
        offline=True,
        settings={**(settings or {}), **ABLATIONS[ablation], "models.seed": result.seed},
    )
    rt = Runtime.create(
        repo,
        request=task.request,
        mode="ci" if task.kind == "ci" else "implement",
        overrides=overrides,
    )
    try:
        if task.kind == "ci":
            log = root / "ci.log"
            log.write_text(task.ci_log or "")
            outcome = (await ci(rt, log_path=log, coder=coder, max_steps=max_steps)).outcome
        else:
            outcome = (
                await implement(
                    rt, task.request, ImplementOptions(coder=coder, max_steps=max_steps)
                )
            ).outcome
    except ToolError as exc:
        await rt.close("failed", str(exc))
        result.detail = str(exc)
        return result
    await rt.close(outcome.session_status, outcome.summary)
    result.dfm_status = outcome.status
    metrics = load(rt.dirs.data / "metrics", rt.session.id) or {}
    result.wall_ms = float(metrics.get("wall_ms", 0.0))
    result.tokens_in = int(metrics.get("tokens", {}).get("in", 0))
    result.tokens_out = int(metrics.get("tokens", {}).get("out", 0))
    result.context_chars = int(metrics.get("context", {}).get("chars", 0))
    result.memory_peak_mb = float(metrics.get("memory_peak_mb", 0.0))
    result.tool_calls = int(metrics.get("tool_calls", {}).get("commands", 0))
    result.repairs = int(metrics.get("repairs", 0))
    result.steps = int(metrics.get("steps", 0))
    points = cast(list[dict[str, Any]], metrics.get("points", []))
    result.models = sorted({str(p["model"]) for p in points if p.get("model")})
    result.changed_files, result.unnecessary_lines, result.expected_touched = score_diff(
        repo, task.expected
    )
    passed, tail = await run_check(repo, task.check, root)
    model_failed = outcome.summary.startswith(_NO_CODER)
    if not passed and not result.changed_files and model_failed and not result.models:
        # the coder never ran (no model, not enough memory): that measures the machine, not DFM.
        # A model that answered with unusable output did run: that is a fail.
        result.status, result.detail = "error", outcome.summary[:300]
        return result
    result.status = "pass" if passed else "fail"
    result.detail = "" if passed else (outcome.summary if model_failed else tail)[-300:]
    return result


async def run_suite(
    tasks: Sequence[BenchTask] = TASKS,
    ablations: Sequence[str] = ("coder", "full"),
    *,
    workdir: Path | None = None,
    coder: Model | None = None,
    reference: bool = False,
    repeat: int = 1,
    max_steps: int = 20,
    settings: Mapping[str, Any] | None = None,
    cooldown_s: float = 0.0,
) -> list[RunResult]:
    workdir = workdir or state_dirs().data / "benchmarks" / time.strftime("%Y%m%dT%H%M%S")
    results: list[RunResult] = []
    for task in tasks:
        for ablation in ablations:
            if ablation not in ABLATIONS:
                raise ValueError(f"unknown ablation {ablation!r} (known: {', '.join(ABLATIONS)})")
            for n in range(1, repeat + 1):
                model = ReferenceCoder(task.reference) if reference else coder
                results.append(
                    await run_task(
                        task,
                        ablation,
                        workdir=workdir,
                        coder=model,
                        repeat=n,
                        max_steps=max_steps,
                        settings=settings,
                    )
                )
                if cooldown_s > 0 and results[-1].models:
                    # back-to-back model runs keep the GPU at full load: let the laptop cool
                    await asyncio.sleep(cooldown_s)
    return results


def table(results: Sequence[RunResult]) -> str:
    """A markdown comparison: tasks by ablations, then per-ablation aggregates."""
    ablations = list(dict.fromkeys(r.ablation for r in results))
    tasks = list(dict.fromkeys(r.task for r in results))
    cell: dict[tuple[str, str], list[RunResult]] = {}
    for r in results:
        cell.setdefault((r.task, r.ablation), []).append(r)
    lines = ["| task | " + " | ".join(ablations) + " |", "|---|" + "---|" * len(ablations)]
    for task in tasks:
        row: list[str] = []
        for ablation in ablations:
            runs = cell.get((task, ablation), [])
            if not runs or all(r.status == "skipped" for r in runs):
                row.append("skip")
                continue
            if all(r.status in {"error", "invalid"} for r in runs):
                row.append("err")
                continue
            ok = sum(r.status == "pass" for r in runs)
            secs = sum(r.wall_ms for r in runs) / len(runs) / 1000
            row.append(
                f"{'✓' if ok == len(runs) else '✗' if ok == 0 else '~'} {ok}/{len(runs)} {secs:.0f}s"
            )
        lines.append(f"| {task} | " + " | ".join(row) + " |")
    groups: list[list[RunResult]] = [
        [r for r in results if r.ablation == ablation and r.status in {"pass", "fail"}]
        for ablation in ablations
    ]

    def summary(label: str, fn: Callable[[list[RunResult]], str]) -> str:
        return f"| **{label}** | " + " | ".join(fn(runs) if runs else "-" for runs in groups) + " |"

    lines.append(summary("success", lambda rs: f"{sum(r.status == 'pass' for r in rs)}/{len(rs)}"))
    lines.append(
        summary("mean wall", lambda rs: f"{sum(r.wall_ms for r in rs) / len(rs) / 1000:.1f}s")
    )
    lines.append(
        summary(
            "tokens in/out",
            lambda rs: f"{sum(r.tokens_in for r in rs)}/{sum(r.tokens_out for r in rs)}",
        )
    )
    lines.append(
        summary("context chars", lambda rs: f"{sum(r.context_chars for r in rs) // len(rs)}")
    )
    lines.append(summary("unnecessary lines", lambda rs: str(sum(r.unnecessary_lines for r in rs))))
    lines.append(summary("repairs", lambda rs: str(sum(r.repairs for r in rs))))
    lines.append(summary("peak MB", lambda rs: f"{max(r.memory_peak_mb for r in rs):.0f}"))
    lines.append(
        summary("models", lambda rs: ", ".join(sorted({m for r in rs for m in r.models})) or "-")
    )
    return "\n".join(lines)


def write_results(
    results: Sequence[RunResult], out: Path, meta: Mapping[str, Any] | None = None
) -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    data = {"meta": dict(meta or {}), "results": [asdict(r) for r in results]}
    json_path = out / "results.json"
    json_path.write_text(json.dumps(data, indent=1))
    md_path = out / "results.md"
    md_path.write_text(table(results) + "\n")
    return json_path, md_path
