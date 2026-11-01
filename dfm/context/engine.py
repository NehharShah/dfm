"""Bounded, prioritized, redacted model context.

Collectors propose `ContextItem`s; the engine sorts them by priority
(1 = user references ... 10 = broad docs), then by confidence, and fills the
token budget greedily with per-source caps. It never dumps the repository:
files are read as line ranges, big or binary files are refused, and anything
outside the filesystem boundary is dropped with a recorded reason.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dfm.execution import BoundaryViolation, ExecutionEnvironment
from dfm.integrations.repocanon import COMMAND_KINDS, boundaries, package_for, scoped_commands
from dfm.integrations.repokg import module_for, neighborhood
from dfm.provenance import PRIORITY, ContextItem
from dfm.repository.intelligence import RepositoryIntelligence, readme_excerpt
from dfm.secrets import is_secret_path, redact_diff, redact_secret_file
from dfm.tools.filesystem.kinds import STREAMABLE, FileKind, classify_file, read_range, stub

MAX_FILE_BYTES = 256 * 1024
WHOLE_FILE_LINES = 400
HEAD_LINES = 200
# Anything path-shaped: optional ../ ~/ / prefix, directories, a final name. Filtered below.
_PATH_TOKEN = re.compile(
    r"(?<![\w/.~-])((?:\.\./|~/|/)?(?:[\w.-]+/)*[\w.-]+)(?::(\d+)(?:-(\d+))?)?"
)
_SKIP_DIRS = {
    ".git",
    ".dfm",
    "node_modules",
    ".venv",
    "venv",
    "target",
    "dist",
    "build",
    "__pycache__",
}


@dataclass
class Task:
    request: str
    failures: list[ContextItem] = field(default_factory=lambda: [])
    focus_paths: list[str] = field(default_factory=lambda: [])  # from the plan / agent loop


@dataclass(frozen=True)
class TokenBudget:
    total: int
    # Share of the total any one source may take (sources not listed: no cap beyond total).
    caps: dict[str, float] = field(
        default_factory=lambda: {
            "git": 0.3,
            "repokg": 0.15,
            "repocanon": 0.1,
            "reflex": 0.1,
            "repo": 0.6,
        }
    )


@dataclass
class ContextBundle:
    items: list[ContextItem]
    dropped: list[dict[str, Any]]
    budget: int

    @property
    def tokens(self) -> int:
        return sum(i.est_tokens for i in self.items)

    def render(self) -> str:
        return "\n\n".join(item.render() for item in self.items)

    def provenance(self) -> list[dict[str, Any]]:
        return [item.provenance() for item in self.items]


Collector = Callable[[Task], Awaitable[list[ContextItem]]]


def mentioned_paths(text: str) -> list[tuple[str, tuple[int, int] | None]]:
    out: list[tuple[str, tuple[int, int] | None]] = []
    for match in _PATH_TOKEN.finditer(text):
        path = match.group(1).rstrip(".,")
        if path.startswith("http") or path.count(".") > 3:
            continue
        plain_word = "/" not in path and "." not in path
        if plain_word:
            continue
        start, end = match.group(2), match.group(3)
        lines = (int(start), int(end or start)) if start else None
        out.append((path, lines))
    return out


class ContextEngine:
    def __init__(
        self,
        env: ExecutionEnvironment,
        intel: RepositoryIntelligence | None = None,
        *,
        extra: Sequence[Collector] = (),
    ) -> None:
        self.env = env
        self.intel = intel
        self.extra = list(extra)

    # ---------------------------------------------------------------- file reading

    def read_item(
        self, rel: str, *, reason: str, priority: int, lines: tuple[int, int] | None = None
    ) -> tuple[ContextItem | None, str | None]:
        """A file (or line range) as a ContextItem, or (None, why it was not used)."""
        try:
            path = self.env.resolve(rel)
        except BoundaryViolation as exc:
            return None, f"outside boundary: {exc}"
        if not path.is_file():
            return None, "not a file"
        rel_path = (
            path.relative_to(self.env.repo_root).as_posix()
            if path.is_relative_to(self.env.repo_root)
            else str(path)
        )
        info = classify_file(path, text_limit=min(MAX_FILE_BYTES, self.env.limits.read_text_bytes))
        if info.kind in STREAMABLE and lines is not None and not is_secret_path(rel_path):
            text, start, end = read_range(
                path, max(1, lines[0] - 20), lines[1] + 20, self.env.limits.read_range_lines
            )
            body = "\n".join(f"{n:>5} {line}" for n, line in enumerate(text.split("\n"), start))
            return ContextItem.make(
                "repo", body, reason, path=rel_path, line_range=(start, end), priority=priority
            ), None
        if not info.readable:
            if info.kind in {FileKind.SQLITE, FileKind.PARQUET}:
                summary = stub(rel_path, info, path)
                return ContextItem.make(
                    "repo",
                    summary,
                    reason + " (summary, not content)",
                    path=rel_path,
                    priority=priority,
                ), None
            return None, f"{info.kind.value} ({info.size} bytes): not sent to a model"
        data = path.read_bytes()
        text = data.decode(errors="replace")
        if is_secret_path(rel_path):
            return ContextItem.make(
                "repo",
                redact_secret_file(rel_path, text),
                reason + " (secret file: keys only)",
                path=rel_path,
                priority=priority,
            ), None
        all_lines = text.splitlines()
        if lines is not None:
            start = max(1, lines[0] - 20)
            end = min(len(all_lines), lines[1] + 20)
        elif len(all_lines) <= WHOLE_FILE_LINES:
            start, end = 1, len(all_lines)
        else:
            start, end = 1, HEAD_LINES
        body = (
            "\n".join(f"{n:>5} {all_lines[n - 1]}" for n in range(start, end + 1))
            if all_lines
            else ""
        )
        if end < len(all_lines):
            body += f"\n[... {len(all_lines) - end} more lines]"
        return ContextItem.make(
            "repo", body, reason, path=rel_path, line_range=(start, end), priority=priority
        ), None

    # ---------------------------------------------------------------- collectors

    def _user_references(self, task: Task, dropped: list[dict[str, Any]]) -> list[ContextItem]:
        items: list[ContextItem] = []
        seen: set[str] = set()
        refs = mentioned_paths(task.request) + [(p, None) for p in task.focus_paths]
        for rel, lines in refs:
            if rel in seen:
                continue
            seen.add(rel)
            item, why = self.read_item(
                rel, reason="named in the request", priority=PRIORITY["user_reference"], lines=lines
            )
            if item is not None:
                items.append(item)
            elif why != "not a file":
                dropped.append({"path": rel, "reason": why})
        return items

    def _diff(self) -> list[ContextItem]:
        try:
            out = subprocess.run(  # noqa: S603
                [  # noqa: S607
                    "git",
                    "-C",
                    str(self.env.repo_root),
                    "diff",
                    "HEAD",
                    "--no-color",
                    "--no-ext-diff",
                    "--unified=3",
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        if out.returncode != 0 or not out.stdout.strip():
            return []
        return [
            ContextItem.make(  # changes to .env and other secret files keep keys, not values
                "git",
                redact_diff(out.stdout),
                "current uncommitted diff",
                priority=PRIORITY["diff"],
            )
        ]

    def _focus(self, task: Task) -> list[str]:
        paths = [p for p, _ in mentioned_paths(task.request)] + task.focus_paths
        return list(dict.fromkeys(paths))

    def _repokg(self, task: Task) -> list[ContextItem]:
        if self.intel is None or self.intel.graph is None:
            return []
        items: list[ContextItem] = []
        modules = {m for p in self._focus(task) if (m := module_for(self.intel.graph, p))}
        for module in sorted(modules):
            hood = neighborhood(self.intel.graph, module)
            text = "\n".join(f"{k}: {', '.join(v) or '-'}" for k, v in hood.items())
            items.append(
                ContextItem.make(
                    "repokg",
                    text,
                    f"module neighbourhood of {module}",
                    path=module,
                    priority=PRIORITY["repokg_module"],
                )
            )
        return items

    def _repocanon(self, task: Task) -> list[ContextItem]:
        if self.intel is None:
            return []
        canon = self.intel.canon
        focus = self._focus(task)
        anchor = focus[0] if focus else None
        lines: list[str] = []
        pkg = package_for(canon, anchor) if anchor else None
        if pkg is not None:
            lines.append(f"package: {pkg.name} ({pkg.path})")
        for kind in COMMAND_KINDS:
            cmds = scoped_commands(canon, kind, anchor)
            if cmds:
                lines.append(f"{kind}: " + "; ".join(f"{c.command} (in {c.cwd})" for c in cmds))
        lines.extend(f"boundary: {b.name}: {b.description}" for b in boundaries(canon))
        for attr in ("conventions", "naming_conventions"):
            lines.extend(f"convention: {c.name}: {c.value}" for c in getattr(canon, attr, []) or [])
        lines.extend(f"avoid: {a}" for a in getattr(canon, "anti_patterns", []) or [])
        if not lines:
            return []
        return [
            ContextItem.make(
                "repocanon",
                "\n".join(lines),
                "how this repository is built, tested and organized",
                priority=PRIORITY["repocanon"],
                confidence=0.9,
            )
        ]

    def _tests_and_neighbors(self, task: Task) -> list[ContextItem]:
        items: list[ContextItem] = []
        for rel in self._focus(task)[:3]:
            stem = Path(rel).stem
            if stem.startswith("test_") or stem.endswith(("_test", ".test", ".spec")):
                continue
            patterns = {
                f"test_{stem}.py",
                f"{stem}_test.py",
                f"{stem}_test.go",
                f"{stem}.test.ts",
                f"{stem}.spec.ts",
                f"{stem}.test.js",
                f"{stem}_test.rs",
                f"{stem}.t.sol",
            }
            for found in self._find(patterns, limit=2):
                item, _ = self.read_item(
                    found, reason=f"tests for {rel}", priority=PRIORITY["test"]
                )
                if item is not None:
                    items.append(item)
            # resolved: `../../.ssh/id_rsa` is lexically under the repo root, really not
            parent = Path(os.path.realpath(self.env.repo_root / rel)).parent
            root = Path(os.path.realpath(self.env.repo_root))
            if parent.is_dir() and (parent == root or parent.is_relative_to(root)):
                siblings = sorted(p.name for p in parent.iterdir() if p.is_file())[:40]
                items.append(
                    ContextItem.make(
                        "repo",
                        "\n".join(siblings),
                        f"files next to {rel}",
                        path=parent.relative_to(self.env.repo_root).as_posix() or ".",
                        priority=PRIORITY["neighbor"],
                        confidence=0.5,
                    )
                )
        return items

    def _find(self, names: set[str], limit: int) -> list[str]:
        """Files called one of `names`, nearest the root first. Skipped directories (.venv,
        node_modules, .git) are pruned before descent, not filtered after: on a repository with
        a populated .venv that is the difference between a few hundred entries and tens of
        thousands, three times a step."""
        found: list[str] = []
        root = self.env.repo_root
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            for name in sorted(filenames):
                if name in names:
                    found.append((Path(dirpath) / name).relative_to(root).as_posix())
                    if len(found) >= limit:
                        return found
        return found

    def _docs(self) -> list[ContextItem]:
        readme = readme_excerpt(self.env.repo_root, max_chars=2000)
        if readme is None:
            return []
        return [
            ContextItem.make(
                "repo",
                readme[1],
                "project description (may be outdated)",
                path=readme[0],
                priority=PRIORITY["docs"],
                confidence=0.6,
            )
        ]

    # ---------------------------------------------------------------- assembly

    async def build(self, task: Task, budget: TokenBudget) -> ContextBundle:
        dropped: list[dict[str, Any]] = []
        candidates: list[ContextItem] = []
        candidates += self._user_references(task, dropped)
        candidates += task.failures
        candidates += await asyncio.to_thread(self._diff)
        candidates += self._repokg(task)
        candidates += self._repocanon(task)
        for collector in self.extra:
            try:
                candidates += await collector(task)
            except Exception as exc:  # optional sources must not block context building
                dropped.append(
                    {
                        "source": getattr(collector, "__name__", "collector"),
                        "reason": f"failed: {exc}",
                    }
                )
        candidates += await asyncio.to_thread(self._tests_and_neighbors, task)
        candidates += self._docs()

        candidates.sort(key=lambda i: (i.priority, -i.confidence))
        chosen: list[ContextItem] = []
        used = 0
        per_source: dict[str, int] = {}
        seen_hashes: set[str] = set()
        for item in candidates:
            if item.content_hash in seen_hashes:
                continue
            cap = budget.caps.get(item.source)
            source_used = per_source.get(item.source, 0)
            if used + item.est_tokens > budget.total:
                dropped.append({**item.provenance(), "dropped": "over total budget"})
                continue
            if (
                cap is not None
                and source_used + item.est_tokens > int(budget.total * cap)
                and item.priority > PRIORITY["user_reference"]
            ):
                dropped.append({**item.provenance(), "dropped": f"over {item.source} cap"})
                continue
            chosen.append(item)
            seen_hashes.add(item.content_hash)
            used += item.est_tokens
            per_source[item.source] = source_used + item.est_tokens
        return ContextBundle(chosen, dropped, budget.total)
