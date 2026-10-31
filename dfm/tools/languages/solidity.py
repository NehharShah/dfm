"""Solidity toolchain adapter: Foundry (forge, cast, anvil) and Hardhat.

Foundry: `forge test` (text; failures carry the suite file), `forge build` for solc
diagnostics, `forge fmt --check`. Hardhat (only when installed locally): `hardhat test`
(mocha) and `hardhat compile`. Remappings come from remappings.txt and foundry.toml.

forge installs solc versions on demand from the network. Offline (the default) a
missing compiler is reported as exactly that, with the command to install it, never
as a test failure. RPC, fork, broadcast and key handling stay in the shell rules
(remote reads ask; signing and broadcasting are never automatic). Anvil runs as a
managed service, not from here.
"""

from __future__ import annotations

import contextlib
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
    parse_solc_text,
    parse_test_output,
)

_SOL_TOOLS = {"forge", "npx", "pnpm", "yarn", "make", "hardhat"}
_HARDHAT_CONFIGS = ("hardhat.config.ts", "hardhat.config.js", "hardhat.config.cjs")
# forge's messages when no solc is available: none cached, or the download was blocked
NO_COMPILER = ("no compiler versions are available", "Failed to install solc")
NO_COMPILER_HINT = (
    "solc is not installed and forge downloads it from the network: approve "
    "`forge build` with package-registry network, or install it with `svm install <version>`"
)


def remappings(repo: Path) -> dict[str, str]:
    """Import prefix → directory, from remappings.txt then foundry.toml (later wins)."""
    entries: list[str] = []
    with contextlib.suppress(OSError):
        entries += (repo / "remappings.txt").read_text().splitlines()
    try:
        with (repo / "foundry.toml").open("rb") as fh:
            profile = cast(dict[str, Any], tomllib.load(fh).get("profile", {}))
        default = cast(dict[str, Any], profile.get("default", {}))
        entries += [str(e) for e in cast(list[Any], default.get("remappings", []))]
    except (OSError, tomllib.TOMLDecodeError):
        pass
    out: dict[str, str] = {}
    for entry in entries:
        entry = entry.split(":", 1)[-1].strip()  # drop a `context:` prefix
        if "=" in entry:
            prefix, target = entry.split("=", 1)
            out[prefix.strip()] = target.strip()
    return out


class SolidityAdapter:
    language = "Solidity"
    extensions = (".sol",)
    manifests = ("foundry.toml", *_HARDHAT_CONFIGS)

    def detect(self, repo: Path, canon: Any) -> bool:
        return any((repo / m).exists() for m in self.manifests) or language_named(canon, "Solidity")

    def _hardhat(self, repo: Path) -> tuple[str, ...] | None:
        if (repo / "node_modules" / ".bin" / "hardhat").exists():
            manager = "pnpm" if (repo / "pnpm-lock.yaml").exists() else "npm"
            return ("pnpm", "exec", "hardhat") if manager == "pnpm" else ("npx", "--no", "hardhat")
        return None

    def commands(self, repo: Path, canon: Any, rel_path: str | None = None) -> ToolchainCommands:
        notes: list[str] = []
        meta = {f"remap:{k}": v for k, v in remappings(repo).items()}
        if (repo / "foundry.toml").exists():
            test = canon_command(canon, "test", rel_path, _SOL_TOOLS)
            if test is None or test.argv[:1] != ("forge",):
                test = ToolCommand(("forge", "test"), ".")
            else:
                test = ToolCommand(test.argv, test.cwd, "repocanon")
            check = ToolCommand(("forge", "build"), test.cwd)
            fmt = ToolCommand(("forge", "fmt"), test.cwd)
            fmt_check = ToolCommand(("forge", "fmt", "--check"), test.cwd)
            if not which_all(["solc"])["solc"] and not (Path.home() / ".svm").is_dir():
                notes.append(NO_COMPILER_HINT)
            return ToolchainCommands(
                "Solidity", test, check, None, fmt, fmt_check, "forge", notes, meta
            )
        hardhat = self._hardhat(repo)
        if hardhat is None:
            notes.append("Hardhat project without a local hardhat install: run the install first")
            return ToolchainCommands("Solidity", notes=notes, meta=meta)
        return ToolchainCommands(
            "Solidity",
            ToolCommand((*hardhat, "test")),
            ToolCommand((*hardhat, "compile")),
            reporter="mocha",
            notes=notes,
            meta=meta,
        )

    def targeted_test(
        self, repo: Path, commands: ToolchainCommands, failure: FailedTest
    ) -> ToolCommand | None:
        test = commands.test
        if test is None:
            return None
        if test.argv[:2] == ("forge", "test"):
            path = ("--match-path", failure.file) if failure.file else ()
            return ToolCommand(
                (*test.argv, "--match-test", f"^{re.escape(failure.id)}$", *path),
                test.cwd,
                "targeted",
            )
        if commands.reporter == "mocha":
            return ToolCommand((*test.argv, "--grep", re.escape(failure.id)), test.cwd, "targeted")
        return None

    def parse_tests(self, result: ProcessResult, commands: ToolchainCommands) -> TestReport:
        report = parse_test_output(result, commands.reporter)
        if any(marker in result.stdout + result.stderr for marker in NO_COMPILER):
            report.ran = False
            report.errors.insert(0, NO_COMPILER_HINT)
        return report

    def parse_diagnostics(self, result: ProcessResult, tool: str) -> list[Diagnostic]:
        text = result.stdout + "\n" + result.stderr
        if any(marker in text for marker in NO_COMPILER):
            return [Diagnostic("error", NO_COMPILER_HINT, tool=tool or "forge")]
        diagnostics = parse_solc_text(text, tool or "solc")
        if not diagnostics and result.exit_code not in (0, None):
            tail = [ln for ln in text.splitlines() if ln.strip()][-5:]
            diagnostics.append(Diagnostic("error", "\n".join(tail) or f"{tool} failed", tool=tool))
        return diagnostics

    def lsp_server(self) -> LspServerSpec | None:
        server = "nomicfoundation-solidity-language-server"
        if runs(server, "--version"):
            return LspServerSpec("Solidity", (server, "--stdio"), "solidity", self.extensions)
        return None

    def tools(self) -> dict[str, str | None]:
        return which_all(["forge", "cast", "anvil", "solc"])
