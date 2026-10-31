"""`dfm ask`: answer questions about the repository from deterministic facts.

RepoCanon and RepoKG supply the facts; a local helper model (Apple FM, else a
local coder) only phrases the answer and is told to say when the facts do not
cover the question. With no model at all, the facts themselves are the answer.
No edits, no commands beyond read-only analysis.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from dfm.models.base import GenerateRequest, ModelError
from dfm.models.structured import record_usage
from dfm.orchestration.resume import describe_interrupted
from dfm.orchestration.runtime import Runtime
from dfm.repository.intelligence import facts, gather, readme_excerpt
from dfm.routing import RouteDecision, RoutingContext, is_resume_request
from dfm.secrets import redact_text
from dfm.security.injection import DATA_RULE, untrusted_block

INSTRUCTIONS = (
    "You answer a developer's question about a software repository, using only the facts "
    "provided. Facts from RepoCanon and RepoKG were computed deterministically from the "
    "repository and are reliable; the README is the project's own description and may be "
    "outdated. If the facts do not answer the question, say what is missing instead of "
    "guessing. Be concise: a short paragraph, then bullet points if useful. " + DATA_RULE
)


@dataclass
class AskAnswer:
    text: str
    route: RouteDecision
    sources: str
    model: str | None
    degraded: list[str] = field(default_factory=lambda: [])


async def ask(rt: Runtime, question: str, decision: RouteDecision | None = None) -> AskAnswer:
    """`decision`: the route a caller already chose (the interactive session routes first)."""
    rt.event("user_request", text=question)
    if decision is None:
        decision = await rt.router.route(
            question, RoutingContext(apple_available=rt.pool.apple is not None)
        )
    if is_resume_request(question):
        return AskAnswer(describe_interrupted(rt), decision, "DFM session history", None)
    intel = await gather(rt.repo, rt.canon, rt.graph)
    rt.event("repocanon_loaded", state=intel.freshness.get("RepoCanon"))
    rt.event("repokg_loaded", state=intel.freshness.get("RepoKG"))
    fact_text = facts(intel)
    blocks = [untrusted_block("RepoCanon+RepoKG", fact_text, trust="deterministic")]
    readme = readme_excerpt(rt.repo)
    if readme is not None:
        blocks.append(untrusted_block(readme[0], readme[1]))
    prompt = redact_text("\n\n".join(blocks) + f"\n\nQuestion: {question}")
    rt.event(
        "context_selected",
        items=[
            {"source": "repocanon+repokg", "reason": "repository facts", "chars": len(fact_text)},
            *(
                [{"source": "repo", "path": readme[0], "reason": "project description"}]
                if readme
                else []
            ),
        ],
    )

    helper = rt.pool.helper()
    if helper is None:
        try:
            helper = rt.pool.coder("small")
        except ModelError:
            helper = None
    if helper is not None:
        rt.event("model_started", model=helper.name, purpose="ask")
        started = time.monotonic()
        try:
            result = await helper.generate(
                GenerateRequest.prompt(
                    prompt, instructions=INSTRUCTIONS, purpose="transform", timeout_s=60
                )
            )
        except ModelError as exc:
            rt.event("failure", stage="ask", error=f"{type(exc).__name__}: {exc}")
        else:
            record_usage(result)
            used = getattr(helper, "last_used", None) or result.model
            rt.event(
                "model_finished", model=used, latency_ms=int((time.monotonic() - started) * 1000)
            )
            return AskAnswer(result.text, decision, intel.provenance_line(), used, intel.degraded)
    note = "No local model available; these are the deterministic facts."
    return AskAnswer(
        f"{note}\n\n{fact_text}", decision, intel.provenance_line(), None, intel.degraded
    )
