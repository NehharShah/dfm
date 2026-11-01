"""Python toolchain adapter: uv, pytest, unittest, ruff, pyright, mypy.

Command choice: RepoCanon's command for the owning package, then repo
configuration, then defaults. Tools run through the project's runner: `uv run`
when uv manages the project, `poetry run` for poetry, the repo's `.venv/bin`
when present, else the PATH. (RepoCanon reports bare `pytest` for uv projects;
the runner prefix fixes that here, see docs/integrations.md.)
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, cast

from dfm.integrations.repocanon import scoped_commands
from dfm.processes import ProcessResult
from dfm.tools.languages.base import (
    LspServerSpec,
    ToolchainCommands,
    ToolCommand,
    fallback_diagnostic,
    read_toml,
    which_all,
)
from dfm.tools.results import (
    Diagnostic,
    FailedTest,
    TestReport,
    parse_diagnostics_text,
    parse_pyright_json,
    parse_ruff_json,
    parse_test_output,
)

_PY_TOOLS = {
    "pytest",
    "py.test",
    "ruff",
    "pyright",
    "mypy",
    "black",
    "isort",
    "python",
    "python3",
    "coverage",
    "tox",
    "nox",
}
_RUNNERS = {"uv", "uvx", "poetry", "pdm", "hatch", "pipenv"}


def _pyproject(repo: Path) -> dict[str, Any]:
    return read_toml(repo / "pyproject.toml")


def _downloads_node(script: Path) -> bool:
    """The pip `pyright` wrappers run Node.js and, without one on PATH, download it (nodeenv)
    outside any policy. Treat such a server as unavailable rather than let it fetch."""
    try:
        head = script.read_text(errors="replace")[:600]
    except OSError:
        return False
    return "pyright" in head and "import" in head and shutil.which("node") is None


class PythonAdapter:
    language = "Python"
    extensions = (".py", ".pyi")
    manifests = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")

    def detect(self, repo: Path, canon: Any) -> bool:
        if any((repo / f).exists() for f in self.manifests):
            return True
        return any(
            getattr(lang, "name", lang) == "Python"
            for lang in getattr(canon, "languages", []) or []
        )

    def runner(self, repo: Path) -> tuple[str, ...]:
        data = _pyproject(repo)
        tool = cast(dict[str, Any], data.get("tool", {}))
        if (repo / "uv.lock").exists() or "uv" in tool:
            return ("uv", "run")
        if (repo / "poetry.lock").exists() or "poetry" in tool:
            return ("poetry", "run")
        return ()

    def _with_runner(self, repo: Path, argv: tuple[str, ...]) -> tuple[str, ...]:
        if not argv or argv[0] in _RUNNERS:
            return argv
        prefix = self.runner(repo)
        if prefix:
            return prefix + argv
        venv_tool = repo / ".venv" / "bin" / argv[0]
        if argv[0] in _PY_TOOLS and venv_tool.exists():
            return (str(venv_tool), *argv[1:])
        return argv

    def _configured(self, repo: Path, tool_name: str) -> bool:
        tool = cast(dict[str, Any], _pyproject(repo).get("tool", {}))
        if tool_name in tool:
            return True
        files = {
            "pyright": ["pyrightconfig.json"],
            "mypy": ["mypy.ini", ".mypy.ini"],
            "ruff": ["ruff.toml", ".ruff.toml"],
        }
        if any((repo / f).exists() for f in files.get(tool_name, [])):
            return True
        if tool_name == "mypy" and (repo / "setup.cfg").exists():
            return "[mypy" in (repo / "setup.cfg").read_text(errors="replace")
        return False

    def _uses_pytest(self, repo: Path) -> bool:
        if (repo / "conftest.py").exists() or (repo / "pytest.ini").exists():
            return True
        text = json.dumps(_pyproject(repo))
        if "pytest" in text:
            return True
        for test_file in (
            list((repo / "tests").glob("**/*.py"))[:50] if (repo / "tests").is_dir() else []
        ):
            if "import pytest" in test_file.read_text(errors="replace"):
                return True
        return False

    def commands(self, repo: Path, canon: Any, rel_path: str | None = None) -> ToolchainCommands:
        notes: list[str] = []

        def from_canon(kind: str) -> ToolCommand | None:
            if canon is None:
                return None
            for sc in scoped_commands(canon, kind, rel_path):  # type: ignore[arg-type]
                cmd = ToolCommand.parse(sc.command, sc.cwd)
                if cmd.argv and (cmd.argv[0] in _PY_TOOLS | _RUNNERS or cmd.argv[0] == "make"):
                    return ToolCommand(self._with_runner(repo, cmd.argv), cmd.cwd, "repocanon")
            return None

        test = from_canon("test")
        if test is None:
            if self._uses_pytest(repo):
                test = ToolCommand(self._with_runner(repo, ("pytest",)), source="default")
            else:
                start = "tests" if (repo / "tests").is_dir() else "."
                test = ToolCommand(
                    self._with_runner(repo, ("python", "-m", "unittest", "discover", "-s", start)),
                    source="default",
                )
                notes.append("no pytest configuration found: using unittest discovery")
        reporter = "unittest" if "unittest" in test.argv else "pytest"
        if reporter == "pytest" and "-q" not in test.argv:
            test = ToolCommand((*test.argv, "-q", "-rfE", "--color=no"), test.cwd, test.source)

        check = None
        if self._configured(repo, "pyright"):
            check = ToolCommand(
                self._with_runner(repo, ("pyright", "--outputjson")), source="config"
            )
        elif self._configured(repo, "mypy"):
            check = ToolCommand(self._with_runner(repo, ("mypy", ".")), source="config")
        else:
            check = ToolCommand(
                self._with_runner(repo, ("python", "-m", "compileall", "-q", ".")), source="default"
            )
        lint = format_cmd = format_check = None
        if self._configured(repo, "ruff"):
            lint = ToolCommand(
                self._with_runner(repo, ("ruff", "check", "--output-format", "json", ".")),
                source="config",
            )
            format_cmd = ToolCommand(
                self._with_runner(repo, ("ruff", "format", ".")), source="config"
            )
            format_check = ToolCommand(
                self._with_runner(repo, ("ruff", "format", "--check", ".")), source="config"
            )
        elif self._configured(repo, "black"):
            format_cmd = ToolCommand(self._with_runner(repo, ("black", ".")), source="config")
            format_check = ToolCommand(
                self._with_runner(repo, ("black", "--check", ".")), source="config"
            )
        return ToolchainCommands(
            "Python", test, check, lint, format_cmd, format_check, reporter, notes
        )

    def targeted_test(
        self, repo: Path, commands: ToolchainCommands, failure: FailedTest
    ) -> ToolCommand | None:
        if commands.test is None:
            return None
        if commands.reporter == "unittest":
            return ToolCommand(
                self._with_runner(repo, ("python", "-m", "unittest", failure.id)),
                commands.test.cwd,
                "targeted",
            )
        base = [a for a in commands.test.argv if not a.startswith("-")]
        flags = [a for a in commands.test.argv if a.startswith("-")]
        if "::" in failure.id or failure.id.endswith(".py"):
            return ToolCommand((*base, failure.id, *flags), commands.test.cwd, "targeted")
        return ToolCommand(
            (*base, "-k", failure.id.split("::")[-1], *flags), commands.test.cwd, "targeted"
        )

    def parse_tests(self, result: ProcessResult, commands: ToolchainCommands) -> TestReport:
        return parse_test_output(result, commands.reporter)

    def parse_diagnostics(self, result: ProcessResult, tool: str) -> list[Diagnostic]:
        text = result.stdout
        if tool == "pyright" and text.lstrip().startswith("{"):
            return parse_pyright_json(text)
        if tool == "ruff" and text.lstrip().startswith("["):
            return parse_ruff_json(text)
        diagnostics = parse_diagnostics_text(result.stdout + "\n" + result.stderr, tool)
        if not diagnostics and result.exit_code not in (0, None):
            diagnostics.append(fallback_diagnostic(result, tool))
        return diagnostics

    def lsp_server(self) -> LspServerSpec | None:
        for argv in (
            ("pyright-langserver", "--stdio"),
            ("basedpyright-langserver", "--stdio"),
            ("pylsp",),
        ):
            found = which_all([argv[0]])[argv[0]]
            if found and not _downloads_node(Path(found)):
                return LspServerSpec("Python", argv, "python", self.extensions)
        return None

    def tools(self) -> dict[str, str | None]:
        return which_all(["python3", "uv", "pytest", "ruff", "pyright", "mypy"])
