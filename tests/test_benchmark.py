import json
import os
import pwd
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from dfm.benchmark import (
    ABLATIONS,
    BY_ID,
    TASKS,
    ReferenceCoder,
    run_suite,
    run_task,
    table,
    write_results,
)
from dfm.benchmark.tasks import DONE, create
from dfm.cli.app import app
from dfm.config import ConfigOverrides, load_config
from dfm.models.base import GenerateRequest, GenerateResult
from dfm.orchestration.runtime import Runtime

AVAILABLE = [t for t in TASKS if all(shutil.which(tool) for tool in t.needs)]


@pytest.fixture(autouse=True)
def toolchains(monkeypatch: pytest.MonkeyPatch) -> None:
    """`home` swaps HOME; rustup and cargo still need the real toolchain directories."""
    real = Path(pwd.getpwuid(os.getuid()).pw_dir)
    monkeypatch.setenv("CARGO_HOME", os.environ.get("CARGO_HOME", str(real / ".cargo")))
    monkeypatch.setenv("RUSTUP_HOME", os.environ.get("RUSTUP_HOME", str(real / ".rustup")))


@pytest.mark.parametrize("task", AVAILABLE, ids=lambda t: t.id)
async def test_every_fixture_fails_first_and_its_reference_passes(
    home: Path, tmp_path: Path, task
) -> None:  # type: ignore[no-untyped-def]
    result = await run_task(task, "coder", workdir=tmp_path, coder=ReferenceCoder(task.reference))
    assert result.status == "pass", (result.detail, result.dfm_status)
    assert result.expected_touched and result.unnecessary_lines == 0, result.changed_files


def test_ablations_switch_integrations_off(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path)}
    coder = load_config(
        None, ConfigOverrides(offline=True, settings=ABLATIONS["coder"]), env=env
    ).config
    assert not (
        coder.integrations.repocanon.enabled
        or coder.integrations.repokg.enabled
        or coder.integrations.reflex.enabled
    )
    assert coder.models.apple.enabled is False and coder.network.github == "deny"  # offline
    kg = load_config(None, ConfigOverrides(settings=ABLATIONS["+kg"]), env=env).config
    assert (
        kg.integrations.repocanon.enabled
        and kg.integrations.repokg.enabled
        and not kg.integrations.reflex.enabled
    )


async def test_scoring_unnecessary_edits_and_failures(home: Path, tmp_path: Path) -> None:
    task = BY_ID["python-bug"]
    sloppy = ReferenceCoder([create("NOTES.md", "thoughts\nmore\n"), *task.reference])
    noisy = await run_task(task, "coder", workdir=tmp_path / "a", coder=sloppy)
    assert (
        noisy.status == "pass"
        and noisy.unnecessary_lines == 2
        and "NOTES.md" in noisy.changed_files
    )
    quitter = ReferenceCoder([{"action": "give_up", "reason": "no idea"}])
    failed = await run_task(task, "coder", workdir=tmp_path / "b", coder=quitter)
    assert failed.status == "fail" and failed.dfm_status == "failed" and not failed.expected_touched
    broken = replace(task, check=("true",))  # a check that already passes measures nothing
    assert (
        await run_task(broken, "coder", workdir=tmp_path / "c", coder=sloppy)
    ).status == "invalid"
    missing = replace(task, needs=("no-such-tool-xyz",))
    assert (
        await run_task(missing, "coder", workdir=tmp_path / "d", coder=sloppy)
    ).status == "skipped"


async def test_table_and_results_files(home: Path, tmp_path: Path) -> None:
    task = BY_ID["python-bug"]
    runs = [
        await run_task(
            task,
            ablation,
            workdir=tmp_path / ablation.lstrip("+"),
            coder=ReferenceCoder(task.reference),
        )
        for ablation in ("coder", "+canon")
    ]
    md = table(runs)
    assert md.splitlines()[0] == "| task | coder | +canon |"
    assert "| python-bug | ✓ 1/1" in md and "| **success** | 1/1 | 1/1 |" in md
    json_path, md_path = write_results(runs, tmp_path / "out", {"reference": True})
    data = json.loads(json_path.read_text())
    assert (
        data["meta"]["reference"]
        and len(data["results"]) == 2
        and md_path.read_text().startswith("| task |")
    )
    assert runs[0].wall_ms > 0 and runs[0].steps == 2 and runs[0].memory_peak_mb > 0


