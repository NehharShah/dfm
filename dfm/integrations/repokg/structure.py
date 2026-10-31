"""Structural diff: what changed in how the repository is connected.

The delta itself is RepoKG's (`repokg.api.diff_graph`, or `repokg.diff.build`
without the patch); DFM only summarizes it and adds one judgement RepoKG cannot
make alone: new edges that cross directory roles RepoCanon identified (source
importing tests, frontend importing backend, code importing scripts or infra).

Graphs at a base ref come from RepoKG's own `refs.checkout`, a temporary
detached worktree, so the developer's working tree is never checked out over.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from dfm.integrations.repokg.provider import KG, _build, _module  # pyright: ignore[reportPrivateUsage]

# (from-role, to-role) pairs worth a warning when a new edge appears.
_SUSPICIOUS_ROLE_PAIRS = {
    ("source", "test"): "production code imports test code",
    ("backend", "test"): "production code imports test code",
    ("api", "test"): "production code imports test code",
    ("frontend", "test"): "production code imports test code",
    ("frontend", "backend"): "frontend imports backend code directly",
    ("backend", "frontend"): "backend imports frontend code",
    ("source", "scripts"): "code imports a scripts directory",
    ("source", "infra"): "code imports infrastructure definitions",
    ("source", "deployment"): "code imports deployment definitions",
    ("source", "docs"): "code imports from docs",
}


@dataclass(frozen=True)
class BoundaryWarning:
    edge: str
    reason: str


@dataclass(frozen=True)
class StructuralDelta:
    raw: dict[str, Any]
    shape_changed: bool
    reasons: list[str]
    added_modules: list[str]
    removed_modules: list[str]
    renamed_modules: list[str]
    added_edges: list[str]
    removed_edges: list[str]
    ops_changes: list[str]
    boundary_warnings: list[BoundaryWarning] = field(default_factory=lambda: [])

    @property
    def empty(self) -> bool:
        return not (
            self.added_modules
            or self.removed_modules
            or self.added_edges
            or self.removed_edges
            or self.ops_changes
        )

    def markdown(self) -> str:
        if self.empty:
            return "No structural change (modules, import edges and ops surface unchanged)."
        lines = ["Structural change: " + ", ".join(self.reasons)]
        for title, items in (
            ("New modules", self.added_modules),
            ("Removed modules", self.removed_modules),
            ("Moved/renamed modules", self.renamed_modules),
            ("New dependencies", self.added_edges),
            ("Removed dependencies", self.removed_edges),
            ("Ops surface", self.ops_changes),
        ):
            if items:
                lines.append(f"\n{title}:")
                lines.extend(f"- {item}" for item in items)
        if self.boundary_warnings:
            lines.append("\nArchitecture warnings:")
            lines.extend(f"- {w.edge}: {w.reason}" for w in self.boundary_warnings)
        return "\n".join(lines)


def _diff(before: KG, after: KG) -> dict[str, Any]:
    try:
        api = _module("repokg.api")
        return cast(dict[str, Any], api.diff_graph(before, after))
    except ImportError:
        return cast(dict[str, Any], _module("repokg.diff").build(before, after))


def _edge(e: dict[str, Any]) -> str:
    return f"{e['from']} -> {e['to']}"


def structural_diff(before: KG, after: KG, roles: dict[str, str] | None = None) -> StructuralDelta:
    """`roles`: directory path -> RepoCanon role_kind, for boundary warnings."""
    delta = _diff(before, after)
    diff_mod = _module("repokg.diff")
    modules = cast(dict[str, list[dict[str, Any]]], delta.get("modules", {}))
    edges = cast(dict[str, list[dict[str, Any]]], delta.get("edges", {}))
    ops_changes: list[str] = []
    for kind, section in cast(dict[str, dict[str, list[Any]]], delta.get("ops", {})).items():
        for verb in ("added", "removed", "changed"):
            for item in section.get(verb, []):
                if isinstance(item, dict):
                    entry = cast(dict[str, Any], item)
                    label = entry.get("file") or entry.get("path") or entry.get("name")
                else:
                    label = item
                ops_changes.append(f"{verb} {kind}: {label}")
    added_edges = [_edge(e) for e in edges.get("added", [])]
    warnings: list[BoundaryWarning] = []
    for e in edges.get("added", []):
        pair = (_role(str(e["from"]), roles) or "", _role(str(e["to"]), roles) or "")
        reason = _SUSPICIOUS_ROLE_PAIRS.get(pair)
        if reason:
            warnings.append(BoundaryWarning(_edge(e), reason))
    return StructuralDelta(
        raw=delta,
        shape_changed=bool(diff_mod.shape_changed(delta)),
        reasons=[str(r) for r in diff_mod.shape_reasons(delta)],
        added_modules=[str(m["path"]) for m in modules.get("added", [])],
        removed_modules=[str(m["path"]) for m in modules.get("removed", [])],
        renamed_modules=[
            f"{r['from']} -> {r['to']} ({r.get('confidence', '?')})"
            for r in modules.get("renamed", [])
        ],
        added_edges=added_edges,
        removed_edges=[_edge(e) for e in edges.get("removed", [])],
        ops_changes=ops_changes,
        boundary_warnings=warnings,
    )


def _role(module: str, roles: dict[str, str] | None) -> str | None:
    if not roles:
        return None
    best: str | None = None
    best_len = -1
    for path, role in roles.items():
        root = path.rstrip("/")
        if (module == root or module.startswith(root + "/")) and len(root) > best_len:
            best, best_len = role, len(root)
    return best


def roles_from_canon(canon: Any) -> dict[str, str]:
    """RepoCanon `key_directories` → {path: role_kind}; empty for degraded results."""
    dirs = cast(list[Any], getattr(canon, "key_directories", None) or [])
    return {str(getattr(d, "path", "")): str(getattr(d, "role_kind", "")) for d in dirs}


def _graph_at_ref_sync(repo: Path, ref: str) -> KG:
    refs = _module("repokg.refs")
    with refs.checkout(str(repo), ref) as tree:
        return _build(Path(tree), None, False)


async def graph_at_ref(repo: Path, ref: str = "HEAD") -> KG | None:
    """The graph as of `ref`, built in a temporary detached worktree (removed afterwards)."""
    try:
        return await asyncio.to_thread(_graph_at_ref_sync, repo.resolve(), ref)
    except Exception:  # RepoKG raises RuntimeError/OSError for missing refs, shallow clones, ...
        return None
