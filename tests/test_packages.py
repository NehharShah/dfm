from pathlib import Path

from dfm.orchestration.implement import ImplementOptions, implement
from dfm.orchestration.runtime import Runtime
from dfm.policy import Approval
from dfm.policy.types import ActionClass, PolicyVerdict, ProposedAction
from dfm.tools.packages import missing_packages, typo_warning
from tests import repo_fixtures as fx
from tests.helpers import DONE, ScriptedCoder


def only(text: str, repo: Path) -> tuple[str, tuple[str, ...] | None]:
    (proposal,) = missing_packages(text, repo)
    return proposal.package, proposal.command


def test_python(tmp_path: Path) -> None:
    pip = fx.write(tmp_path / "pip", {"setup.py": "", "mypkg/__init__.py": ""})
    assert only("ModuleNotFoundError: No module named 'yaml'", pip) == (
        "pyyaml",
        ("python", "-m", "pip", "install", "pyyaml"),
    )
    uv = fx.write(tmp_path / "uv", {"pyproject.toml": "[project]\nname='x'\n", "uv.lock": ""})
    assert only('error: Import "sklearn.metrics" could not be resolved', uv) == (
        "scikit-learn",
        ("uv", "add", "scikit-learn"),
    )
    assert missing_packages("No module named 'json'", pip) == []  # stdlib
    assert missing_packages("No module named 'mypkg.sub'", pip) == []  # the repo's own code
    assert missing_packages('Import "x" could not be resolved from source', pip) == []  # stubs only


def test_javascript(tmp_path: Path) -> None:
    pnpm = fx.write(tmp_path / "p", {"package.json": "{}", "pnpm-lock.yaml": ""})
    assert only("Error: Cannot find module 'lodash/merge'", pnpm) == (
        "lodash",
        ("pnpm", "add", "lodash"),
    )
    assert only("Module not found: Error: Can't resolve '@scope/ui/button'", pnpm)[0] == "@scope/ui"
    ts = "src/a.ts(1,20): error TS2307: Cannot find module 'node:fs' or its corresponding type declarations."
    assert only(ts, pnpm) == ("@types/node", ("pnpm", "add", "-D", "@types/node"))
    assert missing_packages("Cannot find module './local'", pnpm) == []
    assert missing_packages("Error: Cannot find module 'fs'", pnpm) == []  # runtime builtin
    npm = fx.write(tmp_path / "n", {"package.json": "{}"})
    assert only("Cannot find package 'zod'", npm) == ("zod", ("npm", "install", "zod"))


def test_rust_go_swift_forge(tmp_path: Path) -> None:
    repo = fx.write(tmp_path / "r", {})
    assert only("error[E0432]: unresolved import `serde`", repo) == (
        "serde",
        ("cargo", "add", "serde"),
    )
    assert missing_packages("error[E0433]: use of undeclared crate or module `std`", repo) == []
    go = (
        "main.go:4:2: no required module provides package github.com/acme/log; to add it:\n"
        "\tgo get github.com/acme/log"
    )
    assert only(go, repo) == ("github.com/acme/log", ("go", "get", "github.com/acme/log"))
    swift = missing_packages("error: no such module 'Alamofire'", repo)
    assert swift[0].command is None and "Package.swift" in swift[0].reason
    sol = 'Error: Source "forge-std/Test.sol" not found: File not found.'
    assert only(sol, repo) == ("foundry-rs/forge-std", ("forge", "install", "foundry-rs/forge-std"))


def test_typo_squatting_warning() -> None:
    assert typo_warning("python", "reqeusts") is not None
    assert typo_warning("python", "requests") is None
    assert typo_warning("npm", "lodahs") is not None
    assert typo_warning("cargo", "serde") is None
    assert typo_warning("python", "completely-unrelated-name") is None


class RecordingApprover:
    def __init__(self) -> None:
        self.asked: list[ProposedAction] = []

    async def ask(self, action: ProposedAction, verdict: PolicyVerdict) -> Approval:
        self.asked.append(action)
        return Approval.DENY


async def test_missing_dependency_asks_with_the_exact_command(
    home: Path, git_repo: Path, git
) -> None:  # type: ignore[no-untyped-def]
    fx.write(
        git_repo,
        {
            "pyproject.toml": '[project]\nname = "calc"\nversion = "0"\n[tool.pytest.ini_options]\npythonpath = ["src"]\n',
            "src/calc/__init__.py": "",
            "src/calc/fmt.py": "import fancyformat\n\n\ndef show(x):\n    return fancyformat.render(x)\n",
            "tests/test_fmt.py": "from calc.fmt import show\n\n\ndef test_show():\n    assert show(1) == '1'\n",
        },
    )
    git(git_repo, "add", ".")
    git(git_repo, "commit", "-qm", "fmt")
    approver = RecordingApprover()
    rt = Runtime.create(
        git_repo, request="fix the failing test", mode="implement", approver=approver
    )
    result = await implement(
        rt, "fix the failing test", ImplementOptions(coder=ScriptedCoder(DONE), max_steps=2)
    )
    await rt.close(result.outcome.session_status)
    installs = [a for a in approver.asked if a.action_class is ActionClass.PACKAGE_NETWORK]
    # the ASK names the install (the classifier shows the pip part of `python -m pip ...`)
    assert installs and installs[0].summary == "pip install fancyformat"
    assert len(installs) == 1  # asked once, not on every test run
    assert any(
        "missing python package fancyformat: `python -m pip install fancyformat`" in n
        for n in result.outcome.not_verified
    )
    assert result.outcome.status == "failed"  # reported, not papered over
