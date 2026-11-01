"""Swift toolchain adapter: SwiftPM (`swift build` / `swift test`),
swift-format, swiftlint, sourcekit-lsp.

Command choice: RepoCanon's command for the owning package, then SwiftPM defaults
in the nearest Package.swift directory. Xcode projects are only detected:
xcodebuild is reported, never run on DFM's own initiative (schemes, signing and
simulators are the developer's call). Both Swift Testing and XCTest output parse.

SwiftPM sandboxes manifest evaluation with its own sandbox-exec, and macOS refuses
to nest sandboxes ("sandbox_apply: Operation not permitted"). DFM's sandbox already
confines the whole command (writes, and network off), so SwiftPM commands DFM builds
pass `--disable-sandbox`.
"""

from __future__ import annotations

import functools
import re
import subprocess
from pathlib import Path
from typing import Any

from dfm.processes import ProcessResult
from dfm.tools.languages.base import (
    LspServerSpec,
    ToolchainCommands,
    ToolCommand,
    canon_command,
    language_named,
    nearest_dir_with,
    runs,
    which_all,
)
from dfm.tools.results import (
    Diagnostic,
    FailedTest,
    TestReport,
    parse_diagnostics_text,
    parse_test_output,
    strip_ansi,
)

_SWIFT_TOOLS = {"swift", "make"}
# Swift 6.4 (Command Line Tools) intermittently fails to load the Swift Testing macro plugin,
# sandboxed or not (3 of 8 plain `swift test` runs on the bootstrap machine). Naming the plugin
# directory explicitly fixed it (8 of 8); the hint remains for toolchains laid out differently.
MACRO_PLUGIN_FLAKE = "plugin for module 'TestingMacros' not found"
TRANSIENT_HINT = (
    "toolchain: the Swift Testing macro plugin failed to load (intermittent in this toolchain, "
    "not a code error); rerun the tests"
)


@functools.cache
def swift_testing_plugin_dir() -> str | None:
    """The active toolchain's Swift Testing macro plugin directory, if it has one."""
    try:
        out = subprocess.run(
            ["xcrun", "--find", "swift"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    plugins = Path(out.stdout.strip()).resolve().parent.parent / "lib/swift/host/plugins/testing"
    return str(plugins) if plugins.is_dir() else None


def package_dir(repo: Path, rel_path: str | None) -> str:
    """The nearest directory at or above `rel_path` with a Package.swift ("." if none)."""
    return nearest_dir_with(repo, rel_path, "Package.swift")


class SwiftAdapter:
    language = "Swift"
    extensions = (".swift",)
    manifests = ("Package.swift",)

    def detect(self, repo: Path, canon: Any) -> bool:
        return (repo / "Package.swift").exists() or language_named(canon, "Swift")

    def commands(self, repo: Path, canon: Any, rel_path: str | None = None) -> ToolchainCommands:
        notes: list[str] = []
        test = canon_command(canon, "test", rel_path, _SWIFT_TOOLS)
        if test is not None:
            test = ToolCommand(test.argv, test.cwd, "repocanon")
        else:
            cwd = package_dir(repo, rel_path)
            if (repo / cwd / "Package.swift").exists():
                plugins = swift_testing_plugin_dir()
                extra = ("-Xswiftc", "-plugin-path", "-Xswiftc", plugins) if plugins else ()
                test = ToolCommand(("swift", "test", "--disable-sandbox", *extra), cwd)
        if test is None:
            if any(repo.glob("*.xcodeproj")) or any(repo.glob("*.xcworkspace")):
                notes.append(
                    "Xcode project without Package.swift: xcodebuild is not run automatically"
                )
            return ToolchainCommands("Swift", notes=notes, reporter="swift")
        cwd = test.cwd
        check = ToolCommand(("swift", "build", "--disable-sandbox"), cwd)
        lint = fmt = fmt_check = None
        if (repo / cwd / ".swiftlint.yml").exists() and which_all(["swiftlint"])["swiftlint"]:
            lint = ToolCommand(("swiftlint", "lint", "--quiet"), cwd, "config")
        if (repo / cwd / ".swift-format").exists():
            fmt = ToolCommand(("swift", "format", "--in-place", "--recursive", "."), cwd, "config")
            fmt_check = ToolCommand(
                ("swift", "format", "lint", "--strict", "--recursive", "."), cwd, "config"
            )
        return ToolchainCommands("Swift", test, check, lint, fmt, fmt_check, "swift", notes)

    def targeted_test(
        self, repo: Path, commands: ToolchainCommands, failure: FailedTest
    ) -> ToolCommand | None:
        test = commands.test
        if test is None or test.argv[:2] != ("swift", "test"):
            return None
        # Swift Testing: "addsTwoNumbers"; XCTest: "Module.Class/testMethod". --filter is a regex.
        return ToolCommand((*test.argv, "--filter", re.escape(failure.id)), test.cwd, "targeted")

    def parse_tests(self, result: ProcessResult, commands: ToolchainCommands) -> TestReport:
        report = parse_test_output(result, commands.reporter)
        if MACRO_PLUGIN_FLAKE in result.stderr:
            report.errors.insert(0, TRANSIENT_HINT)
        return report

    def parse_diagnostics(self, result: ProcessResult, tool: str) -> list[Diagnostic]:
        text = strip_ansi(result.stdout + "\n" + result.stderr)
        diagnostics = parse_diagnostics_text(text, tool or "swiftc")
        if not diagnostics and result.exit_code not in (0, None):
            tail = [ln for ln in text.splitlines() if ln.strip()][-5:]
            diagnostics.append(Diagnostic("error", "\n".join(tail) or f"{tool} failed", tool=tool))
        return diagnostics

    def lsp_server(self) -> LspServerSpec | None:
        if runs("sourcekit-lsp", "--help"):
            return LspServerSpec("Swift", ("sourcekit-lsp",), "swift", self.extensions)
        return None

    def tools(self) -> dict[str, str | None]:
        return which_all(["swift", "sourcekit-lsp", "swiftlint", "xcodebuild"])
