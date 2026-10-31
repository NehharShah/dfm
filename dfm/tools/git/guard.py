"""Git safety: never overwrite unrelated user work (ADR 0008).

At session start DFM snapshots every dirty file (content hash, staged vs
unstaged, untracked). Writes to a file that was already dirty are refused
unless the plan names it; LFS pointers, submodules and nested repositories are
never edited as text. Operations in progress (merge, rebase, cherry-pick,
revert, bisect) and conflicts stop the task. At the end, every dirty-at-start
file DFM did not intentionally edit must be byte-identical.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from dfm.policy import ActionClass, Decision, PolicyVerdict
from dfm.tools.git.state import file_hash
from dfm.tools.git.tools import GitStatus, parse_status_v2


def _deny(why: str) -> PolicyVerdict:
    return PolicyVerdict(Decision.DENY, ActionClass.WORKTREE_WRITE, why)


def _git(repo: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603
            ["git", "-C", str(repo), "-c", "core.quotepath=off", *args],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


@dataclass(frozen=True)
class RepoState:
    git_dir: Path | None
    in_progress: list[str]  # merge, rebase, cherry-pick, revert, bisect
    detached: bool
    conflicts: list[str]
    submodules: list[str]
    lfs_patterns: list[str]
    sparse: bool
    linked_worktree: bool
    nested_repos: list[str]

    @property
    def blocking(self) -> list[str]:
        reasons = [f"{op} in progress: finish or abort it first" for op in self.in_progress]
        if self.conflicts:
            reasons.append(f"unresolved conflicts: {', '.join(self.conflicts[:10])}")
        return reasons

    @property
    def warnings(self) -> list[str]:
        out: list[str] = []
        if self.detached:
            out.append("detached HEAD: commits would not be on a branch")
        if self.sparse:
            out.append("sparse checkout: some files are not present")
        if self.linked_worktree:
            out.append("this is a linked worktree")
        if self.nested_repos:
            out.append(f"nested repositories (not edited): {', '.join(self.nested_repos[:5])}")
        return out


@dataclass(frozen=True)
class DirtySnapshot:
    files: dict[str, str | None]  # path -> sha256 (None = deleted/absent)
    staged: list[str]
    partially_staged: list[str]
    untracked: list[str]


@dataclass
class GitGuard:
    repo: Path
    snapshot: DirtySnapshot | None = None
    state: RepoState | None = None
    planned: set[str] = field(default_factory=lambda: set())

    def inspect(self) -> RepoState:
        git_dir_raw = _git(self.repo, "rev-parse", "--absolute-git-dir")
        git_dir = Path(git_dir_raw.strip()) if git_dir_raw else None
        common_raw = _git(self.repo, "rev-parse", "--git-common-dir")
        status_raw = _git(
            self.repo, "status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all"
        )
        status = (
            parse_status_v2(status_raw)
            if status_raw is not None
            else GitStatus(None, None, None, 0, 0)
        )
        in_progress: list[str] = []
        if git_dir is not None:
            markers = {
                "merge": ["MERGE_HEAD"],
                "rebase": ["rebase-merge", "rebase-apply"],
                "cherry-pick": ["CHERRY_PICK_HEAD"],
                "revert": ["REVERT_HEAD"],
                "bisect": ["BISECT_LOG"],
            }
            in_progress = [
                op for op, names in markers.items() if any((git_dir / n).exists() for n in names)
            ]
        common = Path(common_raw.strip()) if common_raw else None
        if common is not None and not common.is_absolute():
            common = (self.repo / common).resolve()
        submodules = [e.path for e in status.entries if e.submodule]
        gitmodules = self.repo / ".gitmodules"
        if gitmodules.exists():
            for line in gitmodules.read_text(errors="replace").splitlines():
                if line.strip().startswith("path"):
                    submodules.append(line.split("=", 1)[1].strip())
        lfs: list[str] = []
        attributes = self.repo / ".gitattributes"
        if attributes.exists():
            for line in attributes.read_text(errors="replace").splitlines():
                if "filter=lfs" in line and line.split():
                    lfs.append(line.split()[0])
        sparse = (_git(self.repo, "config", "--get", "core.sparseCheckout") or "").strip() == "true"
        self.state = RepoState(
            git_dir=git_dir,
            in_progress=in_progress,
            detached=status.detached,
            conflicts=status.conflicted(),
            submodules=sorted(set(submodules)),
            lfs_patterns=lfs,
            sparse=sparse,
            linked_worktree=git_dir is not None
            and common is not None
            and git_dir.resolve() != common.resolve(),
            nested_repos=self._nested(),
        )
        return self.state

    def _nested(self, max_depth: int = 4) -> list[str]:
        found: list[str] = []
        skip = {".git", "node_modules", ".venv", "target", ".dfm"}
        for dirpath, dirnames, _ in os.walk(self.repo):
            rel = Path(dirpath).relative_to(self.repo)
            if len(rel.parts) >= max_depth:
                dirnames[:] = []
            if rel.parts and (Path(dirpath) / ".git").exists():
                found.append(rel.as_posix())
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d not in skip]
        return found

    def take_snapshot(self) -> DirtySnapshot:
        raw = _git(self.repo, "status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all")
        status = parse_status_v2(raw) if raw is not None else GitStatus(None, None, None, 0, 0)
        files = {
            e.path: file_hash(self.repo / e.path) for e in status.entries if e.kind != "ignored"
        }
        self.snapshot = DirtySnapshot(
            files, status.staged(), status.partially_staged(), status.untracked()
        )
        return self.snapshot

    def start(self) -> list[str]:
        """Inspect + snapshot at session start; returns blocking reasons (empty = OK to edit)."""
        state = self.inspect()
        self.take_snapshot()
        return state.blocking

    def plan_paths(self, paths: set[str]) -> None:
        """Files the developer-visible plan says DFM will edit (dirty ones become editable)."""
        self.planned |= paths

    def check_write(self, rel: str) -> PolicyVerdict | None:
        """FsTools guard: None = no objection."""
        state = self.state or self.inspect()
        if state.blocking:
            return _deny("; ".join(state.blocking))
        for sub in state.submodules:
            if rel == sub or rel.startswith(sub.rstrip("/") + "/"):
                return _deny(f"{rel} is inside submodule {sub}; edit it in that repository")
        for nested in state.nested_repos:
            if rel == nested or rel.startswith(nested + "/"):
                return _deny(f"{rel} is inside nested repository {nested}")
        for pattern in state.lfs_patterns:
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(Path(rel).name, pattern):
                return _deny(f"{rel} is tracked by Git LFS; not edited as text")
        snap = self.snapshot
        if snap is not None and rel in snap.files and rel not in self.planned:
            staged = " (partially staged)" if rel in snap.partially_staged else ""
            return _deny(
                f"{rel} had uncommitted changes before this session{staged}; name it in the plan to edit it"
            )
        return None

    def end_check(self, intentionally_modified: set[str]) -> list[str]:
        """Dirty-at-start files DFM did not mean to touch must be unchanged."""
        if self.snapshot is None:
            return []
        problems: list[str] = []
        for path, digest in self.snapshot.files.items():
            if path in intentionally_modified:
                continue
            if file_hash(self.repo / path) != digest:
                problems.append(f"{path} changed during the session but DFM did not edit it")
        return problems
