from pathlib import Path

import pytest

from dfm.config import DFMConfig
from dfm.integrations.repocanon import RepoCanonProvider, degraded_discovery
from dfm.policy import Policy
from dfm.processes import ProcessManager, ProcessRegistry, ProcessResult
from dfm.tools.errors import ToolError
from dfm.tools.filesystem import FsTools, SearchReplace
from dfm.tools.languages import PythonAdapter
from dfm.tools.results import FailedTest
from dfm.tools.shell.runner import CommandRunner
from tests import repo_fixtures as fx
from tests.helpers import make_env


def make_runner(
    repo: Path, tmp_path: Path, events: list[tuple[str, dict[str, object]]] | None = None
) -> CommandRunner:
    env = make_env(repo, tmp_path, session_id="py")
    pm = ProcessManager(ProcessRegistry(env.tmp / "p"))
    sink = (lambda t, **kw: events.append((t, kw))) if events is not None else None
    return CommandRunner(env, pm, Policy(DFMConfig()), on_event=sink)


# ---------------------------------------------------------------- runner


async def test_runner_runs_allowed_and_traces(tmp_path: Path) -> None:
    repo = fx.python_uv(tmp_path / "r")
    events: list[tuple[str, dict[str, object]]] = []
    outcome = await make_runner(repo, tmp_path, events).run("ls src")
    assert outcome.allowed and outcome.result is not None and "calc" in outcome.result.stdout
    assert [t for t, _ in events] == ["command_started", "command_finished"]


async def test_runner_refuses_without_running(tmp_path: Path) -> None:
    repo = fx.python_uv(tmp_path / "r")
    events: list[tuple[str, dict[str, object]]] = []
    outcome = await make_runner(repo, tmp_path, events).run(["rm", "-rf", "src"])
    assert not outcome.allowed
    assert (repo / "src").exists()
    assert events[-1][0] == "command_finished" and events[-1][1]["refused"]
    leak = await make_runner(repo, tmp_path).run("cat ~/.ssh/id_rsa")
    assert not leak.allowed


async def test_runner_outside_cwd_and_missing_binary(tmp_path: Path) -> None:
    repo = fx.python_uv(tmp_path / "r")
    runner = make_runner(repo, tmp_path)
    with pytest.raises(ToolError):
        await runner.run("ls", cwd="../..")
    missing = await runner.run(["pytest-does-not-exist"])
    assert missing.allowed is False or (
        missing.result is not None and missing.result.exit_code == 127
    )


# ---------------------------------------------------------------- adapter commands


def test_uv_project_uses_uv_run(tmp_path: Path) -> None:
    repo = fx.python_uv(tmp_path / "r")
    cmds = PythonAdapter().commands(repo, degraded_discovery(repo))
    assert cmds.test is not None and cmds.test.argv[:3] == ("uv", "run", "pytest")
    assert cmds.reporter == "pytest"
    assert cmds.check is not None and "pyright" in cmds.check.argv
    assert cmds.lint is not None and cmds.lint.argv[:4] == ("uv", "run", "ruff", "check")


async def test_repocanon_command_gets_runner_prefix(tmp_path: Path) -> None:
    repo = fx.python_uv(tmp_path / "r")
    canon = await RepoCanonProvider().analyze(repo)
    cmds = PythonAdapter().commands(repo, canon)
    assert cmds.test is not None and cmds.test.argv[:3] == ("uv", "run", "pytest")
    assert cmds.test.source == "repocanon"


def test_plain_project_unittest_fallback_and_venv(tmp_path: Path) -> None:
    repo = fx.write(
        tmp_path / "r",
        {"setup.py": "", "tests/test_x.py": "import unittest\n", ".venv/bin/python": ""},
    )
    cmds = PythonAdapter().commands(repo, None)
    assert cmds.reporter == "unittest"
    assert cmds.test is not None and cmds.test.argv[0].endswith(".venv/bin/python")
    assert cmds.test.argv[1:] == ("-m", "unittest", "discover", "-s", "tests")
    assert cmds.check is not None and "compileall" in cmds.check.argv
    assert cmds.notes


