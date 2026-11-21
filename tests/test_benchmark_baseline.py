"""Baselines (#21): what a benchmark run came to is kept, and a later version is compared with it."""

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from dfm.benchmark import TASKS, RunResult, baseline, run_suite
from dfm.cli.app import app

COMMITTED = Path(__file__).parent / "baselines" / "reference.json"


def run(
    task: str, outcome: str, *, steps: int = 3, tokens: int = 100, ablation: str = "coder"
) -> RunResult:
    return RunResult(
        task, ablation, 1, "pass", outcome=outcome, steps=steps, tokens_out=tokens, wall_ms=2000
    )


def make(*results: RunResult) -> dict[str, Any]:
    return baseline.make(list(results), {"dfm_version": "x"})


def test_cells_summarize_outcomes_and_costs() -> None:
    runs = [run("t", "success", steps=2), run("t", "partial", steps=4), run("u", "failed")]
    skipped = RunResult("s", "coder", 1, "skipped")  # never reached the check: not in a baseline
    cells = baseline.cells([*runs, skipped])
    assert set(cells) == {"t|coder", "u|coder"}
    assert cells["t|coder"]["worst"] == "partial" and cells["t|coder"]["steps"] == 3.0
    assert cells["t|coder"]["outcomes"] == {"success": 1, "partial": 1}


def test_a_worse_outcome_is_a_regression_and_a_better_one_is_not() -> None:
    before = make(run("a", "success"), run("b", "partial"), run("c", "success"))
    after = make(run("a", "partial"), run("b", "success"), run("c", "success"))
    result = baseline.compare(before, after)
    assert result.regressions == ["a|coder: success -> partial"] and not result.ok
    assert result.improvements == ["b|coder: partial -> success"]
    assert "regressions:" in result.report()


def test_unverified_blocked_and_failed_rank_below_success() -> None:
    for worse in ("unverified", "partial", "blocked", "failed"):
        assert not baseline.compare(make(run("a", "success")), make(run("a", worse))).ok, worse
    assert baseline.compare(make(run("a", "failed")), make(run("a", "failed"))).ok


def test_a_violation_is_a_regression_even_if_the_baseline_had_one() -> None:
    both = make(run("a", "violation"))
    assert baseline.compare(both, both).regressions == ["a|coder: violation (always a regression)"]
    new = baseline.compare(make(run("a", "success")), both)
    assert new.regressions == ["a|coder: violation (always a regression)"]  # reported once


def test_cost_growth_is_drift_not_a_regression() -> None:
    result = baseline.compare(
        make(run("a", "success", steps=4, tokens=1000)),
        make(run("a", "success", steps=12, tokens=5000)),
    )
    assert result.ok and len(result.drift) == 2
    small = baseline.compare(
        make(run("a", "success", steps=4, tokens=1000)),
        make(run("a", "success", steps=5, tokens=1100)),
    )
    assert small.drift == []


def test_cells_missing_on_either_side_are_noted_not_failed() -> None:
    result = baseline.compare(
        make(run("a", "success"), run("b", "success")),
        make(run("a", "success"), run("c", "success")),
    )
    assert result.ok
    assert "b|coder: not run here (skipped or no coder)" in result.not_compared
    assert "c|coder: not in the baseline" in result.not_compared


def test_save_load_round_trip_and_rejects_other_files(tmp_path: Path) -> None:
    data = make(run("a", "success"))
    path = baseline.save(data, tmp_path / "nested" / "b.json")
    assert baseline.load(path) == data
    (tmp_path / "other.json").write_text(json.dumps({"results": []}))
    with pytest.raises(ValueError, match="not a benchmark baseline"):
        baseline.load(tmp_path / "other.json")


def test_environment_names_the_version_the_machine_and_the_model(tmp_path: Path) -> None:
    model = tmp_path / "Qwen-test-4bit"
    model.mkdir()
    (model / "config.json").write_text(
        json.dumps({"model_type": "qwen3", "quantization": {"bits": 4, "group_size": 64}})
    )
    env = baseline.environment(model, repeat=2)
    assert env["dfm_version"] and env["python"] and env["repeat"] == 2
    assert env["model"]["name"] == "Qwen-test-4bit" and env["model"]["path"] == str(model)
    assert baseline.environment(None)["model"] is None


def test_the_committed_reference_baseline_is_well_formed() -> None:
    data = baseline.load(COMMITTED)
    assert data["meta"]["reference"] is True and data["meta"]["dfm_version"]
    assert data["cells"] and all(c["worst"] == "success" for c in data["cells"].values())


async def test_this_version_still_meets_the_committed_reference_baseline(
    home: Path, tmp_path: Path
) -> None:
    """Every fixture that needs no extra toolchain is solved by its reference solution, as in the
    baseline. A change that makes one fail, or leaves its work unverified, shows up here."""
    tasks = [t for t in TASKS if not t.needs]
    results = await run_suite(tasks, ["coder", "full"], workdir=tmp_path, reference=True)
    comparison = baseline.compare(baseline.load(COMMITTED), baseline.make(results, {}))
    assert comparison.ok, comparison.report()
    assert baseline.cells(results), "no fixture ran"


def test_cli_saves_a_baseline_and_compares_with_it(home: Path, tmp_path: Path) -> None:
    path = tmp_path / "b.json"
    args = ["benchmark", "--reference", "--tasks", "python-bug", "--ablation", "coder"]
    saved = CliRunner().invoke(
        app, [*args, "--out", str(tmp_path / "r1"), "--save-baseline", str(path)]
    )
    assert saved.exit_code == 0, saved.output
    assert set(baseline.load(path)["cells"]) == {"python-bug|coder"}
    same = CliRunner().invoke(app, [*args, "--out", str(tmp_path / "r2"), "--compare", str(path)])
    assert same.exit_code == 0 and "no differences" in same.output, same.output

    worse = baseline.load(path)
    worse["cells"]["python-bug|coder"]["worst"] = "success"
    worse["cells"]["python-bug|coder"]["steps"] = 0.1  # the run now takes far more steps: drift
    baseline.save(worse, path)
    drift = CliRunner().invoke(app, [*args, "--out", str(tmp_path / "r3"), "--compare", str(path)])
    assert drift.exit_code == 0 and "drift:" in drift.output, drift.output

    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    refused = CliRunner().invoke(app, [*args, "--out", str(tmp_path / "r4"), "--compare", str(bad)])
    assert refused.exit_code == 2 and "not a benchmark baseline" in refused.output
