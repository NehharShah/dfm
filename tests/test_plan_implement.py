import hashlib
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from dfm.cli.app import app
from dfm.orchestration.implement import ImplementOptions, implement
from dfm.orchestration.plan import load_plan, plan
from dfm.orchestration.runtime import Runtime
from dfm.security.sandbox import already_sandboxed
from tests.helpers import BUGGY, DONE, FIX, ScriptedCoder

Git = Callable[..., str]


@pytest.fixture
def repo(calc_repo: Path) -> Path:
    return calc_repo


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root)
        if rel.parts[0] in {".git", ".dfm"}:
            continue
        h.update(str(rel).encode() + b"\0" + path.read_bytes())
    return h.hexdigest()


@pytest.mark.skipif(already_sandboxed(), reason="inside a sandbox: a read-only one cannot be added")
async def test_plan_writes_nothing_and_names_the_failure(home: Path, repo: Path) -> None:
    before = tree_hash(repo)
    rt = Runtime.create(repo, request="fix the failing test", mode="plan")
    result = await plan(rt, "fix the failing test")
    await rt.close("completed")
    assert tree_hash(repo) == before
    assert not (repo / ".pytest_cache").exists()  # tests ran, but the repo was read-only
    assert any("test_add" in f for f in result.failing_tests)
    assert "tests/test_ops.py" in result.likely_files
    assert any("test command: pytest" in f for f in result.repository_facts)
    assert result.drafted_by == "deterministic"
    assert result.steps and "test_add" in result.steps[0]
    text = result.render()
    for heading in (
        "Goal",
        "Repository facts",
        "Affected package",
        "Affected modules",
        "Relevant historical practice",
        "Likely files",
        "Implementation steps",
        "Verification plan",
        "Risks",
        "Open questions",
    ):
        assert f"## {heading}" in text
    loaded = load_plan(rt, rt.session.id)
    assert loaded is not None and loaded.likely_files == result.likely_files


async def test_plan_uses_model_draft_but_keeps_facts(home: Path, repo: Path) -> None:
    draft = {
        "likely_files": ["src/calc/ops.py", "src/made_up.py"],
        "steps": ["Change the subtraction to an addition in add()"],
        "verification": ["Run the calc tests"],
        "risks": ["None"],
        "open_questions": [],
    }
    rt = Runtime.create(repo, request="fix the failing test", mode="plan")
    result = await plan(rt, "fix the failing test", coder=ScriptedCoder(draft))
    await rt.close("completed")
    assert result.drafted_by == "scripted-coder"
    assert result.steps == ["Change the subtraction to an addition in add()"]
    assert "src/calc/ops.py" in result.likely_files and "src/made_up.py" not in result.likely_files
    assert any("pytest" in v for v in result.verification)


async def test_implement_from_plan_unlocks_planned_dirty_file(home: Path, repo: Path) -> None:
    (repo / "src/calc/ops.py").write_text(BUGGY + "# developer note\n")
    draft = {
        "likely_files": ["src/calc/ops.py"],
        "steps": ["fix add"],
        "verification": [],
        "risks": [],
        "open_questions": [],
    }
    planner = Runtime.create(repo, request="fix", mode="plan")
    await plan(planner, "fix the failing test", coder=ScriptedCoder(draft))
    await planner.close("completed")

    blocked = Runtime.create(repo, request="fix", mode="implement")
    without = await implement(
        blocked,
        "fix the failing test",
        ImplementOptions(coder=ScriptedCoder(FIX, DONE), max_steps=3),
    )
    await blocked.close("failed")
    assert without.outcome.status == "failed"

    rt = Runtime.create(repo, request="fix", mode="implement")
    result = await implement(
        rt,
        "fix the failing test",
        ImplementOptions(from_plan=planner.session.id, coder=ScriptedCoder(FIX, DONE)),
    )
    await rt.close(result.outcome.session_status)
    assert result.outcome.status == "completed"
    assert (
        repo / "src/calc/ops.py"
    ).read_text() == "def add(a, b):\n    return a + b\n# developer note\n"


