"""TypeScript / JavaScript toolchain adapter: pnpm/npm/yarn/bun, tsc, eslint,
vitest, jest, node --test, typescript-language-server.

Command choice: RepoCanon's command for the owning package (its directory), else the
package's `test` script, else the runner its dependencies name, else `node --test`.
When the test script is a plain vitest / jest / node --test call, DFM runs that tool
directly with a machine-readable reporter instead of the script's human output.
Package lifecycle scripts never run here: installs are PACKAGE_NETWORK (policy-gated).
Tools are used only when installed (node_modules/.bin or PATH): `npx tsc` without a local
TypeScript would go to the registry, which the sandbox blocks only after a long stall.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from dfm.processes import ProcessResult
from dfm.tools.languages.base import (
    LspServerSpec,
    ToolchainCommands,
    ToolCommand,
    canon_command,
    fallback_diagnostic,
    language_named,
    runs,
    which_all,
)
from dfm.tools.results import (
    Diagnostic,
    FailedTest,
    Reporter,
    TestReport,
    parse_diagnostics_text,
    parse_eslint_json,
    parse_test_output,
)

_JS_TOOLS = {"pnpm", "npm", "yarn", "bun", "npx", "bunx", "vitest", "jest", "node", "make"}
_EXEC: dict[str, tuple[str, ...]] = {
    "pnpm": ("pnpm", "exec"),
    "npm": ("npx", "--no"),
    "yarn": ("yarn",),
    "bun": ("bunx",),
}
_LOCKFILES = (
    ("pnpm-lock.yaml", "pnpm"),
    ("bun.lock", "bun"),
    ("bun.lockb", "bun"),
    ("yarn.lock", "yarn"),
    ("package-lock.json", "npm"),
)
_ESLINT_CONFIGS = (
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    "eslint.config.ts",
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".eslintrc.yml",
)
_PRETTIER_CONFIGS = (
    ".prettierrc",
    ".prettierrc.json",
    ".prettierrc.js",
    ".prettierrc.cjs",
    ".prettierrc.mjs",
    ".prettierrc.yaml",
    ".prettierrc.yml",
    "prettier.config.js",
    "prettier.config.mjs",
)


def _package_json(directory: Path) -> dict[str, Any]:
    try:
        return cast(dict[str, Any], json.loads((directory / "package.json").read_text()))
    except (OSError, ValueError):
        return {}


def package_manager(repo: Path) -> str:
    declared = str(_package_json(repo).get("packageManager", ""))
    if declared:
        return declared.split("@", 1)[0]
    for lockfile, manager in _LOCKFILES:
        if (repo / lockfile).exists():
            return manager
    return "npm"


def package_dir(repo: Path, rel_path: str | None) -> str:
    """The nearest directory at or above `rel_path` with a package.json ("." if none)."""
    if not rel_path:
        return "."
    current = Path(rel_path)
    for parent in [current, *current.parents]:
        if parent.as_posix() not in {"", "."} and (repo / parent / "package.json").is_file():
            return parent.as_posix()
    return "."


def _dependencies(pkg: dict[str, Any]) -> set[str]:
    deps: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        deps |= set(cast(dict[str, Any], pkg.get(key, {})))
    return deps


def _escape(name: str) -> str:
    return re.escape(name)


def _tool(repo: Path, cwd: str, name: str, exec_prefix: tuple[str, ...]) -> tuple[str, ...] | None:
    """How to run an npm-provided tool: the package manager's exec for a local install,
    the bare name when it is on PATH, None when it is not installed at all."""
    for directory in (repo / cwd, repo):
        if (directory / "node_modules" / ".bin" / name).exists():
            return (*exec_prefix, name)
    return (name,) if shutil.which(name) else None


class TypeScriptAdapter:
    language = "TypeScript"
    extensions = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
    manifests = ("package.json",)

    def detect(self, repo: Path, canon: Any) -> bool:
        return (
            (repo / "package.json").exists()
            or language_named(canon, "TypeScript")
            or language_named(canon, "JavaScript")
        )

    def _structured_test(
        self, script: str, run: Callable[[str], tuple[str, ...] | None]
    ) -> tuple[tuple[str, ...], Reporter] | None:
        """The test script as a direct runner call with a machine-readable reporter."""
        if any(op in script for op in ("&&", "||", ";", "|", "$(")):
            return None
        argv = shlex.split(script)
        if not argv:
            return None
        if argv[:2] == ["node", "--test"]:
            return ("node", "--test", "--test-reporter=tap", *argv[2:]), "tap"
        prefix = run(argv[0]) if argv[0] in {"vitest", "jest"} else None
        if prefix is None:
            return None
        if argv[0] == "vitest":
            rest = [a for a in argv[1:] if a not in {"run", "--run", "watch", "dev"}]
            return (*prefix, "run", *rest, "--reporter=json"), "vitest-json"
        return (*prefix, *argv[1:], "--json"), "jest-json"

    def commands(self, repo: Path, canon: Any, rel_path: str | None = None) -> ToolchainCommands:
        notes: list[str] = []
        manager = package_manager(repo)
        exec_prefix = _EXEC.get(manager, _EXEC["npm"])
        from_canon = canon_command(canon, "test", rel_path, _JS_TOOLS)
        cwd = from_canon.cwd if from_canon is not None else package_dir(repo, rel_path)
        pkg = _package_json(repo / cwd)
        deps = _dependencies(pkg) | _dependencies(_package_json(repo))
        script = str(cast(dict[str, Any], pkg.get("scripts", {})).get("test", ""))

        def run(name: str) -> tuple[str, ...] | None:
            return _tool(repo, cwd, name, exec_prefix)

        reporter: Reporter = "auto"
        structured = self._structured_test(script, run) if script else None
        if structured is not None:
            test = ToolCommand(structured[0], cwd, "repocanon" if from_canon else "default")
            reporter = structured[1]
            notes.append(f"test script `{script}` run directly with a machine-readable reporter")
        elif from_canon is not None:
            test = ToolCommand(from_canon.argv, cwd, "repocanon")
        elif script:
            test = ToolCommand((manager, "run", "test"), cwd, "default")
        elif "vitest" in deps and (vitest := run("vitest")) is not None:
            test = ToolCommand((*vitest, "run", "--reporter=json"), cwd, "default")
            reporter = "vitest-json"
        elif "jest" in deps and (jest := run("jest")) is not None:
            test = ToolCommand((*jest, "--json"), cwd, "default")
            reporter = "jest-json"
        else:
            test = ToolCommand(("node", "--test", "--test-reporter=tap"), cwd, "default")
            reporter = "tap"
            notes.append("no test script or runner dependency: using node --test")

        def has(names: tuple[str, ...]) -> bool:
            return any((repo / cwd / n).exists() or (repo / n).exists() for n in names)

        def tool(name: str, *args: str, source: str = "config") -> ToolCommand | None:
            run = _tool(repo, cwd, name, exec_prefix)
            if run is None:
                notes.append(f"{name} is configured but not installed: skipped")
                return None
            return ToolCommand((*run, *args), cwd, source)

        check = None
        if has(("tsconfig.json",)):
            check = tool("tsc", "--noEmit", "--pretty", "false", source="default")
        lint = fmt = fmt_check = None
        if has(_ESLINT_CONFIGS) or "eslintConfig" in pkg:
            lint = tool("eslint", "-f", "json", ".")
        if has(("biome.json", "biome.jsonc")):
            lint = lint or tool("biome", "lint", ".")
            fmt = tool("biome", "format", "--write", ".")
            fmt_check = fmt and tool("biome", "format", ".")
        elif has(_PRETTIER_CONFIGS) or "prettier" in pkg:
            fmt = tool("prettier", "--write", ".")
            fmt_check = fmt and tool("prettier", "--check", ".")
        if not (repo / cwd / "node_modules").exists() and not (repo / "node_modules").exists():
            notes.append(
                f"node_modules missing: run `{manager} install` first (package network, asks)"
            )
        return ToolchainCommands("TypeScript", test, check, lint, fmt, fmt_check, reporter, notes)

    def targeted_test(
        self, repo: Path, commands: ToolchainCommands, failure: FailedTest
    ) -> ToolCommand | None:
        test = commands.test
        if test is None:
            return None
        file = failure.file
        if file and test.cwd != "." and file.startswith(test.cwd + "/"):
            file = file[len(test.cwd) + 1 :]
        pattern = _escape(failure.id)
        if commands.reporter == "vitest-json":
            head = test.argv[: test.argv.index("run") + 1]
            tail = tuple(a for a in test.argv[len(head) :] if a.startswith("--reporter"))
            argv = (*head, *([file] if file else []), "-t", pattern, *tail)
        elif commands.reporter == "jest-json":
            argv = (*test.argv, *([file] if file else []), "-t", pattern)
        elif commands.reporter == "tap" and test.argv[:2] == ("node", "--test"):
            argv = (*test.argv, f"--test-name-pattern={pattern}", *([file] if file else []))
        else:
            return None
        return ToolCommand(argv, test.cwd, "targeted")

    def parse_tests(self, result: ProcessResult, commands: ToolchainCommands) -> TestReport:
        return parse_test_output(result, commands.reporter)

    def parse_diagnostics(self, result: ProcessResult, tool: str) -> list[Diagnostic]:
        if tool == "eslint" and result.stdout.lstrip().startswith("["):
            return parse_eslint_json(result.stdout)
        diagnostics = parse_diagnostics_text(result.stdout + "\n" + result.stderr, tool)
        if not diagnostics and result.exit_code not in (0, None):
            diagnostics.append(fallback_diagnostic(result, tool))
        return diagnostics

    def lsp_server(self) -> LspServerSpec | None:
        if runs("typescript-language-server"):
            return LspServerSpec(
                "TypeScript",
                ("typescript-language-server", "--stdio"),
                "typescript",
                self.extensions,
            )
        return None

    def tools(self) -> dict[str, str | None]:
        return which_all(
            ["node", "npm", "pnpm", "yarn", "bun", "tsc", "eslint", "typescript-language-server"]
        )
