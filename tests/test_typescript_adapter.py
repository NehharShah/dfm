import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from dfm.integrations.repocanon import RepoCanonProvider
from dfm.orchestration.implement import ImplementOptions, implement
from dfm.orchestration.runtime import Runtime
from dfm.processes import ProcessResult
from dfm.tools.filesystem import FsTools, SearchReplace
from dfm.tools.languages import ToolchainCommands, ToolCommand, TypeScriptAdapter, select_adapter
from dfm.tools.languages.typescript import package_dir, package_manager
from dfm.tools.results import FailedTest, parse_test_output
from dfm.tools.shell.runner import CommandRunner
from tests import repo_fixtures as fx
from tests.helpers import DONE, ScriptedCoder, sandboxed_runner

FIXTURES = Path(__file__).parent / "fixtures"
OUTPUTS = FIXTURES / "test_outputs"
NODE = shutil.which("node")
node_only = pytest.mark.skipif(NODE is None, reason="node not installed")
# tsc is rarely global; a sibling checkout's copy is used read-only when present
TSC = shutil.which("tsc") or next(
    (
        str(p)
        for p in [Path(__file__).parents[2] / "reflex-tui" / "node_modules" / ".bin" / "tsc"]
        if p.exists()
    ),
    None,
)


def result(stdout: str, code: int = 1, stderr: str = "") -> ProcessResult:
    return ProcessResult(("x",), code, stdout, stderr, 5)


# ---------------------------------------------------------------- command selection


def test_package_manager_from_lockfile_and_field(tmp_path: Path) -> None:
    assert package_manager(fx.write(tmp_path / "a", {"package.json": "{}"})) == "npm"
    assert (
        package_manager(fx.write(tmp_path / "b", {"package.json": "{}", "yarn.lock": ""})) == "yarn"
    )
    assert (
        package_manager(fx.write(tmp_path / "c", {"package.json": "{}", "bun.lock": ""})) == "bun"
    )
    declared = {"package.json": json.dumps({"packageManager": "pnpm@9.1.0"}), "yarn.lock": ""}
    assert package_manager(fx.write(tmp_path / "d", declared)) == "pnpm"


def install_bins(directory: Path, *names: str) -> Path:
    """Pretend `names` are installed locally (only their presence matters for selection)."""
    for name in names:
        fx.write(directory, {f"node_modules/.bin/{name}": ""})
    return directory


def test_vitest_script_runs_with_json_reporter(tmp_path: Path) -> None:
    repo = install_bins(shutil.copytree(FIXTURES / "ts_calc", tmp_path / "ts"), "vitest", "tsc")
    adapter = TypeScriptAdapter()
    assert isinstance(select_adapter(repo, None), TypeScriptAdapter)
    cmds = adapter.commands(repo, None)
    assert cmds.test is not None
    assert cmds.test.argv == ("npx", "--no", "vitest", "run", "--reporter=json")
    assert cmds.reporter == "vitest-json"
    assert cmds.check is not None
    assert cmds.check.argv == ("npx", "--no", "tsc", "--noEmit", "--pretty", "false")


def test_tools_that_are_not_installed_are_never_fetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # no global tsc / vitest
    cmds = TypeScriptAdapter().commands(FIXTURES / "ts_calc", None)
    assert cmds.test is not None and cmds.test.argv == ("npm", "run", "test")  # fails fast
    assert cmds.check is None  # `npx tsc` would go to the registry
    assert any("tsc is configured but not installed" in n for n in cmds.notes)
    assert any("node_modules missing" in n for n in cmds.notes)


async def test_monorepo_package_from_repocanon(tmp_path: Path) -> None:
    repo = fx.pnpm_monorepo(tmp_path / "mono")
    canon = await RepoCanonProvider().analyze(repo)
    adapter = TypeScriptAdapter()
    plain = adapter.commands(repo, canon, "packages/api/src/server.ts")
    assert plain.test is not None and plain.test.cwd == "packages/api"
    assert plain.test.source == "repocanon" and plain.reporter == "auto"
    install_bins(repo / "packages/api", "jest")
    cmds = adapter.commands(repo, canon, "packages/api/src/server.ts")
    assert cmds.test is not None and cmds.test.cwd == "packages/api"
    assert cmds.test.argv == ("pnpm", "exec", "jest", "--json") and cmds.reporter == "jest-json"
    assert cmds.test.source == "repocanon"
    assert package_dir(repo, "packages/api/src/server.ts") == "packages/api"


def test_compound_script_and_bare_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    compound = fx.write(
        tmp_path / "c",
        {"package.json": json.dumps({"scripts": {"test": "tsc && vitest run"}}), "yarn.lock": ""},
    )
    cmds = TypeScriptAdapter().commands(compound, None)
    assert cmds.test is not None and cmds.test.argv == ("yarn", "run", "test")
    assert cmds.reporter == "auto"
    bare = install_bins(
        fx.write(tmp_path / "b", {"package.json": "{}", "eslint.config.js": "", ".prettierrc": ""}),
        "eslint",
        "prettier",
    )
    cmds = TypeScriptAdapter().commands(bare, None)
    assert cmds.test is not None and cmds.test.argv == ("node", "--test", "--test-reporter=tap")
    assert cmds.lint is not None and cmds.lint.argv == ("npx", "--no", "eslint", "-f", "json", ".")
    assert cmds.format_check is not None and "--check" in cmds.format_check.argv
    assert cmds.check is None  # no tsconfig


