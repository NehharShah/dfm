"""Routing evidence from past sessions.

Local inference costs no money, so routing optimizes for reliability and speed:
for each task class, how often did dfm-small and dfm-primary finish the task?
The evidence is DFM's own session history (the same traces Reflex ingests).

Bounded influence: it only moves a decision between coder tiers, never turns code
work into a non-coding route, never overrides an explicit user choice, and always
says why in the decision's reasons.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from dfm.routing.router import RouteDecision, RoutingContext
from dfm.sessions import SessionStore, repo_hash

_CLASSES: tuple[tuple[str, str], ...] = (
    ("ci", r"\b(ci|pipeline|workflow|github actions)\b"),
    (
        "tests",
        r"\b(failing|broken|flaky)\s+tests?\b|\btests?\s+(are\s+)?failing\b|\bmake .*tests? pass\b",
    ),
    ("refactor", r"\b(refactor|restructure|migrate|rename|extract)\b"),
    ("bugfix", r"\b(fix|bug|crash|error|race|regression|broken)\b"),
    ("feature", r"\b(implement|add|support|create|build|introduce)\b"),
)


def task_class(request: str) -> str:
    text = request.lower()
    for name, pattern in _CLASSES:
        if re.search(pattern, text):
            return name
    return "other"


@dataclass
class TierStats:
    sessions: int = 0
    completed: int = 0

    @property
    def rate(self) -> float:
        return self.completed / self.sessions if self.sessions else 0.0

    def describe(self) -> str:
        return f"{self.completed}/{self.sessions}"


class RoutingEvidence:
    def __init__(
        self, store: SessionStore, repo: Path | None = None, *, min_samples: int = 3
    ) -> None:
        self.store = store
        self.repo_hash = repo_hash(repo) if repo is not None else None
        self.min_samples = min_samples
        self._stats: dict[tuple[str, str], TierStats] | None = None

    def stats(self) -> dict[tuple[str, str], TierStats]:
        if self._stats is not None:
            return self._stats
        table: dict[tuple[str, str], TierStats] = {}
        for summary in self.store.list_sessions():
            try:
                record = self.store.load(summary.id)
            except (OSError, ValueError):
                continue
            header = record.header
            if header.get("mode") not in {"implement", "implement-worktree"}:
                continue
            if self.repo_hash is not None and header.get("repo_hash") != self.repo_hash:
                continue
            tiers = [
                e.get("coder")
                for e in record.events
                if e["type"] == "route_decision" and e.get("coder")
            ]
            status = record.status
            if not tiers or status == "running":
                continue
            key = (task_class(str(header.get("request", ""))), str(tiers[-1]))
            entry = table.setdefault(key, TierStats())
            entry.sessions += 1
            entry.completed += status == "completed"
        self._stats = table
        return table

    def adjust(self, decision: RouteDecision, request: str, ctx: RoutingContext) -> RouteDecision:
        if decision.coder is None or decision.source == "override":
            return decision
        cls = task_class(request)
        table = self.stats()
        small = table.get((cls, "small"), TierStats())
        primary = table.get((cls, "primary"), TierStats())
        small_struggles = small.sessions >= self.min_samples and small.rate < 0.5
        primary_better = primary.sessions == 0 or primary.rate >= small.rate + 0.2
        if decision.coder == "small" and small_struggles and primary_better:
            why = f"history: dfm-small finished {small.describe()} {cls} tasks"
            if primary.sessions:
                why += f", dfm-primary {primary.describe()}"
            return decision.with_coder("primary", why, "reflex")
        small_reliable = small.sessions >= max(5, self.min_samples) and small.rate >= 0.8
        small_as_good = primary.sessions < self.min_samples or small.rate >= primary.rate
        if decision.coder == "primary" and small_reliable and small_as_good:
            why = f"history: dfm-small finished {small.describe()} {cls} tasks; faster"
            return decision.with_coder("small", why, "reflex")
        return decision
