"""`dfm plan`: what DFM would do, without touching the repository.

Facts come from deterministic sources: RepoCanon (commands, package), RepoKG
(modules, blast radius), the test suite run in a read-only sandbox (failures).
A local model only drafts the steps, risks and open questions; without one,
DFM derives steps from the failing tests. The plan is stored in the session so
`dfm implement --from-plan <session>` can execute it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, cast

from pydantic import BaseModel, Field

from dfm.context import ContextEngine, Task, TokenBudget, mentioned_paths
from dfm.integrations.repocanon import package_for
from dfm.integrations.repocanon import summary as canon_summary
from dfm.integrations.repokg import blast_radius, module_for
from dfm.models.base import GenerateRequest, Model, ModelError
from dfm.models.structured import structured
from dfm.orchestration.agent import AgentLoop
from dfm.orchestration.runtime import Runtime
from dfm.recovery import Stage
from dfm.security.injection import DATA_RULE
from dfm.tools.results import compress


class PlanDraft(BaseModel):
    likely_files: list[str] = Field(description="Repository paths that will probably change")
    steps: list[str] = Field(description="Ordered implementation steps, each one sentence")
    verification: list[str] = Field(description="How to verify, beyond running the test suite")
    risks: list[str]
    open_questions: list[str]


@dataclass
class Plan:
    goal: str
    repository_facts: list[str]
    affected_package: str | None
    affected_modules: list[str]
    blast_radius: list[str]
    historical_practice: list[str]
    likely_files: list[str]
    steps: list[str]
    verification: list[str]
    risks: list[str]
    open_questions: list[str]
    failing_tests: list[str] = field(default_factory=lambda: [])
    drafted_by: str = "deterministic"
    blocked: str | None = None

    def render(self) -> str:
        def section(title: str, items: list[str] | str | None) -> list[str]:
            if isinstance(items, str):
                items = [items]
            return [f"## {title}", *(f"- {i}" for i in (items or ["(none)"])), ""]

        lines = [f"# Plan: {self.goal}", ""]
        if self.blocked:
            lines += [f"**Blocked:** {self.blocked}", ""]
        lines += section("Goal", self.goal)
        lines += section("Repository facts", self.repository_facts)
        lines += section("Affected package", self.affected_package or "(single package)")
        lines += section(
            "Affected modules",
            self.affected_modules + [f"blast radius: {b}" for b in self.blast_radius],
        )
        lines += section("Relevant historical practice", self.historical_practice)
        lines += section("Likely files", self.likely_files)
        lines += section("Implementation steps", self.steps)
        lines += section("Verification plan", self.verification)
        lines += section("Risks", self.risks)
        lines += section("Open questions", self.open_questions)
        lines.append(f"_Steps drafted by: {self.drafted_by}. Facts: RepoCanon, RepoKG, test run._")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _model_for_planning(rt: Runtime, coder: Model | None) -> tuple[Model | None, int]:
    if coder is not None:
        return coder, 6000
    try:
        return rt.pool.coder("small"), 6000
    except ModelError:
        helper = rt.pool.helper()
        return helper, 2500  # Apple FM: ~8k tokens in total


async def plan(rt: Runtime, request: str, *, coder: Model | None = None) -> Plan:
    rt.event("user_request", text=request)
    loop = AgentLoop(rt, read_only=True, services=False)  # a plan starts nothing
    blocked = await loop.analyze(request)
    intel = loop.intel
    report = loop.last_report
    commands = loop.commands

    facts: list[str] = []
    affected_package: str | None = None
    modules: list[str] = []
    radius: list[str] = []
    failing = [f"{f.id}: {f.message}" for f in (report.failed if report else [])]
    mentioned = [p for p, _ in mentioned_paths(request) if (rt.repo / p).exists()]
    failing_files = [
        f.file for f in (report.failed if report else []) if f.file and not f.file.startswith("/")
    ]
    focus = list(dict.fromkeys([*mentioned, *failing_files]))

    if intel is not None:
        summary = canon_summary(intel.canon)
        for key in ("languages", "frameworks", "package_managers", "topology", "test_layout"):
            value: Any = summary.get(key)
            if value:
                text = (
                    ", ".join(str(v) for v in cast(list[Any], value))
                    if isinstance(value, list)
                    else str(value)
                )
                facts.append(f"{key.replace('_', ' ')}: {text}")
        if focus:
            pkg = package_for(intel.canon, focus[0])
            affected_package = f"{pkg.name} ({pkg.path})" if pkg is not None else None
        if intel.graph is not None and focus:
            modules = sorted({m for p in focus if (m := module_for(intel.graph, p))})
            br = blast_radius(intel.graph, focus)
            radius = [
                f"direct dependents: {', '.join(br.direct_dependents) or '-'}",
                f"transitive: {', '.join(br.transitive_dependents) or '-'}",
            ]
        facts.extend(f"degraded: {d}" for d in intel.degraded)
    if commands is not None:
        facts.append(f"test command: {commands.test}")
        if commands.check is not None:
            facts.append(f"check command: {commands.check}")
    if report is not None:
        facts.append(f"current tests: {report.summary()}")

    verification = (
        [f"run `{commands.test}` and confirm it passes"]
        if commands and commands.test
        else ["no test command found: verify manually"]
    )
    if commands is not None and commands.check is not None:
        verification.append(f"run `{commands.check}`")
    verification.append("review `git diff`; confirm no unrelated files changed")
    if intel is not None and intel.graph is not None:
        verification.append("compare the RepoKG graph before/after for unexpected new dependencies")

    result = Plan(
        goal=request,
        repository_facts=facts,
        affected_package=affected_package,
        affected_modules=modules,
        blast_radius=radius,
        historical_practice=[],
        likely_files=focus,
        steps=[],
        verification=verification,
        risks=[],
        open_questions=[],
        failing_tests=failing,
        blocked=blocked,
    )

    practices = await rt.reflex.practices(rt.repo, request) if rt.reflex.enabled else []
    rt.event("reflex_loaded", practices=len(practices), error=rt.reflex.last_error)
    result.historical_practice = [p.line() for p in practices] or [
        f"none found ({rt.reflex.last_error})"
        if rt.reflex.last_error
        else "none recorded for this repository"
    ]

    model, budget = _model_for_planning(rt, coder)
    draft: PlanDraft | None = None
    if model is not None and not blocked:
        engine = ContextEngine(rt.env, intel)
        bundle = await engine.build(Task(request, focus_paths=focus), TokenBudget(budget))
        prompt = (
            f"Task: {request}\n\nRepository facts:\n"
            + "\n".join(f"- {f}" for f in facts)
            + (
                f"\n\nCurrent test failures:\n{compress(report)}"
                if report and not report.ok
                else ""
            )
            + f"\n\nContext:\n{bundle.render()}\n\nDraft an implementation plan. Do not write code."
        )
        rt.event("model_started", model=model.name, purpose="plan")
        try:
            draft = await structured(
                model,
                GenerateRequest.prompt(
                    prompt,
                    instructions="You plan code changes for a developer. Be specific and brief. "
                    + DATA_RULE,
                    purpose="transform",
                    timeout_s=120,
                ),
                PlanDraft,
            )
            rt.event(
                "model_finished",
                model=getattr(model, "last_used", None) or model.name,
                purpose="plan",
            )
        except ModelError as exc:
            rt.event("failure", stage="plan", error=f"{type(exc).__name__}: {exc}")
    if draft is not None:
        result.drafted_by = getattr(model, "last_used", None) or (model.name if model else "model")
        known = {p for p in draft.likely_files if (rt.repo / p).exists()}
        result.likely_files = list(dict.fromkeys([*focus, *sorted(known)]))
        result.steps = draft.steps
        result.verification = list(dict.fromkeys([*verification, *draft.verification]))
        result.risks = draft.risks
        result.open_questions = draft.open_questions
    else:
        for f in report.failed if report else []:
            where = f" at {f.file}:{f.line}" if f.file else ""
            result.steps.append(
                f"Make `{f.id}` pass: inspect the failure{where} ({f.message[:80]}) and fix the code it exercises"
            )
        if not result.steps:
            result.steps = [
                "Locate the code named in the request (search, RepoKG modules)",
                "Make the smallest change",
                "Add or update a test",
            ]
        result.open_questions.append("No local model was available to draft detailed steps.")
    rt.store.append(rt.session, "plan", plan=result.to_dict())
    loop.checkpointer.save(Stage.PLAN, plan=result.to_dict())
    return result


def load_plan(rt: Runtime, session_id: str) -> Plan | None:
    record = rt.store.load(session_id)
    for event in reversed(record.events):
        if event["type"] == "plan":
            return Plan(**event["plan"])
    return None
