"""Rust toolchain adapter: cargo, rustc, rustfmt, clippy, rust-analyzer.

Command choice: RepoCanon's command for the owning crate (run in that crate's
directory, which is how cargo picks the package), then defaults. `cargo check`
and `cargo clippy` use `--message-format=json` so diagnostics come back as
file:line records. build.rs and proc macros run code at check time, so every
cargo command goes through CommandRunner and the sandbox like any other build.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, cast

from dfm.processes import ProcessResult
from dfm.tools.languages.base import (
    LspServerSpec,
    ToolchainCommands,
    ToolCommand,
    canon_command,
    language_named,
    runs,
    which_all,
    with_filter,
)
from dfm.tools.results import (
    Diagnostic,
    FailedTest,
    TestReport,
    parse_cargo_json,
    parse_diagnostics_text,
    parse_test_output,
)

_RUST_TOOLS = {"cargo", "make", "just"}


def _manifest(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def workspace_members(repo: Path) -> dict[str, str]:
    """Crate name → directory (relative to the repo) for every workspace member."""
    root = _manifest(repo / "Cargo.toml")
    members: dict[str, str] = {}
    package = cast(dict[str, Any], root.get("package", {}))
    if "name" in package:
        members[str(package["name"])] = "."
    workspace = cast(dict[str, Any], root.get("workspace", {}))
    excluded = {str(e) for e in cast(list[Any], workspace.get("exclude", []))}
    for pattern in cast(list[Any], workspace.get("members", [])):
        for directory in sorted(repo.glob(str(pattern))):
            rel = directory.relative_to(repo).as_posix()
            if rel in excluded:
                continue
            name = cast(dict[str, Any], _manifest(directory / "Cargo.toml").get("package", {}))
            if "name" in name:
                members[str(name["name"])] = rel
    return members


def crate_for(repo: Path, rel_path: str) -> str | None:
    """The workspace member that owns `rel_path` (deepest directory wins)."""
    best: tuple[int, str] | None = None
    for name, directory in workspace_members(repo).items():
        if directory == "." or rel_path == directory or rel_path.startswith(directory + "/"):
            depth = 0 if directory == "." else directory.count("/") + 1
            if best is None or depth > best[0]:
                best = (depth, name)
    return best[1] if best else None


class RustAdapter:
    language = "Rust"
    extensions = (".rs",)
    manifests = ("Cargo.toml",)

    def detect(self, repo: Path, canon: Any) -> bool:
        return (repo / "Cargo.toml").exists() or language_named(canon, "Rust")

    def commands(self, repo: Path, canon: Any, rel_path: str | None = None) -> ToolchainCommands:
        notes: list[str] = []
        test = canon_command(canon, "test", rel_path, _RUST_TOOLS)
        if test is not None:
            test = ToolCommand(test.argv, test.cwd, "repocanon")
        else:
            argv: tuple[str, ...] = ("cargo", "test")
            crate = crate_for(repo, rel_path) if rel_path else None
            if crate is not None and workspace_members(repo).get(crate) != ".":
                argv = (*argv, "-p", crate)
            test = ToolCommand(argv, source="default")
        if test.argv[:1] == ("cargo",) and "nextest" in test.argv:
            notes.append("cargo-nextest output is parsed as plain text; failures may be coarse")
        cwd = test.cwd
        tools = self.tools()
        check = ToolCommand(
            ("cargo", "check", "--all-targets", "--message-format=json"), cwd, "default"
        )
        lint = (
            ToolCommand(
                ("cargo", "clippy", "--all-targets", "--message-format=json"), cwd, "default"
            )
            if tools.get("cargo-clippy")
            else None
        )
        if lint is None:
            notes.append("clippy not installed: no lint step")
        fmt = check_fmt = None
        if tools.get("rustfmt"):
            fmt = ToolCommand(("cargo", "fmt"), cwd, "default")
            check_fmt = ToolCommand(("cargo", "fmt", "--check"), cwd, "default")
        return ToolchainCommands("Rust", test, check, lint, fmt, check_fmt, "cargo", notes)

    def targeted_test(
        self, repo: Path, commands: ToolchainCommands, failure: FailedTest
    ) -> ToolCommand | None:
        if commands.test is None or commands.test.argv[:2] != ("cargo", "test"):
            return None
        return with_filter(commands.test, failure.id, extra=("--exact",))

    def parse_tests(self, result: ProcessResult, commands: ToolchainCommands) -> TestReport:
        report = parse_test_output(result, commands.reporter)
        if not report.ran and result.exit_code not in (0, None):
            # the test binary never built: surface the compiler errors instead of a bare tail
            errors = parse_diagnostics_text(result.stderr, "rustc")
            report.errors.extend(f"{d.location}: {d.message}" for d in errors[:10])
        return report

    def parse_diagnostics(self, result: ProcessResult, tool: str) -> list[Diagnostic]:
        diagnostics = parse_cargo_json(result.stdout)
        if tool == "clippy":
            diagnostics = [
                Diagnostic(d.severity, d.message, d.file, d.line, d.column, d.code, "clippy")
                for d in diagnostics
            ]
        if not diagnostics:
            diagnostics = parse_diagnostics_text(result.stderr, tool or "rustc")
        if not diagnostics and result.exit_code not in (0, None):
            diagnostics.append(
                Diagnostic("error", result.stderr.strip()[-500:] or f"{tool} failed", tool=tool)
            )
        return diagnostics

    def lsp_server(self) -> LspServerSpec | None:
        if runs("rust-analyzer"):  # often a rustup proxy without the component behind it
            return LspServerSpec("Rust", ("rust-analyzer",), "rust", self.extensions)
        return None

    def tools(self) -> dict[str, str | None]:
        return which_all(
            ["cargo", "rustc", "rustfmt", "cargo-clippy", "rust-analyzer", "cargo-nextest"]
        )
