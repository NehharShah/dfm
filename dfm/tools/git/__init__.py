"""Git status/diff parsing, guarded local operations, safety and worktrees."""

from dfm.tools.git.guard import DirtySnapshot, GitGuard, RepoState
from dfm.tools.git.tools import (
    Commit,
    Diff,
    DiffStat,
    GitStatus,
    GitTools,
    StatusEntry,
    parse_status_v2,
)
from dfm.tools.git.worktree import Worktree, WorktreeManager

__all__ = [
    "Commit",
    "Diff",
    "DiffStat",
    "DirtySnapshot",
    "GitGuard",
    "GitStatus",
    "GitTools",
    "RepoState",
    "StatusEntry",
    "Worktree",
    "WorktreeManager",
    "parse_status_v2",
]