async def test_plan_without_a_read_only_sandbox_does_not_run_tests(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("dfm.security.sandbox.available", lambda: False)
    before = tree_hash(repo)
    rt = Runtime.create(repo, request="fix the failing test", mode="plan")
    result = await plan(rt, "fix the failing test")
    await rt.close("completed")
    assert tree_hash(repo) == before and not (repo / ".pytest_cache").exists()
    assert result.failing_tests == []  # not run, rather than run with write access


async def test_implement_in_worktree_returns_patch(home: Path, repo: Path, git: Git) -> None:
    rt = Runtime.create(repo, request="fix", mode="implement")
    result = await implement(
        rt, "fix the failing test", ImplementOptions(worktree=True, coder=ScriptedCoder(FIX, DONE))
    )
    await rt.close(result.outcome.session_status)
    assert result.outcome.status == "completed"
    assert (repo / "src/calc/ops.py").read_text() == BUGGY  # the developer's tree is untouched
    assert result.patch_path is not None and "+    return a + b" in result.patch_path.read_text()
    assert git(repo, "worktree", "list").count("\n") == 1  # DFM's worktree was removed
    applied = subprocess.run(
        ["git", "-C", str(repo), "apply", "--check", str(result.patch_path)],
        capture_output=True,
        text=True,
    )
    assert applied.returncode == 0, applied.stderr


async def test_worktree_run_keeps_offline_and_the_approver(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dfm.config import ConfigOverrides
    from dfm.policy import NonInteractiveApprover
    from dfm.policy.network import NetworkMode

    created: list[Runtime] = []
    real_create = Runtime.create

    def spy(*args: Any, **kwargs: Any) -> Runtime:
        made = real_create(*args, **kwargs)
        created.append(made)
        return made

    monkeypatch.setattr(Runtime, "create", spy)
    approver = NonInteractiveApprover()
    rt = Runtime.create(
        repo,
        request="fix",
        mode="implement",
        overrides=ConfigOverrides(offline=True),
        approver=approver,
    )
    result = await implement(
        rt, "fix the failing test", ImplementOptions(worktree=True, coder=ScriptedCoder(FIX, DONE))
    )
    await rt.close(result.outcome.session_status)
    inner = created[-1]
    assert inner is not rt and inner.env.network is NetworkMode.OFFLINE
    assert inner.approver is approver


def test_cli_plan_and_implement_without_models(home: Path, repo: Path) -> None:
    runner = CliRunner()
    planned = runner.invoke(app, ["plan", "fix the failing test", "--repo", str(repo), "--json"])
    assert planned.exit_code == 0, planned.output
    payload = json.loads(planned.stdout)
    assert payload["session"]
    # tests run only where the sandbox can keep the repo read-only (not when already inside one)
    assert bool(payload["failing_tests"]) is not already_sandboxed()
    done = runner.invoke(app, ["implement", "fix the failing test", "--repo", str(repo), "--json"])
    assert done.exit_code == 1
    out = json.loads(done.stdout)
    assert out["status"] == "failed" and "dfm setup" in out["summary"]


@pytest.mark.parametrize("missing", ["nope-session"])
async def test_from_plan_missing(home: Path, repo: Path, missing: str) -> None:
    from dfm.tools.errors import ToolError

    rt = Runtime.create(repo, request="x", mode="implement")
    with pytest.raises((ToolError, FileNotFoundError, ValueError)):
        await implement(rt, "x", ImplementOptions(from_plan=missing, coder=ScriptedCoder(DONE)))
    await rt.close("failed")


async def test_read_only_mode_reports_missing_packages_without_installing(
    home: Path, repo: Path
) -> None:
    from dfm.orchestration.agent import AgentLoop

    rt = Runtime.create(repo, request="plan", mode="plan")
    loop = AgentLoop(rt, read_only=True, services=False)
    ran: list[object] = []

    async def no_run(*args: object, **kwargs: object) -> object:
        ran.append(args)
        raise AssertionError("read-only mode must not run an install")

    loop.dfm_runner.run = no_run  # type: ignore[method-assign]
    installed = await loop.handle_missing_packages(
        "ModuleNotFoundError: No module named 'requests'"
    )
    await rt.close("completed")
    assert installed is False and ran == []
    assert loop.services is None
    assert any("read-only mode" in m for m in loop.missing)
