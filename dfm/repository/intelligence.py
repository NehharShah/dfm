"""RepositoryIntelligence: RepoCanon + RepoKG (+ Reflex later) for one repository.

    canon  → what should happen?   (RepoCanon ProjectModel, or DegradedCanon)
    graph  → what is connected?    (RepoKG kg document, or None)
    reflex → what worked before?   (added by 062)

DFM composes the upstream results; it does not copy them into its own model.
Every piece records freshness or why it degraded, so answers can say how much
of what they rest on was actually available.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from dfm.integrations.repocanon import DegradedCanon, RepoCanonProvider
from dfm.integrations.repocanon import summary as canon_summary
from dfm.integrations.repokg import KG, RepoKGProvider, graph_summary
from dfm.repository.freshness import FreshnessState

README_NAMES = ("README.md", "README.rst", "README.txt", "README")


@dataclass
class RepositoryIntelligence:
    repo: Path
    canon: Any  # repocanon ProjectModel | DegradedCanon
    graph: KG | None
    reflex: Any = None
    freshness: dict[str, str] = field(default_factory=lambda: {})
    degraded: list[str] = field(default_factory=lambda: [])

    def provenance_line(self) -> str:
        parts = [f"{name} {state}" for name, state in self.freshness.items()]
        return "Sources: " + ", ".join(parts)


async def gather(
    repo: Path, canon_provider: RepoCanonProvider, graph_provider: RepoKGProvider
) -> RepositoryIntelligence:
    repo = repo.resolve()
    canon_state = canon_provider.state(repo)
    canon = await canon_provider.analyze(repo)
    graph = await graph_provider.graph(repo)
    intel = RepositoryIntelligence(repo, canon, graph)
    if isinstance(canon, DegradedCanon):
        intel.freshness["RepoCanon"] = "degraded (not installed)"
        intel.degraded.append("RepoCanon unavailable: minimal manifest discovery")
    else:
        intel.freshness["RepoCanon"] = (
            "fresh" if canon_state is not FreshnessState.STALE else "refreshed"
        )
    if graph is None:
        intel.freshness["RepoKG"] = f"unavailable ({graph_provider.last_error})"
        intel.degraded.append(f"RepoKG unavailable: {graph_provider.last_error}")
    else:
        intel.freshness["RepoKG"] = "fresh"
    return intel


def readme_excerpt(repo: Path, max_chars: int = 3000) -> tuple[str, str] | None:
    for name in README_NAMES:
        path = repo / name
        if path.is_file():
            text = path.read_text(errors="replace")
            return name, text[:max_chars]
    return None


def facts(intel: RepositoryIntelligence, max_chars: int = 9000) -> str:
    """Deterministic facts as compact text (RepoCanon first, then RepoKG)."""
    lines: list[str] = []
    canon: dict[str, Any] = canon_summary(intel.canon)

    def joined(value: Any) -> str:
        if isinstance(value, list):
            return ", ".join(str(v) for v in cast(list[Any], value))
        return str(value)

    lines.append(f"Repository: {canon.get('repo')}")
    for key, label in (
        ("languages", "Languages"),
        ("frameworks", "Frameworks/tools"),
        ("package_managers", "Package managers"),
        ("topology", "Topology"),
        ("test_layout", "Test layout"),
    ):
        value = canon.get(key)
        if value:
            lines.append(f"{label}: {joined(value)}")
    commands = cast(dict[str, Any], canon.get("commands") or {})
    if commands:
        lines.append("Commands: " + "; ".join(f"{k}: {joined(v)}" for k, v in commands.items()))
    for key, label in (
        ("packages", "Packages"),
        ("key_directories", "Key directories"),
        ("conventions", "Conventions"),
        ("boundaries", "Architecture boundaries"),
        ("uncertainty", "Uncertain"),
    ):
        items = cast(list[Any], canon.get(key) or [])
        if items:
            rendered = [_render_item(i) for i in items]
            lines.append(f"{label}: " + "; ".join(rendered[:15]))
    if intel.graph is not None:
        graph = graph_summary(intel.graph, limit=25)
        if graph["modules"]:
            lines.append("Modules (largest first): " + "; ".join(graph["modules"]))
        if graph["edges"]:
            lines.append("Internal dependencies: " + "; ".join(graph["edges"]))
        if graph["ops"]:
            lines.append("Ops surface: " + ", ".join(f"{k}: {v}" for k, v in graph["ops"].items()))
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[: max_chars - 20] + "\n[... truncated]"


def _render_item(item: Any) -> str:
    if isinstance(item, dict):
        entry = cast(dict[str, Any], item)
        return f"{entry.get('name')} ({entry.get('path')})"
    return str(item)
