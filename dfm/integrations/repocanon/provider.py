# pyright: reportMissingTypeStubs=false
"""RepoCanon: how this repository is supposed to be worked in (ADR 0004).

DFM keeps RepoCanon's own `ProjectModel` and answers questions with small
functions over it; it never builds a parallel repository model. When RepoCanon
is not installed, `degraded_discovery` reads the obvious manifests and says
clearly that it is degraded.

Everything RepoCanon reports came from repository files, so it is untrusted:
a test command found here still goes through shell policy before it runs.
"""

from __future__ import annotations

import asyncio
import json
import tomllib
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from dfm.integrations.base import IntegrationHealth
from dfm.repository.freshness import (
    FreshnessState,
    SnapshotCache,
    snapshot,
    structural_fingerprint,
)
from dfm.sessions.store import repo_hash

if TYPE_CHECKING:
    from repocanon.models.project import ArchitectureBoundary, Package, ProjectModel

CommandKind = Literal["install", "build", "dev", "test", "lint", "format", "typecheck"]
COMMAND_KINDS: tuple[CommandKind, ...] = (
    "install",
    "build",
    "dev",
    "test",
    "lint",
    "format",
    "typecheck",
)


@dataclass(frozen=True)
class DegradedCanon:
    """Minimal manifest-based discovery when RepoCanon is unavailable."""

    repo_name: str
    languages: list[str]
    package_managers: list[str]
    manifests: list[str]
    commands: dict[str, list[str]]
    notes: list[str] = field(default_factory=lambda: [])
    degraded: bool = True


def repocanon_available() -> bool:
    try:
        import repocanon.analyzer  # noqa: F401  # pyright: ignore[reportUnusedImport]
    except ImportError:
        return False
    return True


class RepoCanonProvider:
    """Analyze once, then reuse until the structural fingerprint changes.

    With `cache_root`, results persist across DFM runs in DFM's own data directory.
    """

    def __init__(self, *, enabled: bool = True, cache_root: Path | None = None) -> None:
        self._enabled = enabled
        self._cache_root = cache_root
        self._memory: dict[Path, tuple[str, ProjectModel | DegradedCanon]] = {}
        self._refreshing: set[Path] = set()

    @property
    def usable(self) -> bool:
        return self._enabled and repocanon_available()

    def _salt(self) -> list[str]:
        return ["repocanon", metadata.version("repocanon") if self.usable else "degraded"]

    def fingerprint(self, repo: Path) -> str:
        return structural_fingerprint(repo, self._salt())

    def _disk(self, repo: Path) -> SnapshotCache | None:
        if self._cache_root is None:
            return None
        return SnapshotCache(self._cache_root, "repocanon", repo_hash(repo))

    async def analyze(self, repo: Path) -> ProjectModel | DegradedCanon:
        repo = repo.resolve()
        fp = await asyncio.to_thread(self.fingerprint, repo)
        cached = self._memory.get(repo)
        if cached is not None and cached[0] == fp:
            return cached[1]
        disk = self._disk(repo)
        if self.usable and disk is not None:
            loaded = await asyncio.to_thread(disk.load)
            if loaded is not None and loaded[0].fingerprint == fp:
                from repocanon.models.project import ProjectModel

                try:
                    model = ProjectModel.model_validate_json(loaded[1])
                except ValueError:
                    model = None
                if model is not None:
                    self._memory[repo] = (fp, model)
                    return model
        return await self._run(repo, fp)

    async def refresh(self, repo: Path, *, force: bool = False) -> ProjectModel | DegradedCanon:
        repo = repo.resolve()
        if not force:
            return await self.analyze(repo)
        return await self._run(repo, await asyncio.to_thread(self.fingerprint, repo))

    async def _run(self, repo: Path, fp: str) -> ProjectModel | DegradedCanon:
        self._refreshing.add(repo)
        try:
            if not self.usable:
                result: ProjectModel | DegradedCanon = degraded_discovery(repo)
            else:
                model = await asyncio.to_thread(_analyze, repo)
                disk = self._disk(repo)
                if disk is not None:
                    snap = snapshot("repocanon", self._salt()[1], repo, fp)
                    await asyncio.to_thread(disk.save, snap, model.model_dump_json())
                result = model
        finally:
            self._refreshing.discard(repo)
        self._memory[repo] = (fp, result)
        return result

    def state(self, repo: Path) -> FreshnessState:
        repo = repo.resolve()
        if not self.usable:
            return FreshnessState.DEGRADED
        if repo in self._refreshing:
            return FreshnessState.REFRESHING
        fp = self.fingerprint(repo)
        cached = self._memory.get(repo)
        if cached is not None:
            return FreshnessState.FRESH if cached[0] == fp else FreshnessState.STALE
        disk = self._disk(repo)
        loaded = disk.load() if disk is not None else None
        if loaded is None:
            return FreshnessState.MISSING
        return FreshnessState.FRESH if loaded[0].fingerprint == fp else FreshnessState.STALE

    async def healthcheck(self) -> IntegrationHealth:
        if not self._enabled:
            return IntegrationHealth("repocanon", False, None, "disabled in config")
        if not repocanon_available():
            return IntegrationHealth("repocanon", False, None, "not installed: degraded discovery")
        version = metadata.version("repocanon")
        from repocanon.models.project import ProjectModel

        schema = ProjectModel.model_fields["schema_version"].default
        supported = schema == 2
        detail = f"schema_version {schema}" + ("" if supported else " (untested by DFM)")
        return IntegrationHealth("repocanon", True, version, detail)


