"""`dfm resume [session]`: continue an interrupted session.

    pick the session (latest unfinished one for this repo, or the given id)
      → refuse if its DFM process is still alive
      → latest checkpoint → reconcile with the repository → report
      → stop orphaned processes the session left behind (ownership-checked)
      → export DFM worktrees the session left behind as patches, then remove them
      → mark the old session interrupted, start a new one linked to it
      → continue the engineering loop with DFM's intact edits editable again

Approvals given in the old session do not carry over (fresh Policy).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from dfm.models.base import Model, ModelError
from dfm.orchestration.agent import AgentLoop, TaskOutcome
from dfm.orchestration.runtime import Runtime
from dfm.processes import ProcessManager
from dfm.processes.manager import pid_alive
from dfm.recovery import Checkpointer
from dfm.recovery.reconcile import Reconciliation, reconcile
from dfm.routing import RoutingContext
from dfm.sessions import SessionStore, SessionSummary
from dfm.tools.errors import ToolError
from dfm.tools.git import WorktreeManager
from dfm.tools.git.state import is_git_repo


@dataclass
class ResumeResult:
    previous: SessionSummary
    reconciliation: Reconciliation | None
    outcome: TaskOutcome | None
    summary: str
    recovered_patches: list[Path] = field(default_factory=lambda: [])


def pick_session(store: SessionStore, rt: Runtime, session_id: str | None) -> SessionSummary:
    candidates = [s for s in store.list_sessions(rt.repo) if s.id != rt.session.id]
    if session_id is not None:
        chosen = next((s for s in candidates if s.id == session_id), None)
        if chosen is None:
            raise ToolError(f"no session {session_id} for this repository")
        return chosen
    unfinished = [
        s for s in candidates if s.status == "running" and s.mode in {"implement", "resume"}
    ]
    if not unfinished:
        raise ToolError("no interrupted session to resume in this repository")
    return unfinished[0]


def describe_interrupted(rt: Runtime) -> str:
    """Read-only: what the latest interrupted session was doing (for `dfm ask "what were we doing"`)."""
    try:
        previous = pick_session(rt.store, rt, None)
    except ToolError as exc:
        return str(exc).capitalize() + "."
    lines = [f"Interrupted session {previous.id} ({previous.started_at}): {previous.request}"]
    checkpoint = Checkpointer(rt.repo, previous.id).latest()
    if checkpoint is not None:
        lines.append(f"Last checkpoint: {checkpoint.stage.value}")
        if checkpoint.modified:
            lines.append(f"Files DFM edited: {', '.join(sorted(checkpoint.modified))}")
        if checkpoint.pending:
            lines.append(f"Remaining: {', '.join(checkpoint.pending)}")
    lines.append(f"Continue it with: dfm resume {previous.id}")
    return "\n".join(lines)


def recover_worktrees(rt: Runtime, session_id: str) -> list[Path]:
    """Worktrees DFM created for `session_id` and never removed (crash mid-task).

    Their changes are exported as patches first; only DFM-registered worktrees
    under DFM's own root are removed (WorktreeManager checks both).
    """
    if not is_git_repo(rt.repo):
        return []
    manager = WorktreeManager(rt.repo, rt.dirs.data, rt.policy, session_id=rt.session.id)
    patches: list[Path] = []
    for worktree in manager.owned():
        if worktree.session_id != session_id:
            continue
        if worktree.path.exists():
            patch_dir = rt.dirs.data / "patches"
            patch_dir.mkdir(parents=True, exist_ok=True)
            patch = patch_dir / f"{session_id}-{worktree.id}-recovered.patch"
            patch.write_text(manager.diff(worktree))
            patches.append(patch)
        manager.remove(worktree)
    return patches


async def resume(
    rt: Runtime, session_id: str | None = None, *, coder: Model | None = None, max_steps: int = 25
) -> ResumeResult:
    previous = pick_session(rt.store, rt, session_id)
    record = rt.store.load(previous.id)
    if previous.status != "running":
        raise ToolError(f"session {previous.id} already ended ({previous.status})")
    pid = record.header.get("dfm_pid")
    if isinstance(pid, int) and pid_alive(pid):
        raise ToolError(f"session {previous.id} is still running (pid {pid})")

    checkpoint = Checkpointer(rt.repo, previous.id).latest()
    reconciliation = (
        reconcile(checkpoint, rt.repo, rt.pm.registry) if checkpoint is not None else None
    )
    lines = [f"Resuming {previous.id}: {previous.request}"]
    if checkpoint is not None:
        lines.append(f"Last checkpoint: {checkpoint.stage.value} (#{checkpoint.n})")
        if checkpoint.pending:
            lines.append(f"Remaining: {', '.join(checkpoint.pending)}")
    if reconciliation is not None:
        lines.append(reconciliation.report())
        for orphan in reconciliation.orphans:
            await ProcessManager(rt.pm.registry).stop(orphan.id)
    patches = recover_worktrees(rt, previous.id)
    for patch in patches:
        lines.append(f"- recovered an abandoned worktree as a patch: git apply {patch}")
    summary = "\n".join(lines)

    old = rt.store.open(previous.id, rt.repo)
    rt.store.append(
        old,
        "recovery",
        failed=previous.id,
        fallback=rt.session.id,
        reason="session interrupted; resumed",
    )
    rt.store.complete(old, "interrupted", f"resumed as {rt.session.id}")
    rt.event("note", text=f"resumed from session {previous.id}", previous=previous.id)

    if coder is None:
        decision = await rt.router.route(
            previous.request, RoutingContext(apple_available=rt.pool.apple is not None)
        )
        try:
            coder = rt.pool.coder(decision.coder or "primary")
        except ModelError:
            coder = None
    planned: set[str] = set(reconciliation.intact) if reconciliation is not None else set()
    loop = AgentLoop(rt, coder=coder, max_steps=max_steps, planned_paths=planned)
    loop.history.append(summary)
    outcome = await loop.run(previous.request)
    return ResumeResult(previous, reconciliation, outcome, summary, patches)
