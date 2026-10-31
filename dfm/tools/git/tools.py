"""Structured Git access. Reads are parsed, not passed through as text.

Local writes are deliberately narrow: `add` only stages files DFM itself
modified, `commit` only runs when the developer asked for a commit in this
session. Remote operations are never run from here; they go through the shell
classifier and policy like any command (push is always a separate, explicit step).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from dfm.execution import ExecutionEnvironment
from dfm.policy import ActionClass, Decision, Policy, ProposedAction
from dfm.processes import ProcessManager, ProcessResult
from dfm.tools.errors import ToolError


@dataclass(frozen=True)
class StatusEntry:
    path: str
    index: str  # X: staged state ('.' = unchanged)
    worktree: str  # Y: unstaged state
    kind: str  # changed | renamed | unmerged | untracked | ignored
    orig_path: str | None = None
    submodule: bool = False

    @property
    def staged(self) -> bool:
        return self.kind in {"changed", "renamed"} and self.index != "."

    @property
    def unstaged(self) -> bool:
        return self.kind in {"changed", "renamed"} and self.worktree != "."


@dataclass(frozen=True)
class GitStatus:
    branch: str | None  # None when detached
    head: str | None  # None before the first commit
    upstream: str | None
    ahead: int
    behind: int
    entries: list[StatusEntry] = field(default_factory=lambda: [])

    @property
    def detached(self) -> bool:
        return self.branch is None and self.head is not None

    @property
    def clean(self) -> bool:
        return not [e for e in self.entries if e.kind != "ignored"]

    def staged(self) -> list[str]:
        return [e.path for e in self.entries if e.staged]

    def unstaged(self) -> list[str]:
        return [e.path for e in self.entries if e.unstaged]

    def partially_staged(self) -> list[str]:
        return [e.path for e in self.entries if e.staged and e.unstaged]

    def untracked(self) -> list[str]:
        return [e.path for e in self.entries if e.kind == "untracked"]

    def conflicted(self) -> list[str]:
        return [e.path for e in self.entries if e.kind == "unmerged"]


def parse_status_v2(raw: str) -> GitStatus:
    """Parse `git status --porcelain=v2 --branch -z` output."""
    branch: str | None = None
    head: str | None = None
    upstream: str | None = None
    ahead = behind = 0
    entries: list[StatusEntry] = []
    records = raw.split("\0")
    i = 0
    while i < len(records):
        rec = records[i]
        i += 1
        if not rec:
            continue
        if rec.startswith("# branch.oid "):
            oid = rec.split(" ", 2)[2]
            head = None if oid == "(initial)" else oid
        elif rec.startswith("# branch.head "):
            name = rec.split(" ", 2)[2]
            branch = None if name == "(detached)" else name
        elif rec.startswith("# branch.upstream "):
            upstream = rec.split(" ", 2)[2]
        elif rec.startswith("# branch.ab "):
            _, _, a, b = rec.split(" ")
            ahead, behind = int(a), -int(b)
        elif rec.startswith("1 "):
            parts = rec.split(" ", 8)
            xy, sub = parts[1], parts[2]
            entries.append(
                StatusEntry(parts[8], xy[0], xy[1], "changed", submodule=sub.startswith("S"))
            )
        elif rec.startswith("2 "):
            parts = rec.split(" ", 9)
            xy, sub = parts[1], parts[2]
            orig = records[i] if i < len(records) else None
            i += 1
            entries.append(
                StatusEntry(parts[9], xy[0], xy[1], "renamed", orig, sub.startswith("S"))
            )
        elif rec.startswith("u "):
            parts = rec.split(" ", 10)
            entries.append(StatusEntry(parts[10], parts[1][0], parts[1][1], "unmerged"))
        elif rec.startswith("? "):
            entries.append(StatusEntry(rec[2:], "?", "?", "untracked"))
        elif rec.startswith("! "):
            entries.append(StatusEntry(rec[2:], "!", "!", "ignored"))
    return GitStatus(branch, head, upstream, ahead, behind, entries)


@dataclass(frozen=True)
class DiffStat:
    path: str
    added: int | None  # None for binary
    removed: int | None


@dataclass(frozen=True)
class Diff:
    files: list[DiffStat]
    text: str

    @property
    def lines_changed(self) -> int:
        return sum((f.added or 0) + (f.removed or 0) for f in self.files)


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    author: str
    date: str


class GitTools:
    def __init__(
        self,
        env: ExecutionEnvironment,
        pm: ProcessManager,
        policy: Policy,
        *,
        proposed_by: str = "agent",
    ) -> None:
        self.env = env
        self.pm = pm
        self.policy = policy
        self.proposed_by = proposed_by

    @property
    def repo(self) -> Path:
        return self.env.repo_root

    async def _git(self, *args: str, timeout: float = 60) -> ProcessResult:
        result = await self.pm.run(
            [
                "git",
                "-C",
                str(self.repo),
                "-c",
                "core.quotepath=off",
                "-c",
                "color.ui=never",
                *args,
            ],
            self.env,
            timeout=timeout,
        )
        return result

    async def _git_ok(self, *args: str) -> str:
        result = await self._git(*args)
        if result.exit_code != 0:
            raise ToolError(f"git {' '.join(args[:2])} failed: {result.stderr.strip()[:500]}")
        return result.stdout

    async def status(self) -> GitStatus:
        return parse_status_v2(
            await self._git_ok(
                "status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all"
            )
        )

    async def diff(
        self, *, staged: bool = False, ref: str | None = None, paths: tuple[str, ...] = ()
    ) -> Diff:
        base: list[str] = ["diff", "--no-ext-diff"]
        if staged:
            base.append("--cached")
        if ref is not None:
            if ref.startswith("-"):
                raise ToolError(f"invalid ref {ref!r}")
            base.append(ref)
        tail = ["--", *paths] if paths else []
        numstat = await self._git_ok(*base, "--numstat", "-z", *tail)
        text = await self._git_ok(*base, *tail)
        files: list[DiffStat] = []
        records = numstat.split("\0")
        i = 0
        while i < len(records):
            rec = records[i]
            i += 1
            if not rec.strip():
                continue
            added, removed, path = rec.split("\t", 2)
            if path == "":  # rename: old and new paths follow
                path = records[i + 1] if i + 1 < len(records) else ""
                i += 2
            files.append(
                DiffStat(
                    path,
                    None if added == "-" else int(added),
                    None if removed == "-" else int(removed),
                )
            )
        return Diff(files, text)

    async def log(self, n: int = 10, ref: str = "HEAD") -> list[Commit]:
        if ref.startswith("-"):
            raise ToolError(f"invalid ref {ref!r}")
        out = await self._git("log", f"-{max(1, n)}", "--format=%H%x1f%s%x1f%an%x1f%aI", ref, "--")
        if out.exit_code != 0:
            return []  # no commits yet
        commits: list[Commit] = []
        for line in out.stdout.splitlines():
            sha, subject, author, date = line.split("\x1f")
            commits.append(Commit(sha, subject, author, date))
        return commits

    async def show_file(self, path: str, ref: str = "HEAD") -> str:
        if ref.startswith("-") or ":" in ref:
            raise ToolError(f"invalid ref {ref!r}")
        return await self._git_ok("show", f"{ref}:{path}")

    async def add(self, paths: list[str], *, allowed: set[str]) -> None:
        """Stage files DFM modified. Never `add -A`, never partially staged user files."""
        outside = [p for p in paths if p not in allowed]
        if outside:
            raise ToolError(f"refusing to stage files DFM did not modify: {', '.join(outside)}")
        if not paths:
            return
        verdict = await self.policy.authorize(
            ProposedAction(
                "git", ActionClass.WORKTREE_WRITE, f"git add {' '.join(paths)}", self.proposed_by
            )
        )
        if verdict.decision is not Decision.ALLOW:
            raise ToolError(f"policy denied git add: {verdict.reason}")
        await self._git_ok("add", "--", *paths)

    async def commit(self, message: str, *, user_requested: bool) -> str:
        """Commit staged changes. Only when the developer asked for a commit in this session."""
        if not user_requested:
            raise ToolError("DFM commits only when you ask it to (`dfm commit`)")
        if not message.strip():
            raise ToolError("empty commit message")
        verdict = await self.policy.authorize(
            ProposedAction(
                "git",
                ActionClass.LOCAL_STATE_CHANGE,
                f"git commit -m {message.splitlines()[0]!r}",
                self.proposed_by,
            )
        )
        if verdict.decision is not Decision.ALLOW:
            raise ToolError(f"policy denied git commit: {verdict.reason}")
        await self._git_ok("commit", "-m", message)
        return (await self._git_ok("rev-parse", "HEAD")).strip()