def _analyze(repo: Path) -> ProjectModel:
    from repocanon.analyzer import analyze_repo
    from repocanon.config import load_config

    return analyze_repo(repo, load_config(repo))


# ---------------------------------------------------------------- questions over the result


def is_degraded(result: ProjectModel | DegradedCanon) -> bool:
    return isinstance(result, DegradedCanon)


def package_for(result: ProjectModel | DegradedCanon, rel_path: str) -> Package | None:
    """The monorepo package containing `rel_path` (longest matching root)."""
    if isinstance(result, DegradedCanon):
        return None
    best: Package | None = None
    for pkg in result.monorepo_packages:
        root = pkg.path.rstrip("/")
        contains = root in {"", "."} or rel_path == root or rel_path.startswith(root + "/")
        if contains and (best is None or len(root) > len(best.path.rstrip("/"))):
            best = pkg
    return best


@dataclass(frozen=True)
class ScopedCommand:
    command: str
    cwd: str  # repo-relative directory to run it in ("." for the root)


_JS_MANAGERS = ("pnpm", "yarn", "bun")


def _workspace_manager(result: ProjectModel) -> str | None:
    names = {pm.name for pm in result.package_managers}
    return next((m for m in _JS_MANAGERS if m in names), None)


def _normalize(command: str, pkg: Package, result: ProjectModel) -> str:
    """RepoCanon 0.2.1 labels pnpm/yarn/bun workspace packages as npm; use the root's manager."""
    manager = _workspace_manager(result)
    if manager and pkg.package_manager == "npm" and command.startswith("npm "):
        return manager + command[3:]
    return command


def scoped_commands(
    result: ProjectModel | DegradedCanon, kind: CommandKind, rel_path: str | None = None
) -> list[ScopedCommand]:
    """Commands of one kind with the directory to run them in, preferring the owning package."""
    if isinstance(result, DegradedCanon):
        return [ScopedCommand(c, ".") for c in result.commands.get(kind, [])]
    if rel_path is not None:
        pkg = package_for(result, rel_path)
        if pkg is not None:
            own: list[str] = getattr(pkg.commands, kind)
            if own:
                return [ScopedCommand(_normalize(c, pkg, result), pkg.path) for c in own]
    return [ScopedCommand(c, ".") for c in getattr(result.commands, kind)]


def commands(
    result: ProjectModel | DegradedCanon, kind: CommandKind, rel_path: str | None = None
) -> list[str]:
    """Command strings only (see `scoped_commands` for where to run them)."""
    return [sc.command for sc in scoped_commands(result, kind, rel_path)]


def boundaries(result: ProjectModel | DegradedCanon) -> list[ArchitectureBoundary]:
    return [] if isinstance(result, DegradedCanon) else list(result.architecture_boundaries)


def summary(result: ProjectModel | DegradedCanon) -> dict[str, Any]:
    """A compact, JSON-friendly view for context and `dfm repo canon`."""
    if isinstance(result, DegradedCanon):
        return {
            "repo": result.repo_name,
            "degraded": True,
            "languages": result.languages,
            "package_managers": result.package_managers,
            "manifests": result.manifests,
            "commands": result.commands,
            "notes": result.notes,
        }
    return {
        "repo": result.repo_name,
        "degraded": False,
        "languages": [f"{lang.name} ({lang.file_count})" for lang in result.languages],
        "frameworks": [f.name for f in result.frameworks],
        "package_managers": [pm.name for pm in result.package_managers],
        "topology": str(result.topology),
        "packages": [{"name": p.name, "path": p.path} for p in result.monorepo_packages],
        "test_layout": str(result.test_layout),
        "commands": {k: v for k in COMMAND_KINDS if (v := getattr(result.commands, k))},
        "key_directories": [f"{d.path}: {d.role}" for d in result.key_directories],
        "conventions": [
            f"{c.name}: {c.value}" for c in result.conventions + result.naming_conventions
        ],
        "boundaries": [f"{b.name}: {b.description}" for b in result.architecture_boundaries],
        "preferred_libraries": result.preferred_libraries,
        "anti_patterns": result.anti_patterns,
        "uncertainty": result.uncertainty_notes,
        "fingerprint": result.structural_fingerprint[:16],
    }


