"""Cairo / Starknet toolchain adapter: scarb, snforge, starknet-devnet (detect).

Command choice: RepoCanon's command for the owning package, then the package's test
runner: snforge when Starknet Foundry is a dev-dependency or the `test` script calls it,
else `scarb test` (cairo-test). `scarb build` gives compiler diagnostics; `scarb fmt
--check` checks formatting. Scarb.toml `[workspace] members` locate packages. Devnets
and `sncast` (transactions, accounts) are never started or sent from here.
"""

from __future__ import annotations

import re
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
)
from dfm.tools.results import (
    Diagnostic,
    FailedTest,
    TestReport,
    parse_diagnostics_text,
    parse_test_output,
)

_CAIRO_TOOLS = {"scarb", "snforge", "make"}


def _manifest(directory: Path) -> dict[str, Any]:
    try:
        with (directory / "Scarb.toml").open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def workspace_members(repo: Path) -> dict[str, str]:
    """Package name → directory for the root package and every `[workspace] members` glob."""
    root = _manifest(repo)
    members: dict[str, str] = {}
    package = cast(dict[str, Any], root.get("package", {}))
    if "name" in package:
        members[str(package["name"])] = "."
    workspace = cast(dict[str, Any], root.get("workspace", {}))
    for pattern in cast(list[Any], workspace.get("members", [])):
        for directory in sorted(repo.glob(str(pattern))):
            name = cast(dict[str, Any], _manifest(directory).get("package", {})).get("name")
            if name:
                members[str(name)] = directory.relative_to(repo).as_posix()
    return members


def package_dir(repo: Path, rel_path: str | None) -> str:
    if not rel_path:
        return "."
    for parent in [Path(rel_path), *Path(rel_path).parents]:
        if parent.as_posix() not in {"", "."} and (repo / parent / "Scarb.toml").is_file():
            return parent.as_posix()
    return "."


def uses_snforge(manifest: dict[str, Any]) -> bool:
    dev = cast(dict[str, Any], manifest.get("dev-dependencies", {}))
    script = str(cast(dict[str, Any], manifest.get("scripts", {})).get("test", ""))
    return "snforge_std" in dev or "snforge" in script


class CairoAdapter:
    language = "Cairo"
    extensions = (".cairo",)
    manifests = ("Scarb.toml",)

    def detect(self, repo: Path, canon: Any) -> bool:
        return (repo / "Scarb.toml").exists() or language_named(canon, "Cairo")

    def commands(self, repo: Path, canon: Any, rel_path: str | None = None) -> ToolchainCommands:
        notes: list[str] = []
        from_canon = canon_command(canon, "test", rel_path, _CAIRO_TOOLS)
        cwd = from_canon.cwd if from_canon is not None else package_dir(repo, rel_path)
        manifest = _manifest(repo / cwd) or _manifest(repo)
        if from_canon is not None:
            test = ToolCommand(from_canon.argv, cwd, "repocanon")
        elif uses_snforge(manifest):
            test = ToolCommand(("snforge", "test"), cwd)
        else:
            test = ToolCommand(("scarb", "test"), cwd)
        tools = self.tools()
        missing = [t for t in ("scarb", "snforge") if t in test.argv[:1] and not tools.get(t)]
        if missing:
            notes.append(f"{missing[0]} is not installed: tests cannot run")
        return ToolchainCommands(
            "Cairo",
            test,
            ToolCommand(("scarb", "build"), cwd),
            None,
            ToolCommand(("scarb", "fmt"), cwd),
            ToolCommand(("scarb", "fmt", "--check"), cwd),
            "cairo",
            notes,
        )

    def targeted_test(
        self, repo: Path, commands: ToolchainCommands, failure: FailedTest
    ) -> ToolCommand | None:
        test = commands.test
        if test is None or test.argv[:2] != ("snforge", "test"):
            return None  # cairo-test filtering differs between Scarb versions: rerun all
        return ToolCommand((*test.argv, failure.id, "--exact"), test.cwd, "targeted")

    def parse_tests(self, result: ProcessResult, commands: ToolchainCommands) -> TestReport:
        report = parse_test_output(result, commands.reporter)
        if not report.ran:
            errors = parse_diagnostics_text(result.stdout + "\n" + result.stderr, "scarb")
            report.errors.extend(f"{d.location}: {d.message}" for d in errors[:10])
        return report

    def parse_diagnostics(self, result: ProcessResult, tool: str) -> list[Diagnostic]:
        text = result.stdout + "\n" + result.stderr
        diagnostics = [
            d
            for d in parse_diagnostics_text(text, tool or "scarb")
            if not re.match(r"could not compile", d.message)
        ]
        if not diagnostics and result.exit_code not in (0, None):
            tail = [ln for ln in text.splitlines() if ln.strip()][-5:]
            diagnostics.append(Diagnostic("error", "\n".join(tail) or f"{tool} failed", tool=tool))
        return diagnostics

    def lsp_server(self) -> LspServerSpec | None:
        if runs("scarb", "--version") and runs("scarb", "cairo-language-server", "--version"):
            return LspServerSpec(
                "Cairo", ("scarb", "cairo-language-server"), "cairo", self.extensions
            )
        return None

    def tools(self) -> dict[str, str | None]:
        return which_all(["scarb", "snforge", "starknet-devnet"])
