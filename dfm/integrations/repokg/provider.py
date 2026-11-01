"""RepoKG: how this repository is structurally connected (ADR 0005).

DFM uses RepoKG's own kg.json document (a dict, `repokg_version` 1) and answers
module-level navigation questions over it; it never builds its own code graph.

- Prefers a stable `repokg.api.build_graph` when RepoKG provides one (proposed upstream);
  falls back to `repokg.cli.build_graph` with progress captured, so stdout stays clean.
- PR lookup (network) only when the caller allows GitHub per network policy.
- RepoKG's incremental scan cache lives in DFM's data dir, never in the repository.
- Freshness: the graph is reused while Git HEAD, the dirty-file fingerprint and the
  RepoKG version are unchanged.
- Unavailable (not installed, not a Git repository, scan error) → None with a reason;
  callers fall back to LSP + text search.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import json
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any, cast

from dfm.integrations.base import IntegrationHealth
from dfm.repository.freshness import FreshnessState, SnapshotCache, snapshot
from dfm.sessions.store import repo_hash
from dfm.tools.git.state import is_git_repo, worktree_state

KG = dict[str, Any]


def _module(name: str) -> Any:
    """RepoKG is untyped; treat its modules as Any at this one boundary."""
    return importlib.import_module(name)


def repokg_available() -> bool:
    try:
        _module("repokg")
    except ImportError:
        return False
    return True


def has_public_api() -> bool:
    try:
        _module("repokg.api")
    except ImportError:
        return False
    return True


def _build(repo: Path, out: Path | None, github: bool) -> KG:
    pr_limit = 50 if github else 0
    if has_public_api():
        api = _module("repokg.api")
        return cast(
            KG,
            api.build_graph(
                str(repo), out=str(out) if out else None, github=github, pr_limit=pr_limit
            ),
        )
    cli = _module("repokg.cli")
    return cast(
        KG,
        cli.build_graph(
            str(repo),
            str(out) if out else "",
            not github,
            pr_limit,
            (),
            out is not None,
            stream=io.StringIO(),
            cache_off_note="disabled (no cache directory)",
        ),
    )


class RepoKGProvider:
    def __init__(
        self, *, enabled: bool = True, cache_root: Path | None = None, github: bool = False
    ) -> None:
        self._enabled = enabled
        self._cache_root = cache_root
        self._github = github
        self._memory: dict[Path, tuple[str, KG]] = {}
        self.last_error: str | None = None

    @property
    def usable(self) -> bool:
        return self._enabled and repokg_available()

    def _version(self) -> str:
        return metadata.version("repokg") if repokg_available() else "missing"

    def fingerprint(self, repo: Path) -> str:
        return f"{self._version()}:{worktree_state(repo).fingerprint}"

    def _disk(self, repo: Path) -> SnapshotCache | None:
        if self._cache_root is None:
            return None
        return SnapshotCache(self._cache_root, "repokg", repo_hash(repo))

    def _unavailable_reason(self, repo: Path) -> str | None:
        if not self._enabled:
            return "disabled in config"
        if not repokg_available():
            return "repokg not installed"
        if not is_git_repo(repo):
            return "not a Git repository (RepoKG needs Git)"
        return None

    async def graph(self, repo: Path) -> KG | None:
        repo = repo.resolve()
        reason = self._unavailable_reason(repo)
        if reason:
            self.last_error = reason
            return None
        fp = await asyncio.to_thread(self.fingerprint, repo)
        cached = self._memory.get(repo)
        if cached is not None and cached[0] == fp:
            return cached[1]
        disk = self._disk(repo)
        if disk is not None:
            loaded = await asyncio.to_thread(disk.load)
            if loaded is not None and loaded[0].fingerprint == fp:
                kg = cast(KG, json.loads(loaded[1]))
                self._memory[repo] = (fp, kg)
                return kg
        return await self._scan(repo, fp)

    async def refresh(self, repo: Path, *, force: bool = False) -> KG | None:
        repo = repo.resolve()
        if not force:
            return await self.graph(repo)
        if self._unavailable_reason(repo):
            return await self.graph(repo)
        return await self._scan(repo, await asyncio.to_thread(self.fingerprint, repo))

    async def _scan(self, repo: Path, fp: str) -> KG | None:
        disk = self._disk(repo)
        scan_dir = disk.dir / "scan" if disk is not None else None
        if scan_dir is not None:
            scan_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            kg = await asyncio.to_thread(_build, repo, scan_dir, self._github)
        except Exception as exc:  # RepoKG raises RuntimeError/OSError/ValueError on odd repos
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None
        self.last_error = None
        self._memory[repo] = (fp, kg)
        if disk is not None:
            await asyncio.to_thread(
                disk.save, snapshot("repokg", self._version(), repo, fp), json.dumps(kg)
            )
        return kg

    def state(self, repo: Path) -> FreshnessState:
        repo = repo.resolve()
        if self._unavailable_reason(repo):
            return FreshnessState.DEGRADED
        fp = self.fingerprint(repo)
        cached = self._memory.get(repo)
        if cached is not None:
            return FreshnessState.FRESH if cached[0] == fp else FreshnessState.STALE
        disk = self._disk(repo)
        loaded = disk.load() if disk is not None else None
        if loaded is None:
            return FreshnessState.MISSING
        return FreshnessState.FRESH if loaded[0].fingerprint == fp else FreshnessState.STALE

    def mark_stale(self, repo: Path) -> None:
        """Called after DFM edits sources; the next `graph()` rescans."""
        self._memory.pop(repo.resolve(), None)

    async def healthcheck(self) -> IntegrationHealth:
        if not self._enabled:
            return IntegrationHealth("repokg", False, None, "disabled in config")
        if not repokg_available():
            return IntegrationHealth("repokg", False, None, "not installed: LSP + search fallback")
        api = "public api" if has_public_api() else "cli.build_graph (public API patch not applied)"
        return IntegrationHealth("repokg", True, self._version(), api)


# ---------------------------------------------------------------- navigation over a kg document


def modules(kg: KG) -> list[str]:
    return [str(m["path"]) for m in kg.get("modules", [])]


def module_for(kg: KG, rel_path: str) -> str | None:
    """The module (directory) that owns a file: longest matching module path."""
    rel_path = rel_path.strip("./") if rel_path.startswith("./") else rel_path
    best: str | None = None
    for path in modules(kg):
        root = path.rstrip("/")
        contains = root in {"", "."} or rel_path == root or rel_path.startswith(root + "/")
        if contains and (best is None or len(root) > len(best)):
            best = root
    return best


def _edges(kg: KG) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], kg.get("edges", []))


def dependencies(kg: KG, module: str) -> list[str]:
    return sorted({str(e["to"]) for e in _edges(kg) if e["from"] == module and e["to"] != module})


def reverse_dependencies(kg: KG, module: str) -> list[str]:
    return sorted({str(e["from"]) for e in _edges(kg) if e["to"] == module and e["from"] != module})


def neighbors(kg: KG, module: str, depth: int = 1) -> list[str]:
    """Modules within `depth` import hops in either direction."""
    seen = {module}
    frontier: deque[tuple[str, int]] = deque([(module, 0)])
    while frontier:
        current, d = frontier.popleft()
        if d >= depth:
            continue
        for nxt in dependencies(kg, current) + reverse_dependencies(kg, current):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append((nxt, d + 1))
    return sorted(seen - {module})


@dataclass(frozen=True)
class BlastRadius:
    changed: list[str]  # modules owning the changed paths
    direct_dependents: list[str]
    transitive_dependents: list[str]
    unmapped_paths: list[str] = field(default_factory=lambda: [])
    ops: dict[str, list[str]] = field(default_factory=lambda: {})


def blast_radius(kg: KG, paths: Iterable[str]) -> BlastRadius:
    changed: set[str] = set()
    unmapped: list[str] = []
    for path in paths:
        module = module_for(kg, path)
        if module is None:
            unmapped.append(path)
        else:
            changed.add(module)
    direct = {d for m in changed for d in reverse_dependencies(kg, m)} - changed
    transitive: set[str] = set()
    frontier = list(direct)
    while frontier:
        current = frontier.pop()
        for dependent in reverse_dependencies(kg, current):
            if dependent not in changed and dependent not in direct and dependent not in transitive:
                transitive.add(dependent)
                frontier.append(dependent)
    ops = {m: ops_for(kg, m) for m in sorted(changed) if ops_for(kg, m)}
    return BlastRadius(sorted(changed), sorted(direct), sorted(transitive), unmapped, ops)


def ops_for(kg: KG, module: str) -> list[str]:
    """Ops-surface entries (workflows, dockerfiles, compose, charts, ...) located in a module."""
    out: list[str] = []
    for kind, items in cast(dict[str, Any], kg.get("ops", {})).items():
        for item in cast(list[Any], items):
            if isinstance(item, dict):
                entry = cast(dict[str, Any], item)
                path = str(entry.get("file") or entry.get("path") or "")
            else:
                path = str(item)
            if path and (module in {"", "."} or path == module or path.startswith(module + "/")):
                out.append(f"{kind}: {path}")
    return out


def findings(kg: KG) -> list[str]:
    return [f"{f.get('kind')}: {f.get('subject')}" for f in kg.get("findings", [])]


def graph_summary(kg: KG, limit: int = 40) -> dict[str, Any]:
    mods = cast(list[dict[str, Any]], kg.get("modules", []))
    ranked = sorted(mods, key=lambda m: int(m.get("loc", 0)), reverse=True)[:limit]
    return {
        "repo": cast(dict[str, Any], kg.get("repo", {})).get("name"),
        "head": str(cast(dict[str, Any], kg.get("repo", {})).get("head", ""))[:12],
        "languages": [
            f"{lang['lang']} ({lang['files']} files)" for lang in kg.get("languages", [])
        ],
        "modules": [f"{m['path']} [{m.get('lang')}, {m.get('files')} files]" for m in ranked],
        "edges": [f"{e['from']} -> {e['to']} ({e.get('count', 1)})" for e in _edges(kg)[:limit]],
        "ops": {
            k: len(cast(list[Any], v))
            for k, v in cast(dict[str, Any], kg.get("ops", {})).items()
            if v
        },
        "findings": findings(kg)[:10],
        "uncertainty": list(kg.get("uncertainty", []))[:10],
    }


def neighborhood(kg: KG, module: str) -> dict[str, Sequence[str]]:
    return {
        "module": [module],
        "dependencies": dependencies(kg, module),
        "dependents": reverse_dependencies(kg, module),
        "neighbors (2 hops)": neighbors(kg, module, 2),
        "ops": ops_for(kg, module),
    }
