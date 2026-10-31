"""Read-only Git state used for checkpoints and freshness (026 adds the full tool set)."""

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitUnavailable(RuntimeError):
    pass


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(  # noqa: S603
            ["git", "-C", str(repo), "-c", "core.quotepath=off", *args],  # noqa: S607
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitUnavailable(str(exc)) from exc


def is_git_repo(repo: Path) -> bool:
    try:
        return _git(repo, "rev-parse", "--is-inside-work-tree").stdout.strip() == b"true"
    except GitUnavailable:
        return False


def head(repo: Path) -> str | None:
    """Commit id of HEAD, or None (no commits yet / not a repository)."""
    try:
        out = _git(repo, "rev-parse", "--verify", "-q", "HEAD")
    except GitUnavailable:
        return None
    return out.stdout.decode().strip() or None if out.returncode == 0 else None


def dirty_paths(repo: Path) -> list[str]:
    """Paths with staged, unstaged or untracked changes (renames give the new path)."""
    try:
        out = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    except GitUnavailable:
        return []
    if out.returncode != 0:
        return []
    entries = out.stdout.decode(errors="surrogateescape").split("\0")
    paths: list[str] = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        if len(entry) > 3:
            paths.append(entry[3:])
            if entry[0] in "RC":
                i += 1  # skip the rename source
        i += 1
    return paths


def file_hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (FileNotFoundError, IsADirectoryError):
        return None


@dataclass(frozen=True)
class WorktreeState:
    head: str | None
    dirty: dict[str, str | None]  # path -> sha256 of current content (None: deleted)

    @property
    def fingerprint(self) -> str:
        h = hashlib.sha256((self.head or "").encode())
        for path in sorted(self.dirty):
            h.update(f"\0{path}\0{self.dirty[path] or '-'}".encode())
        return h.hexdigest()[:16]


def worktree_state(repo: Path) -> WorktreeState:
    return WorktreeState(head(repo), {p: file_hash(repo / p) for p in dirty_paths(repo)})