def test_targeted_tests() -> None:
    from dfm.tools.languages.base import ToolchainCommands, ToolCommand

    adapter = PythonAdapter()
    repo = Path("/nonexistent")
    cmds = ToolchainCommands("Python", test=ToolCommand(("pytest", "-q")), reporter="pytest")
    t = adapter.targeted_test(repo, cmds, FailedTest("tests/test_a.py::test_b", "x"))
    assert t is not None and t.argv == ("pytest", "tests/test_a.py::test_b", "-q")
    u = ToolchainCommands(
        "Python", test=ToolCommand(("python", "-m", "unittest")), reporter="unittest"
    )
    tu = adapter.targeted_test(repo, u, FailedTest("tests.test_a.T.test_b", "x"))
    assert tu is not None and tu.argv == ("python", "-m", "unittest", "tests.test_a.T.test_b")


def test_diagnostics_fallback() -> None:
    result = ProcessResult(
        ("mypy",), 1, "a.py:3: error: Incompatible return value  [return-value]\n", "", 5
    )
    diags = PythonAdapter().parse_diagnostics(result, "mypy")
    assert diags and diags[0].line == 3


# ---------------------------------------------------------------- end to end: real pytest through the runner


async def test_fail_parse_fix_pass(tmp_path: Path) -> None:
    repo = fx.write(
        tmp_path / "calc",
        {
            "pyproject.toml": '[project]\nname = "calc"\nversion = "0"\n[tool.pytest.ini_options]\npythonpath = ["src"]\n',
            "src/calc/__init__.py": "",
            "src/calc/ops.py": "def add(a, b):\n    return a - b\n",
            "tests/test_ops.py": "from calc.ops import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        },
    )
    adapter = PythonAdapter()
    cmds = adapter.commands(repo, degraded_discovery(repo))
    assert cmds.test is not None and cmds.test.argv[0] == "pytest"
    runner = make_runner(repo, tmp_path)
    outcome = await runner.run(cmds.test.argv)
    assert outcome.result is not None
    report = adapter.parse_tests(outcome.result, cmds)
    assert report.ran and len(report.failed) == 1
    assert report.failed[0].id == "tests/test_ops.py::test_add"
    fs = FsTools(runner.env, runner.policy)
    patched = await fs.patch_file(
        "src/calc/ops.py", [SearchReplace("return a - b", "return a + b")]
    )
    assert patched.applied
    targeted = adapter.targeted_test(repo, cmds, report.failed[0])
    assert targeted is not None
    again = await runner.run(targeted.argv)
    assert again.result is not None and adapter.parse_tests(again.result, cmds).ok
    assert not list(repo.rglob("*.pyc"))  # DFM's runs leave no bytecode behind


async def test_compile_check_leaves_no_bytecode_in_repo(tmp_path: Path) -> None:
    repo = fx.python_uv(tmp_path / "r")
    runner = make_runner(repo, tmp_path)
    outcome = await runner.run(["python3", "-m", "compileall", "-q", "src"])
    assert outcome.result is not None and outcome.result.exit_code == 0
    assert not list(repo.rglob("*.pyc"))
    assert list((runner.env.tmp / "pycache").rglob("*.pyc"))


def test_pip_pyright_wrapper_without_node_is_not_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dfm.tools.languages import python as py

    wrapper = tmp_path / "pyright-langserver"
    wrapper.write_text(
        "#!/usr/bin/python3\nimport sys\nfrom pyright.langserver import entrypoint\nsys.exit(entrypoint())\n"
    )
    monkeypatch.setattr(
        py.shutil, "which", lambda name, *a, **k: None if name == "node" else str(wrapper)
    )
    assert py._downloads_node(wrapper)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(
        py.shutil,
        "which",
        lambda name, *a, **k: "/opt/homebrew/bin/node" if name == "node" else str(wrapper),
    )
    assert not py._downloads_node(wrapper)  # pyright: ignore[reportPrivateUsage]
