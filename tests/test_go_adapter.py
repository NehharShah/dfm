import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from dfm.integrations.repocanon import RepoCanonProvider
from dfm.orchestration.implement import ImplementOptions, implement
from dfm.orchestration.runtime import Runtime
from dfm.processes import ProcessResult
from dfm.tools.filesystem import FsTools, SearchReplace
from dfm.tools.languages import GoAdapter, select_adapter
from dfm.tools.languages.go import module_dir, module_path, workspace_modules
from dfm.tools.results import FailedTest
from dfm.tools.shell.runner import CommandRunner
from tests import repo_fixtures as fx
from tests.helpers import DONE, ScriptedCoder, sandboxed_runner

FIXTURES = Path(__file__).parent / "fixtures"
OUTPUTS = FIXTURES / "test_outputs"
GO = shutil.which("go")
go_only = pytest.mark.skipif(GO is None, reason="go not installed")


def result(stdout: str, code: int = 1, stderr: str = "") -> ProcessResult:
    return ProcessResult(("go",), code, stdout, stderr, 5)


def test_module_and_workspace_detection(tmp_path: Path) -> None:
    repo = fx.write(
        tmp_path / "ws",
        {
            "go.work": "go 1.22\n\nuse (\n\t./api\n\t./tools // dev tools\n)\nuse ./cli\n",
            "api/go.mod": "module example.com/api\n",
            "api/server/server.go": "package server\n",
        },
    )
    assert sorted(workspace_modules(repo)) == ["api", "cli", "tools"]
    assert module_dir(repo, "api/server/server.go") == "api"
    assert module_path(repo / "api") == "example.com/api"
    assert isinstance(select_adapter(repo, None), GoAdapter)
    cmds = GoAdapter().commands(repo, None, "api/server/server.go")
    assert cmds.test is not None and cmds.test.argv == ("go", "test", "-json", "./...")
    assert cmds.test.cwd == "api" and cmds.check is not None and cmds.check.cwd == "api"
    assert cmds.meta == {"module_dir": "api", "module": "example.com/api"}


async def test_repocanon_command_gets_json(tmp_path: Path) -> None:
    repo = fx.go(tmp_path / "g")
    canon = await RepoCanonProvider().analyze(repo)
    cmds = GoAdapter().commands(repo, canon)
    assert cmds.test is not None and cmds.test.argv[:3] == ("go", "test", "-json")
    assert cmds.reporter == "go-json" and cmds.test.source == "repocanon"


def test_targeted_test_runs_one_package_and_subtest() -> None:
    adapter = GoAdapter()
    cmds = adapter.commands(FIXTURES / "go_calc", None)
    t = adapter.targeted_test(
        FIXTURES, cmds, FailedTest("example.com/calc/calc.TestZero/zeros", "x")
    )
    assert t is not None
    assert t.argv == ("go", "test", "-json", "-run", "^TestZero$/^zeros$", "example.com/calc/calc")


def test_goldens() -> None:
    adapter = GoAdapter()
    cmds = adapter.commands(FIXTURES / "go_calc", None)
    report = adapter.parse_tests(result((OUTPUTS / "go_test_module.json").read_text()), cmds)
    assert [f.id for f in report.failed] == ["example.com/calc/calc.TestAdd"]
    failure = report.failed[0]
    assert (failure.file, failure.line) == ("calc/calc_test.go", 7)  # package-relative → repo
    assert "want 5" in failure.message
    broken = adapter.parse_tests(result((OUTPUTS / "go_test_build_fail.json").read_text()), cmds)
    assert not broken.ran and any("calc/calc.go:4:9: invalid operation" in e for e in broken.errors)
    vet = adapter.parse_diagnostics(result("", 1, (OUTPUTS / "go_vet.txt").read_text()), "go vet")
    assert [(d.file, d.line, d.column) for d in vet] == [("calc/calc.go", 4, 9)]
    lint = adapter.parse_diagnostics(
        result("main.go:5:2: ineffectual assignment to x (ineffassign)\n"), "golangci-lint"
    )
    assert lint[0].code == "ineffassign" and lint[0].message == "ineffectual assignment to x"


# ---------------------------------------------------------------- live: real go through the sandbox


def make_runner(repo: Path, tmp_path: Path) -> CommandRunner:
    assert GO is not None
    return sandboxed_runner(repo, tmp_path, str(Path(GO).parent))


@go_only
async def test_live_fail_fix_targeted_pass_and_vet(tmp_path: Path) -> None:
    repo = tmp_path / "calc"
    shutil.copytree(FIXTURES / "go_calc", repo)
    adapter = GoAdapter()
    cmds = adapter.commands(repo, None)
    assert cmds.test is not None
    runner = make_runner(repo, tmp_path)
    outcome = await runner.run(cmds.test.argv, timeout=600)
    assert outcome.result is not None
    report = adapter.parse_tests(outcome.result, cmds)
    assert [f.id for f in report.failed] == ["example.com/calc/calc.TestAdd"], outcome.result
    assert (report.failed[0].file, report.failed[0].line) == ("calc/calc_test.go", 7)

    fs = FsTools(runner.env, runner.policy)
    assert (
        await fs.patch_file("calc/calc.go", [SearchReplace("\treturn a - b\n", "\treturn a + b\n")])
    ).applied
    targeted = adapter.targeted_test(repo, cmds, report.failed[0])
    assert targeted is not None
    again = await runner.run(targeted.argv, timeout=600)
    assert again.result is not None
    rerun = adapter.parse_tests(again.result, cmds)
    assert rerun.ok and rerun.passed == 1, again.result

    (repo / "calc/calc.go").write_text(
        'package calc\n\nfunc Add(a, b int) int {\n\treturn a - "x"\n}\n'
    )
    assert cmds.check is not None
    vet = await runner.run(cmds.check.argv, timeout=600)
    assert vet.result is not None
    diags = adapter.parse_diagnostics(vet.result, "go vet")
    assert [(d.file, d.line) for d in diags] == [("calc/calc.go", 4)]
    assert sorted(p.name for p in repo.iterdir()) == ["calc", "go.mod"]  # no binaries left behind


@go_only
async def test_implement_fixes_a_go_module(
    home: Path, git_repo: Path, git: Callable[..., str]
) -> None:
    shutil.copytree(FIXTURES / "go_calc", git_repo, dirs_exist_ok=True)
    git(git_repo, "add", ".")
    git(git_repo, "commit", "-qm", "calc")
    fix = {
        "action": "patch",
        "reason": "wrong operator",
        "path": "calc/calc.go",
        "search": "\treturn a - b\n",
        "replace": "\treturn a + b\n",
    }
    rt = Runtime.create(git_repo, request="fix the failing test", mode="implement")
    outcome = (
        await implement(
            rt,
            "fix the failing test",
            ImplementOptions(coder=ScriptedCoder(fix, DONE), max_steps=4),
        )
    ).outcome
    await rt.close(outcome.session_status)
    assert outcome.status == "completed", outcome.report()
    assert "go test -json" in outcome.report() and "go vet" in outcome.report()
