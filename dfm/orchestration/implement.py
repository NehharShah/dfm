"""`dfm implement`: run the engineering loop on a task.

The router picks the coder tier (small/primary); a saved plan, if given, names
the files the developer agreed may change (they become editable even if they
were already dirty). With `worktree=True` the loop runs in an isolated DFM
worktree and the result comes back as a patch file the developer applies.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dfm.models.base import Model, ModelError
from dfm.orchestration.agent import AgentLoop, TaskOutcome
from dfm.orchestration.plan import load_plan
from dfm.orchestration.runtime import Runtime
from dfm.routing import RoutingContext
from dfm.tools.errors import ToolError
from dfm.tools.git import WorktreeManager
from dfm.tools.git.state import is_git_repo


@dataclass
class ImplementOptions:
    from_plan: str | None = None
    worktree: bool = False
    base: str = "HEAD"  # worktree base (a PR head for `dfm fix-pr`)
    max_steps: int = 25
    allow_generated_edits: bool = False  # edit generated output directly (explicit override)
    coder: Model | None = None  # tests / benchmarks inject a model


@dataclass
class ImplementResult:
    outcome: TaskOutcome
    patch_path: Path | None = None


async def _pick_coder(rt: Runtime, request: str, opts: ImplementOptions) -> Model | None:
    if opts.coder is not None:
        return opts.coder
    decision = await rt.router.route(
        request, RoutingContext(apple_available=rt.pool.apple is not None)
    )
    try:
        return rt.pool.coder(decision.coder or "primary")
    except ModelError:
        return None


async def implement(
    rt: Runtime, request: str, opts: ImplementOptions | None = None
) -> ImplementResult:
    opts = opts or ImplementOptions()
    planned: set[str] = set()
    if opts.from_plan:
        plan = load_plan(rt, opts.from_plan)
        if plan is None:
            raise ToolError(f"session {opts.from_plan} has no saved plan")
        planned = set(plan.likely_files)
        rt.event(
            "note", text=f"executing plan from session {opts.from_plan}", files=sorted(planned)
        )
    coder = await _pick_coder(rt, request, opts)

    if not opts.worktree:
        loop = AgentLoop(
            rt,
            coder=coder,
            max_steps=opts.max_steps,
            planned_paths=planned,
            allow_generated_edits=opts.allow_generated_edits,
        )
        return ImplementResult(await loop.run(request))

    if not is_git_repo(rt.repo):
        raise ToolError("--worktree needs a Git repository")
    manager = WorktreeManager(
        rt.repo,
        rt.dirs.data,
        rt.policy,
        session_id=rt.session.id,
        prefer=rt.loaded.config.git.prefer_worktree_for_large_tasks,
    )
    worktree = await manager.create(opts.base)
    inner = Runtime.create(
        worktree.path,
        request=request,
        mode="implement-worktree",
        overrides=rt.overrides,
        approver=rt.approver,
    )
    try:
        loop = AgentLoop(
            inner,
            coder=coder,
            max_steps=opts.max_steps,
            planned_paths=planned,
            allow_generated_edits=opts.allow_generated_edits,
        )
        outcome = await loop.run(request)
        await inner.close(outcome.session_status, outcome.summary)
        patch_dir = rt.dirs.data / "patches"
        patch_dir.mkdir(parents=True, exist_ok=True)
        patch_path = patch_dir / f"{rt.session.id}.patch"
        patch_path.write_text(manager.diff(worktree))
        outcome.risks.append(f"changes are in a patch, not your tree: git apply {patch_path}")
        rt.event(
            "note",
            text="worktree result exported as patch",
            patch=str(patch_path),
            inner_session=inner.session.id,
        )
        return ImplementResult(outcome, patch_path)
    finally:
        manager.remove(worktree)