def test_targeted_tests() -> None:
    adapter = TypeScriptAdapter()
    failure = FailedTest("add adds two numbers", "x", "packages/web/src/calc.test.ts", 6)
    vitest = ToolchainCommands(
        "TypeScript",
        test=ToolCommand(("pnpm", "exec", "vitest", "run", "--reporter=json"), "packages/web"),
        reporter="vitest-json",
    )
    t = adapter.targeted_test(Path(), vitest, failure)
    assert t is not None and t.argv == (
        "pnpm",
        "exec",
        "vitest",
        "run",
        "src/calc.test.ts",
        "-t",
        r"add\ adds\ two\ numbers",
        "--reporter=json",
    )
    node = ToolchainCommands(
        "TypeScript", test=ToolCommand(("node", "--test", "--test-reporter=tap")), reporter="tap"
    )
    n = adapter.targeted_test(Path(), node, FailedTest("adds", "x", "src/a.test.ts"))
    assert n is not None and n.argv[-2:] == ("--test-name-pattern=adds", "src/a.test.ts")
    plain = ToolchainCommands("TypeScript", test=ToolCommand(("npm", "run", "test")))
    assert adapter.targeted_test(Path(), plain, failure) is None


# ---------------------------------------------------------------- golden outputs (captured from real tools)


def test_vitest_golden() -> None:
    report = parse_test_output(result((OUTPUTS / "vitest_fail.json").read_text()), "vitest-json")
    assert report.passed == 1 and [f.id for f in report.failed] == ["add adds two numbers"]
    failure = report.failed[0]
    assert (failure.file, failure.line) == ("/work/ts/src/calc.test.ts", 6)
    assert "expected -1 to be 5" in failure.message
    localized = report.localize(Path("/work/ts"))
    assert localized.failed[0].file == "src/calc.test.ts"


def test_node_test_tap_golden() -> None:
    report = parse_test_output(result((OUTPUTS / "node_test_tap.txt").read_text()), "tap")
    assert report.passed == 1 and [f.id for f in report.failed] == ["adds two numbers"]
    failure = report.failed[0]
    assert (failure.file, failure.line) == ("/work/nt/src/calc.test.ts", 5)
    assert failure.message == "Expected values to be strictly equal:"


def test_tsc_and_eslint_goldens() -> None:
    adapter = TypeScriptAdapter()
    tsc = adapter.parse_diagnostics(result((OUTPUTS / "tsc.txt").read_text(), 2), "tsc")
    assert [(d.file, d.line, d.code) for d in tsc] == [("src/calc.ts", 2, "TS2322")]
    eslint = adapter.parse_diagnostics(result((OUTPUTS / "eslint.json").read_text()), "eslint")
    assert [(d.line, d.code) for d in eslint] == [
        (1, "no-unused-vars"),
        (2, "no-unused-vars"),
        (3, "no-undef"),
    ]
    assert eslint[0].file == "/work/es/calc.js" and eslint[0].severity == "error"


# ---------------------------------------------------------------- live: real node / tsc through the sandbox


def make_runner(repo: Path, tmp_path: Path, *extra_path: str) -> CommandRunner:
    assert NODE is not None
    return sandboxed_runner(repo, tmp_path, *extra_path, str(Path(NODE).parent))


@node_only
async def test_live_node_test_fail_fix_pass(tmp_path: Path) -> None:
    repo = tmp_path / "calc"
    shutil.copytree(FIXTURES / "node_calc", repo)
    adapter = TypeScriptAdapter()
    cmds = adapter.commands(repo, None)
    assert cmds.test is not None and cmds.reporter == "tap"
    runner = make_runner(repo, tmp_path)
    outcome = await runner.run(cmds.test.argv, timeout=120)
    assert outcome.result is not None
    report = adapter.parse_tests(outcome.result, cmds).localize(repo)
    assert [f.id for f in report.failed] == ["adds two numbers"], outcome.result
    assert (report.failed[0].file, report.failed[0].line) == ("src/calc.test.ts", 5)

    fs = FsTools(runner.env, runner.policy)
    assert (
        await fs.patch_file("src/calc.ts", [SearchReplace("  return a - b;", "  return a + b;")])
    ).applied
    targeted = adapter.targeted_test(repo, cmds, report.failed[0])
    assert targeted is not None
    again = await runner.run(targeted.argv, timeout=120)
    assert again.result is not None
    rerun = adapter.parse_tests(again.result, cmds)
    assert rerun.ok and rerun.passed == 1, again.result


@node_only
@pytest.mark.skipif(TSC is None, reason="tsc not available")
async def test_live_tsc_error_to_file_line(tmp_path: Path) -> None:
    assert TSC is not None
    repo = tmp_path / "calc"
    shutil.copytree(FIXTURES / "node_calc", repo)
    (repo / "src/calc.ts").write_text(
        'export function add(a: number, b: number): number {\n  return "x";\n}\n'
    )
    runner = make_runner(repo, tmp_path, str(Path(TSC).parent))
    outcome = await runner.run(["tsc", "--noEmit", "--pretty", "false", "-p", "."], timeout=120)
    assert outcome.result is not None
    diags = TypeScriptAdapter().parse_diagnostics(outcome.result, "tsc")
    assert [(d.file, d.line, d.code) for d in diags] == [("src/calc.ts", 2, "TS2322")]
    assert not (repo / "tsconfig.tsbuildinfo").exists()


@node_only
async def test_implement_fixes_a_typescript_package(
    home: Path, git_repo: Path, git: Callable[..., str]
) -> None:
    shutil.copytree(FIXTURES / "node_calc", git_repo, dirs_exist_ok=True)
    git(git_repo, "add", ".")
    git(git_repo, "commit", "-qm", "calc")
    fix = {
        "action": "patch",
        "reason": "wrong operator",
        "path": "src/calc.ts",
        "search": "  return a - b;\n",
        "replace": "  return a + b;\n",
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
    assert "node --test" in outcome.report()
