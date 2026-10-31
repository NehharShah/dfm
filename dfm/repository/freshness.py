"""Is cached repository intelligence still true?

A structural fingerprint covers what RepoCanon's answers depend on: manifests,
lockfiles, CI workflows, RepoCanon's own config, and the directory layout down
to package roots. Source-file edits do not change it; adding a package does.
Git HEAD is recorded for provenance but not fingerprinted, so committing a code
change does not force re-analysis.

Caches live in DFM's data directory keyed by repo hash, never inside the
repository, so a repository cannot ship a forged cache.
"""

import hashlib
import json
import os
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from dfm.tools.git.state import head as git_head


class FreshnessState(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    REFRESHING = "refreshing"
    DEGRADED = "degraded"
    MISSING = "missing"


STRUCTURAL_NAMES = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "uv.lock",
        "poetry.lock",
        "Pipfile",
        "Pipfile.lock",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
        "tsconfig.json",
        "turbo.json",
        "nx.json",
        "lerna.json",
        "deno.json",
        "Cargo.toml",
        "Cargo.lock",
        "go.mod",
        "go.sum",
        "go.work",
        "Package.swift",
        "Package.resolved",
        "foundry.toml",
        "remappings.txt",
        "hardhat.config.ts",
        "hardhat.config.js",
        "Scarb.toml",
        "Scarb.lock",
        "Makefile",
        "justfile",
        "Taskfile.yml",
        "Dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
        "CMakeLists.txt",
        "build.gradle",
        "build.gradle.kts",
        "pom.xml",
        "Gemfile",
        "Gemfile.lock",
        "mix.exs",
        "pubspec.yaml",
        ".tool-versions",
        "rust-toolchain.toml",
        ".python-version",
        ".nvmrc",
    }
)
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".dfm",
        "node_modules",
        ".venv",
        "venv",
        "target",
        "dist",
        "build",
        ".next",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".turbo",
        "out",
        "vendor",
        ".gradle",
        ".build",
        "DerivedData",
        "coverage",
        ".tox",
    }
)
_EXTRA_DIRS = (".github/workflows", ".gitlab-ci", ".circleci", ".repocanon")
MAX_DEPTH = 3


def _file_digest(root: Path, path: Path) -> bytes:
    try:
        data = path.read_bytes()
    except OSError:
        return b""
    return f"\0{path.relative_to(root).as_posix()}\0".encode() + hashlib.sha256(data).digest()


def structural_files(repo: Path) -> list[Path]:
    """Manifest-like files up to MAX_DEPTH, plus CI and RepoCanon config."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(repo):
        rel_depth = len(Path(dirpath).relative_to(repo).parts)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if (d not in _SKIP_DIRS and not d.startswith("."))
            or d in {".github", ".circleci", ".repocanon"}
        )
        if rel_depth >= MAX_DEPTH:
            dirnames[:] = []
        found.extend(Path(dirpath) / f for f in sorted(filenames) if f in STRUCTURAL_NAMES)
    for extra in _EXTRA_DIRS:
        directory = repo / extra
        if directory.is_dir():
            found.extend(sorted(p for p in directory.rglob("*") if p.is_file()))
    return sorted(set(found))


def layout(repo: Path) -> list[str]:
    """Directory names down to depth 2 (package roots), ignoring build output."""
    out: list[str] = []
    for top in sorted(repo.iterdir()) if repo.is_dir() else []:
        if not top.is_dir() or top.name in _SKIP_DIRS or top.name.startswith("."):
            continue
        out.append(top.name + "/")
        try:
            out.extend(
                f"{top.name}/{child.name}/"
                for child in sorted(top.iterdir())
                if child.is_dir()
                and child.name not in _SKIP_DIRS
                and not child.name.startswith(".")
            )
        except OSError:
            continue
    return out


def structural_fingerprint(repo: Path, salt: Iterable[str] = ()) -> str:
    h = hashlib.sha256()
    for part in salt:
        h.update(f"salt\0{part}\0".encode())
    for path in structural_files(repo):
        h.update(_file_digest(repo, path))
    for entry in layout(repo):
        h.update(f"dir\0{entry}\0".encode())
    return h.hexdigest()


@dataclass(frozen=True)
class Snapshot:
    tool: str
    version: str
    fingerprint: str
    head: str | None
    taken_at: float
    extra: dict[str, Any]


class SnapshotCache:
    """`<data>/cache/<tool>/<repo-hash>/{snapshot.json,<payload>}`."""

    def __init__(self, cache_root: Path, tool: str, repo_hash: str) -> None:
        self.dir = cache_root / tool / repo_hash
        self.tool = tool

    def load(self) -> tuple[Snapshot, str] | None:
        try:
            meta = json.loads((self.dir / "snapshot.json").read_text())
            payload = (self.dir / "payload.json").read_text()
        except (OSError, ValueError):
            return None
        if hashlib.sha256(payload.encode()).hexdigest() != meta.get("payload_sha256"):
            return None
        return Snapshot(**meta["snapshot"]), payload

    def save(self, snapshot: Snapshot, payload: str) -> None:
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.dir / "payload.json").write_text(payload)
        meta = {
            "snapshot": asdict(snapshot),
            "payload_sha256": hashlib.sha256(payload.encode()).hexdigest(),
        }
        tmp = self.dir / "snapshot.json.tmp"
        tmp.write_text(json.dumps(meta))
        tmp.replace(self.dir / "snapshot.json")


def snapshot(tool: str, version: str, repo: Path, fingerprint: str, **extra: Any) -> Snapshot:
    return Snapshot(tool, version, fingerprint, git_head(repo), time.time(), extra)