# ---------------------------------------------------------------- degraded discovery


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _js_package_manager(repo: Path) -> str:
    for lockfile, pm in (
        ("pnpm-lock.yaml", "pnpm"),
        ("bun.lockb", "bun"),
        ("bun.lock", "bun"),
        ("yarn.lock", "yarn"),
    ):
        if (repo / lockfile).exists():
            return pm
    return "npm"


def degraded_discovery(repo: Path) -> DegradedCanon:
    languages: list[str] = []
    managers: list[str] = []
    manifests: list[str] = []
    cmds: dict[str, list[str]] = {k: [] for k in COMMAND_KINDS}

    def add(kind: CommandKind, command: str) -> None:
        if command not in cmds[kind]:
            cmds[kind].append(command)

    pyproject = repo / "pyproject.toml"
    if pyproject.exists():
        manifests.append("pyproject.toml")
        languages.append("Python")
        data = _read_toml(pyproject)
        tool = cast(dict[str, Any], data.get("tool", {}))
        uses_uv = (repo / "uv.lock").exists() or "uv" in tool
        run = "uv run " if uses_uv else ""
        managers.append("uv" if uses_uv else "pip")
        add("install", "uv sync" if uses_uv else "pip install -e .")
        if "pytest" in json.dumps(data) or (repo / "tests").is_dir():
            add("test", f"{run}pytest")
        if "ruff" in tool:
            add("lint", f"{run}ruff check .")
            add("format", f"{run}ruff format .")
        for checker in ("pyright", "mypy"):
            if checker in tool:
                add("typecheck", f"{run}{checker}")
    package_json = repo / "package.json"
    if package_json.exists():
        manifests.append("package.json")
        languages.append("TypeScript" if (repo / "tsconfig.json").exists() else "JavaScript")
        pm = _js_package_manager(repo)
        managers.append(pm)
        add("install", f"{pm} install")
        try:
            scripts = cast(dict[str, str], json.loads(package_json.read_text()).get("scripts", {}))
        except (OSError, ValueError):
            scripts = {}
        for name, kind in (
            ("test", "test"),
            ("build", "build"),
            ("dev", "dev"),
            ("lint", "lint"),
            ("format", "format"),
            ("typecheck", "typecheck"),
            ("check", "typecheck"),
        ):
            if name in scripts:
                add(cast(CommandKind, kind), f"{pm} {'test' if name == 'test' else 'run ' + name}")
    if (repo / "Cargo.toml").exists():
        manifests.append("Cargo.toml")
        languages.append("Rust")
        managers.append("cargo")
        for kind, command in (
            ("build", "cargo build"),
            ("test", "cargo test"),
            ("lint", "cargo clippy"),
            ("format", "cargo fmt"),
            ("typecheck", "cargo check"),
        ):
            add(cast(CommandKind, kind), command)
    if (repo / "go.mod").exists():
        manifests.append("go.mod")
        languages.append("Go")
        managers.append("go")
        for kind, command in (
            ("build", "go build ./..."),
            ("test", "go test ./..."),
            ("lint", "go vet ./..."),
            ("format", "gofmt -l ."),
        ):
            add(cast(CommandKind, kind), command)
    if (repo / "Package.swift").exists():
        manifests.append("Package.swift")
        languages.append("Swift")
        managers.append("swiftpm")
        add("build", "swift build")
        add("test", "swift test")
    if (repo / "foundry.toml").exists():
        manifests.append("foundry.toml")
        languages.append("Solidity")
        managers.append("forge")
        add("build", "forge build")
        add("test", "forge test")
        add("format", "forge fmt")
    if (repo / "Scarb.toml").exists():
        manifests.append("Scarb.toml")
        languages.append("Cairo")
        managers.append("scarb")
        add("build", "scarb build")
        add(
            "test",
            "snforge test" if "snforge" in (repo / "Scarb.toml").read_text() else "scarb test",
        )
    makefile = repo / "Makefile"
    if makefile.exists():
        manifests.append("Makefile")
        targets = {
            line.split(":", 1)[0].strip()
            for line in makefile.read_text(errors="replace").splitlines()
            if ":" in line and not line.startswith(("\t", " ", "#", "."))
        }
        for target, kind in (
            ("test", "test"),
            ("build", "build"),
            ("lint", "lint"),
            ("fmt", "format"),
            ("format", "format"),
        ):
            if target in targets:
                add(cast(CommandKind, kind), f"make {target}")
    notes = ["RepoCanon is not installed: commands come from minimal manifest discovery."]
    if not manifests:
        notes.append("No known manifest found.")
    return DegradedCanon(
        repo_name=repo.name,
        languages=languages,
        package_managers=managers,
        manifests=manifests,
        commands={k: v for k, v in cmds.items() if v},
        notes=notes,
    )
