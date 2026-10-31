"""Reconcile a checkpoint with the repository as it is now.

Never assume the repository is unchanged since DFM stopped. The developer's
changes always win: files DFM edited that were changed afterwards are handed
back (not editable on resume), and the report says so before anything continues.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from dfm.processes import ProcessRecord, ProcessRegistry
from dfm.recovery.checkpoint import Checkpoint
from dfm.tools.git.state import file_hash, worktree_state


class RepoChange(StrEnum):
    UNCHANGED = "unchanged"  # exactly as DFM left it
    DFM_EDITS_INTACT = "dfm_edits_intact"  # only unrelated files changed
    USER_CHANGED_DFM_FILES = "user_changed_dfm_files"
    HEAD_MOVED = "head_moved"
    CONFLICTING = "conflicting"  # HEAD moved and DFM's files were changed too


@dataclass
class Reconciliation:
    state: RepoChange
    head_then: str | None
    head_now: str | None
    intact: list[str] = field(default_factory=lambda: [])  # DFM edits still as DFM left them
    changed_by_user: list[str] = field(default_factory=lambda: [])
    deleted: list[str] = field(default_factory=lambda: [])
    # other files that changed meanwhile
    external_changes: list[str] = field(default_factory=lambda: [])
    orphans: list[ProcessRecord] = field(default_factory=lambda: [])

    def report(self) -> str:
        lines = [f"Repository since the last checkpoint: {self.state.value.replace('_', ' ')}"]
        if self.head_then != self.head_now:
            lines.append(f"- HEAD moved: {str(self.head_then)[:12]} -> {str(self.head_now)[:12]}")
        if self.intact:
            lines.append(f"- DFM's edits still in place: {', '.join(self.intact)}")
        if self.changed_by_user:
            lines.append(
                f"- changed after DFM edited them (yours now, not touched on resume): {', '.join(self.changed_by_user)}"
            )
        if self.deleted:
            lines.append(f"- deleted after DFM edited them: {', '.join(self.deleted)}")
        if self.external_changes:
            lines.append(
                f"- other files that changed meanwhile: {', '.join(self.external_changes[:20])}"
            )
        if self.orphans:
            lines.append(
                f"- processes left running by the interrupted session: {', '.join(f'{o.name} (pid {o.pid})' for o in self.orphans)}"
            )
        return "\n".join(lines)


def reconcile(
    checkpoint: Checkpoint, repo: Path, registry: ProcessRegistry | None = None
) -> Reconciliation:
    now = worktree_state(repo)
    result = Reconciliation(RepoChange.UNCHANGED, checkpoint.head, now.head)
    for path, change in checkpoint.modified.items():
        current = file_hash(repo / path)
        if current is None and change.after is not None:
            result.deleted.append(path)
        elif current == change.after:
            result.intact.append(path)
        else:
            result.changed_by_user.append(path)
    known = set(checkpoint.dirty) | set(checkpoint.modified)
    for path, digest in now.dirty.items():
        if path in checkpoint.modified:
            continue
        if path not in known or checkpoint.dirty.get(path) != digest:
            result.external_changes.append(path)
    for path in checkpoint.dirty:
        if path not in now.dirty and path not in checkpoint.modified:
            result.external_changes.append(path)  # committed or reverted meanwhile
    if registry is not None:
        result.orphans = [r for r in registry.orphans() if r.session_id == checkpoint.session_id]

    head_moved = checkpoint.head != now.head
    dfm_files_touched = bool(result.changed_by_user or result.deleted)
    if head_moved and dfm_files_touched:
        result.state = RepoChange.CONFLICTING
    elif head_moved:
        result.state = RepoChange.HEAD_MOVED
    elif dfm_files_touched:
        result.state = RepoChange.USER_CHANGED_DFM_FILES
    elif result.external_changes:
        result.state = RepoChange.DFM_EDITS_INTACT
    return result
