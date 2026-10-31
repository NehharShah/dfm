"""What every language adapter provides.

An adapter turns repository facts (RepoCanon first) into concrete argv lists
for format / check / test / lint, picks the right result parser, and names the
LSP server. It never runs anything itself: commands go through CommandRunner.
"""

from __future__ import annotations

import functools
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

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
