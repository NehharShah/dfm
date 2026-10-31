"""Reflex: how this developer has actually worked successfully before.

DFM asks Reflex read-only questions through its CLI (`reflex handbook --json`,
`reflex handbook ask "<task>" --json`, `reflex skills`) via the CommandRunner,
so the calls are classified, policy-checked and traced. Reflex commands that
write elsewhere (`reflex memory on/off`, `approve`, `install`) are never called.

Practices are historical evidence (authority 8): they enter model context as
`reflex` items labelled historical, never override the user, policy, the
repository or test results, and their source requests (the developer's past
prompts) are not passed on.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from dfm.integrations.base import IntegrationHealth
from dfm.processes import ProcessResult
from dfm.provenance import PRIORITY, ContextItem

Run = Callable[[Sequence[str], Path], Awaitable[ProcessResult | None]]


@dataclass(frozen=True)
class Practice:
    id: str
    kind: str
    text: str
    when: str
    sessions: int
    last: str
    score: float = 0.0

    def line(self) -> str:
        when = f" ({self.when})" if self.when else ""
        return f"{self.text}{when}: seen in {self.sessions} session(s), last {self.last[:10]}"


def _practices(items: Any) -> list[Practice]:
    out: list[Practice] = []
    for raw in cast(list[Any], items if isinstance(items, list) else []):
        if not isinstance(raw, dict):
            continue
        item = cast(dict[str, Any], raw)
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        out.append(
            Practice(
                id=str(item.get("id", "")),
                kind=str(item.get("kind", "")),
                text=text[:300],
                when=str(item.get("when") or "")[:120],
                sessions=int(item.get("sessions") or 0),
                last=str(item.get("last") or ""),
                score=float(item.get("score") or 0.0),
            )
        )
    return out


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    keywords: tuple[str, ...]
    path: Path
    body: str

    def score(self, task: str) -> int:
        words = set(re.findall(r"[a-z][a-z0-9_-]{2,}", task.lower()))
        name_words = set(self.name.lower().split("-"))
        return len(words & set(self.keywords)) + len(words & name_words)


_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.S)


def load_skill(path: Path) -> Skill | None:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    match = _FRONTMATTER.match(text)
    if not match:
        return None
    meta, body = match.group(1), match.group(2)
    name = re.search(r"^name:\s*(.+)$", meta, re.M)
    description = re.search(r"^description:\s*\"?(.+?)\"?$", meta, re.M)
    desc = description.group(1) if description else ""
    similar = re.search(r"similar to:\s*(.+?)\.?$", desc)
    keywords = tuple(k.strip().lower() for k in similar.group(1).split(",")) if similar else ()
    return Skill(
        name.group(1).strip() if name else path.parent.name, desc, keywords, path, body.strip()
    )


class ReflexProvider:
    def __init__(
        self,
        run: Run,
        *,
        enabled: bool = True,
        binary: str = "reflex",
        use_skills: bool = True,
        skills_dir: Path | None = None,
    ) -> None:
        self._run = run
        self.enabled = enabled
        self.binary = binary
        self.use_skills = use_skills
        self.skills_dir = skills_dir or Path.home() / ".reflex" / "skills"
        self.last_error: str | None = None
        # One provider per session: the handbook (past sessions) and the answers for one task
        # do not change while it runs, so each question goes to Reflex once, not every step.
        self._answers: dict[tuple[str, ...], tuple[dict[str, Any] | None, str | None]] = {}

    def skills(self) -> list[Skill]:
        """Skills the developer approved (Reflex writes them only on `reflex approve`)."""
        if not self.enabled or not self.use_skills or not self.skills_dir.is_dir():
            return []
        found = [load_skill(p) for p in sorted(self.skills_dir.glob("*/SKILL.md"))]
        return [s for s in found if s is not None]

    def matching_skills(self, task: str, *, limit: int = 2, min_score: int = 2) -> list[Skill]:
        scored = [(s.score(task), s) for s in self.skills()]
        return [s for score, s in sorted(scored, key=lambda x: -x[0]) if score >= min_score][:limit]

    async def _json(self, args: list[str], repo: Path) -> dict[str, Any] | None:
        if not self.enabled:
            self.last_error = "disabled in config"
            return None
        key = (str(repo), *args)
        if key not in self._answers:
            value = await self._ask(args, repo)
            self._answers[key] = (value, self.last_error)
        value, self.last_error = self._answers[key]
        return value

    async def _ask(self, args: list[str], repo: Path) -> dict[str, Any] | None:
        result = await self._run([self.binary, *args], repo)
        if result is None:
            self.last_error = "refused by policy"
            return None
        if result.exit_code != 0:
            self.last_error = (result.stderr or result.stdout).strip()[
                :300
            ] or f"exit {result.exit_code}"
            return None
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError:
            self.last_error = "Reflex returned non-JSON output"
            return None
        self.last_error = None
        return cast(dict[str, Any], value) if isinstance(value, dict) else None

    async def practices(
        self, repo: Path, task: str | None = None, *, limit: int = 8
    ) -> list[Practice]:
        """Practices relevant to `task` first (Reflex's own matching), then the repo's handbook."""
        found: dict[str, Practice] = {}
        if task:
            answered = await self._json(["handbook", "ask", task[:500], "--json"], repo)
            for p in _practices((answered or {}).get("answers")):
                found.setdefault(p.id or p.text, p)
        handbook = await self._json(["handbook", "--json"], repo)
        for p in _practices((handbook or {}).get("practices")):
            found.setdefault(p.id or p.text, p)
        ranked = sorted(found.values(), key=lambda p: (-p.score, -p.sessions))
        return ranked[:limit]

    async def healthcheck(self) -> IntegrationHealth:
        result = await self._run([self.binary, "--version"], Path.cwd())
        if result is None or result.exit_code != 0:
            return IntegrationHealth(
                "reflex", False, None, "reflex CLI not available: no historical memory"
            )
        return IntegrationHealth(
            "reflex", True, result.stdout.strip() or None, "handbook via CLI (read-only)"
        )

    def collector(self, repo: Path) -> Callable[[Any], Awaitable[list[ContextItem]]]:
        """A ContextEngine collector: practices and matching approved skills as historical
        `reflex` items (priority 6)."""

        async def collect(task: Any) -> list[ContextItem]:
            request = str(getattr(task, "request", ""))
            items: list[ContextItem] = []
            practices = await self.practices(repo, request)
            if practices:
                text = "\n".join(f"- {p.line()}" for p in practices)
                items.append(
                    ContextItem.make(
                        "reflex",
                        text,
                        "how this developer has worked before (Reflex handbook)",
                        priority=PRIORITY["reflex"],
                        confidence=0.6,
                    )
                )
            for skill in self.matching_skills(request):
                items.append(
                    ContextItem.make(
                        "reflex",
                        skill.body[:3000],
                        f"approved Reflex skill '{skill.name}'",
                        path=f"skill:{skill.name}",
                        priority=PRIORITY["reflex"],
                        confidence=0.7,
                    )
                )
            return items

        return collect
