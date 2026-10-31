"""Go toolchain adapter: go, gofmt, gopls, golangci-lint.

Command choice: RepoCanon's command for the owning package, then defaults run from
the owning module's directory (nearest go.mod; go.work workspaces are honored by
the go command itself). `go test` always runs with `-json`. The type check is
`go vet ./...`, which compiles every package without writing binaries
(`go build ./...` would drop an executable when it matches a single main package).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from dfm.processes import ProcessResult
from dfm.tools.languages.base import (
    LspServerSpec,
    ToolchainCommands,
    ToolCommand,
    canon_command,
    language_named,
    runs,
    which_all,
)
from dfm.tools.results import (
    Diagnostic,
    FailedTest,
    TestReport,
    parse_go_diagnostics,
    parse_test_output,
)

_GO_TOOLS = {"go", "make", "gotestsum"}
_TEST_ID = re.compile(r"^(?P<package>.+)\.(?P<test>(?:Test|Example|Fuzz)[^./]*(?:/.*)?)$")


def module_path(module_dir: Path) -> str | None:
    try:
        text = (module_dir / "go.mod").read_text()
    except OSError:
        return None
    match = re.search(r"^module\s+(\S+)", text, re.M)
    return match.group(1).strip('"') if match else None


def module_dir(repo: Path, rel_path: str | None) -> str:
    """The nearest directory at or above `rel_path` with a go.mod ("." if none)."""
    if not rel_path:
        return "."
    for parent in [Path(rel_path), *Path(rel_path).parents]:
        if parent.as_posix() not in {"", "."} and (repo / parent / "go.mod").is_file():
            return parent.as_posix()
    return "."


def workspace_modules(repo: Path) -> list[str]:
    """Module directories listed by go.work `use` directives (empty without go.work)."""
    try:
        text = (repo / "go.work").read_text()
    except OSError:
        return []
    uses = re.findall(r"^\s*use\s+([^\s(]\S*)\s*(?://.*)?$", text, re.M)
    for block in re.findall(r"^\s*use\s*\((.*?)\)", text, re.M | re.S):
        uses += [
            ln.split("//")[0].strip() for ln in block.splitlines() if ln.split("//")[0].strip()
        ]
    return [u.removeprefix("./") or "." for u in uses]


class GoAdapter:
    language = "Go"
    extensions = (".go",)
    manifests = ("go.mod", "go.work")

    def detect(self, repo: Path, canon: Any) -> bool:
        return any((repo / m).exists() for m in self.manifests) or language_named(canon, "Go")

    def commands(self, repo: Path, canon: Any, rel_path: str | None = None) -> ToolchainCommands:
        notes: list[str] = []
        test = canon_command(canon, "test", rel_path, _GO_TOOLS)
        if test is not None and test.argv[:2] == ("go", "test"):
            argv = test.argv if "-json" in test.argv else ("go", "test", "-json", *test.argv[2:])
            test = ToolCommand(argv, test.cwd, "repocanon")
        elif test is not None:
            test = ToolCommand(test.argv, test.cwd, "repocanon")
            notes.append(f"RepoCanon test command `{test}` is not `go test`: output parsed as text")
        else:
            test = ToolCommand(("go", "test", "-json", "./..."), module_dir(repo, rel_path))
        cwd = test.cwd
        reporter = "go-json" if "-json" in test.argv else "auto"
        check = ToolCommand(("go", "vet", "./..."), cwd)
        tools = self.tools()
        lint = None
        if tools.get("golangci-lint") and any(
            (repo / cwd / n).exists() or (repo / n).exists()
            for n in (".golangci.yml", ".golangci.yaml", ".golangci.toml", ".golangci.json")
        ):
            lint = ToolCommand(("golangci-lint", "run", "./..."), cwd, "config")
        fmt = ToolCommand(("gofmt", "-w", "."), cwd) if tools.get("gofmt") else None
        notes.append("gofmt has no failing check mode: formatting is applied, not verified")
        meta = {"module_dir": cwd, "module": module_path(repo / cwd) or ""}
        return ToolchainCommands("Go", test, check, lint, fmt, None, reporter, notes, meta)

    def targeted_test(
        self, repo: Path, commands: ToolchainCommands, failure: FailedTest
    ) -> ToolCommand | None:
        test = commands.test
        match = _TEST_ID.match(failure.id)
        if test is None or test.argv[:2] != ("go", "test") or match is None:
            return None
        pattern = "/".join(f"^{re.escape(part)}$" for part in match.group("test").split("/"))
        flags = tuple(a for a in test.argv[2:] if a.startswith("-") and a != "-run")
        return ToolCommand(
            ("go", "test", *flags, "-run", pattern, match.group("package")), test.cwd, "targeted"
        )

    def parse_tests(self, result: ProcessResult, commands: ToolchainCommands) -> TestReport:
        report = parse_test_output(result, commands.reporter)
        module = commands.meta.get("module", "")
        base = commands.meta.get("module_dir", ".")
        located: list[FailedTest] = []
        for failure in report.failed:
            match = _TEST_ID.match(failure.id)
            file = failure.file
            if match and file and "/" not in file and module:
                # go prints test files relative to their package directory
                package = match.group("package")
                sub = package.removeprefix(module).lstrip("/") if package.startswith(module) else ""
                file = "/".join(p for p in (base, sub, file) if p and p != ".")
            located.append(
                FailedTest(failure.id, failure.message, file, failure.line, failure.trace)
            )
        report.failed = located
        return report

    def parse_diagnostics(self, result: ProcessResult, tool: str) -> list[Diagnostic]:
        diagnostics = parse_go_diagnostics(result.stdout + "\n" + result.stderr, tool or "go")
        if not diagnostics and result.exit_code not in (0, None):
            diagnostics.append(
                Diagnostic(
                    "error",
                    (result.stderr or result.stdout).strip()[-500:] or f"{tool} failed",
                    tool=tool,
                )
            )
        return diagnostics

    def lsp_server(self) -> LspServerSpec | None:
        if runs("gopls", "version"):
            return LspServerSpec("Go", ("gopls",), "go", self.extensions)
        return None

    def tools(self) -> dict[str, str | None]:
        return which_all(["go", "gofmt", "gopls", "golangci-lint"])