def test_cli_reference_run(home: Path, tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "--tasks",
            "python-bug,security-regression",
            "--reference",
            "--out",
            str(tmp_path / "out"),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert [(r["task"], r["ablation"], r["status"]) for r in rows] == [
        ("python-bug", "coder", "pass"),
        ("python-bug", "full", "pass"),
        ("security-regression", "coder", "pass"),
        ("security-regression", "full", "pass"),
    ]
    assert (tmp_path / "out" / "results.md").exists()
    bad = CliRunner().invoke(app, ["benchmark", "--tasks", "nope"])
    assert bad.exit_code == 2
    assert DONE["action"] == "done"


def test_cli_model_pins_both_coder_slots(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    async def fake_suite(*args: Any, **kw: Any) -> list[Any]:
        seen.update(kw["settings"])
        return []

    monkeypatch.setattr("dfm.cli.benchmark_cmd.run_suite", fake_suite)
    model = tmp_path / "model"
    model.mkdir()
    args = ["benchmark", "--tasks", "python-bug", "--model", str(model), "--out", str(tmp_path)]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert seen == {"models.primary.model": str(model), "models.fallback.model": str(model)}


async def test_pinned_model_serves_small_coder_routes(home: Path, tmp_path: Path) -> None:
    model = tmp_path / "model"
    model.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    pinned = {"models.primary.model": str(model), "models.fallback.model": str(model)}
    overrides = ConfigOverrides(offline=True, settings=pinned)
    rt = Runtime.create(repo, request="fix it", mode="implement", overrides=overrides)
    try:  # same weights → one instance, so a small-coder route still gets this model
        assert rt.pool.small is None and rt.pool.primary is not None
        assert rt.pool.coder("small").name == "coder-small"
    finally:
        await rt.close("completed")


async def test_a_check_that_cannot_run_is_skipped(home: Path, tmp_path: Path) -> None:
    task = replace(BY_ID["python-bug"], check=("definitely-not-a-binary-xyz",))
    result = await run_task(task, "coder", workdir=tmp_path, coder=ReferenceCoder(task.reference))
    assert result.status == "skipped" and "cannot run" in result.detail


class UnusableCoder(ReferenceCoder):
    """Answers (with token counts, like a real model) but never with a usable action."""

    name = "unusable"

    async def generate(self, request: GenerateRequest) -> GenerateResult:
        return GenerateResult("", self.name, 0, tokens_in=50, tokens_out=3)


async def test_a_model_with_unusable_output_fails(home: Path, tmp_path: Path) -> None:
    result = await run_task(BY_ID["python-bug"], "coder", workdir=tmp_path, coder=UnusableCoder([]))
    # it ran, so this measures the model (a fail), not the machine (an error)
    assert result.status == "fail" and result.models == ["unusable"]
    assert "no JSON object" in result.detail


async def test_cooldown_only_after_runs_that_used_a_model(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("dfm.benchmark.runner.asyncio.sleep", fake_sleep)
    task = BY_ID["python-bug"]
    await run_suite(
        [task], ["coder"], workdir=tmp_path / "a", coder=UnusableCoder([]), cooldown_s=9
    )
    assert slept == [9]
    await run_suite([task], ["coder"], workdir=tmp_path / "b", reference=True, cooldown_s=9)
    assert slept == [9]  # the reference replay uses no model: nothing to cool down


async def test_no_coder_is_an_error_not_a_task_failure(home: Path, tmp_path: Path) -> None:
    task = BY_ID["python-bug"]
    result = await run_task(task, "coder", workdir=tmp_path)  # no model configured in `home`
    assert result.status == "error" and "coding model" in result.detail
    assert "err" in table([result]) and "| **success** | - |" in table([result])
