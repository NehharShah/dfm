"""What every language adapter provides.

An adapter turns repository facts (RepoCanon first) into concrete argv lists
for format / check / test / lint, picks the right result parser, and names the
LSP server. Build, test and lint commands go through CommandRunner; the only process an
adapter starts itself is a `--version` probe (`runs`) for tool discovery.
"""

from __future__ import annotations

import functools
import shlex
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from dfm.integrations.repocanon import scoped_commands
from dfm.processes import ProcessResult
from dfm.tools.results import Diagnostic, FailedTest, Reporter, TestReport


@dataclass(frozen=True)
class ToolCommand:
    argv: tuple[str, ...]
    cwd: str = "."
    source: str = "default"  # "repocanon" | "default" | "config"

    @staticmethod
    def parse(command: str, cwd: str = ".", source: str = "repocanon") -> ToolCommand:
        return ToolCommand(tuple(shlex.split(command)), cwd, source)

    def __str__(self) -> str:
        return shlex.join(self.argv) + (f"  (in {self.cwd})" if self.cwd != "." else "")


@dataclass(frozen=True)
class ToolchainCommands:
    language: str
    test: ToolCommand | None = None
    check: ToolCommand | None = None  # compile / typecheck
    lint: ToolCommand | None = None
    format: ToolCommand | None = None
    format_check: ToolCommand | None = None
    reporter: Reporter = "auto"
    notes: list[str] = field(default_factory=lambda: [])
    meta: dict[str, str] = field(default_factory=lambda: {})  # adapter-specific facts


@dataclass(frozen=True)
class LspServerSpec:
    language: str
    argv: tuple[str, ...]
    language_id: str
    extensions: tuple[str, ...]
    init_options: dict[str, Any] = field(default_factory=lambda: {})


class LanguageAdapter(Protocol):
    @property
    def language(self) -> str: ...

    @property
    def extensions(self) -> tuple[str, ...]: ...

    @property
    def manifests(self) -> tuple[str, ...]: ...  # root files that make the language certain

    def detect(self, repo: Path, canon: Any) -> bool: ...

    def commands(
        self, repo: Path, canon: Any, rel_path: str | None = None
    ) -> ToolchainCommands: ...

    def targeted_test(
        self, repo: Path, commands: ToolchainCommands, failure: FailedTest
    ) -> ToolCommand | None: ...

    def parse_tests(self, result: ProcessResult, commands: ToolchainCommands) -> TestReport: ...

    def parse_diagnostics(self, result: ProcessResult, tool: str) -> list[Diagnostic]: ...

    def lsp_server(self) -> LspServerSpec | None: ...

    def tools(self) -> dict[str, str | None]: ...


def which_all(names: list[str]) -> dict[str, str | None]:
    return {name: shutil.which(name) for name in names}


def canon_command(
    canon: Any, kind: str, rel_path: str | None, tools: set[str]
) -> ToolCommand | None:
    """RepoCanon's command of `kind` for the owning package, if it runs one of `tools`."""
    if canon is None:
        return None
    for sc in scoped_commands(canon, kind, rel_path):  # type: ignore[arg-type]
        cmd = ToolCommand.parse(sc.command, sc.cwd)
        if cmd.argv and cmd.argv[0] in tools:
            return cmd
    return None


def with_filter(command: ToolCommand, *args: str, extra: tuple[str, ...] = ()) -> ToolCommand:
    """`command` with `args` before any `--` separator and `extra` after it."""
    argv = list(command.argv)
    split = argv.index("--") if "--" in argv else len(argv)
    head, tail = argv[:split], argv[split + 1 :]
    rest = ("--", *tail, *extra) if (tail or extra) else ()
    return ToolCommand((*head, *args, *rest), command.cwd, "targeted")


def language_named(canon: Any, name: str) -> bool:
    return any(
        getattr(lang, "name", lang) == name for lang in getattr(canon, "languages", []) or []
    )


def read_toml(path: Path) -> dict[str, Any]:
    """A manifest's tables, or `{}` when it is missing or malformed (an adapter never fails on
    a broken manifest; it falls back to defaults)."""
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def nearest_dir_with(repo: Path, rel_path: str | None, manifest: str) -> str:
    """The nearest directory at or above `rel_path` holding `manifest` ("." if none): the
    package a file belongs to, for `go.mod`, `Scarb.toml`, `Package.swift` and the like."""
    if not rel_path:
        return "."
    for parent in [Path(rel_path), *Path(rel_path).parents]:
        if parent.as_posix() not in {"", "."} and (repo / parent / manifest).is_file():
            return parent.as_posix()
    return "."


def toml_workspace_members(repo: Path, manifest: str) -> dict[str, str]:
    """Package name → directory (relative to the repo) for the root `[package]` and every
    `[workspace] members` glob, minus `[workspace] exclude`: Cargo.toml and Scarb.toml share
    this layout."""
    root = read_toml(repo / manifest)
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
            name = cast(dict[str, Any], read_toml(directory / manifest).get("package", {}))
            if "name" in name:
                members[str(name["name"])] = rel
    return members


def fallback_diagnostic(result: ProcessResult, tool: str, *, lines: int = 0) -> Diagnostic:
    """When a tool failed but nothing parsed: one error carrying its output tail, so the model
    sees why. `lines` > 0 keeps the last non-blank lines; otherwise the last 500 characters."""
    text = (result.stderr or result.stdout).strip()
    if lines:
        tail = "\n".join([ln for ln in text.splitlines() if ln.strip()][-lines:])
    else:
        tail = text[-500:]
    return Diagnostic("error", tail or f"{tool} failed", tool=tool)


@functools.cache
def runs(binary: str, *args: str) -> bool:
    """Whether `binary` exists *and* starts: rustup and xcrun install proxies for missing components."""
    if shutil.which(binary) is None:
        return False
    try:
        out = subprocess.run(  # noqa: S603
            [binary, *(args or ("--version",))], capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0
