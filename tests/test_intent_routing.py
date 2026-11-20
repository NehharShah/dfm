"""The typed intent of a request (issue #18): what picks the read-only or the editing path."""

import ast
from pathlib import Path

import pytest

from dfm.interactive.session import InteractiveSession
from dfm.observability.metrics import summarize
from dfm.routing import Intent, Router, RoutingContext

ROOT = Path(__file__).resolve().parents[1]

# (instruction, intent, kind of turn). Terse instructions as a developer types them.
CASES = [
    ("fix failing tests", Intent.MODIFY, "task"),
    ("explain failing tests", Intent.INVESTIGATE, "question"),
    ("why are the tests failing", Intent.INVESTIGATE, "question"),
    ("what does the add function do", Intent.INVESTIGATE, "question"),
    ("how does this update the cache?", Intent.INVESTIGATE, "question"),
    ("show me how to make the tests pass", Intent.INVESTIGATE, "question"),
    ("what changed", Intent.INVESTIGATE, "question"),
    ("can you change main.py", Intent.MODIFY, "task"),
    ("can you fix the failing test?", Intent.MODIFY, "task"),
    ("refactor the parser", Intent.MODIFY, "task"),
    ("add a docstring to add()", Intent.MODIFY, "task"),
    ("debug the flaky test", Intent.MODIFY, "task"),
    ("do a rename of foo to bar", Intent.MODIFY, "task"),
    ("where is parse_config defined? rename it to load_config", Intent.MODIFY, "task"),
    ("explain the retry logic, then fix the backoff", Intent.MODIFY, "task"),
    ("git checkout main", Intent.GIT_CHANGE, "task"),
    ("switch to the develop branch", Intent.GIT_CHANGE, "task"),
    ("run the tests", Intent.RUN, "question"),
    ("review my diff", Intent.REVIEW, "review"),
    ("plan how to add caching", Intent.PLAN, "plan"),
    ("make a plan for the refactor", Intent.PLAN, "plan"),
    ("explain this repository", Intent.INVESTIGATE, "question"),
]


async def route(text: str):
    return await Router().route(text, RoutingContext(apple_available=False))


@pytest.mark.parametrize(("text", "intent", "kind"), CASES)
async def test_terse_instructions_get_the_expected_intent(
    text: str, intent: Intent, kind: str
) -> None:
    decision = await route(text)
    assert decision.effective_intent is intent
    assert InteractiveSession.classify(text, decision) == kind


async def test_a_question_that_reads_like_a_change_is_read_only_and_says_so() -> None:
    decision = await route("update the docs?")
    assert decision.ambiguous and decision.effective_intent is Intent.INVESTIGATE
    assert InteractiveSession.classify("update the docs?", decision) == "question"
    # the same words as an instruction are a change, and are not flagged
    plain = await route("update the docs")
    assert plain.effective_intent is Intent.MODIFY and not plain.ambiguous


async def test_unmatched_text_and_fallbacks_stay_read_only() -> None:
    decision = await route("hello there")
    assert decision.source == "fallback"
    assert decision.effective_intent is Intent.INVESTIGATE


async def test_a_decision_built_without_an_intent_keeps_the_old_reading() -> None:
    from dfm.routing import Route, RouteDecision

    coder = RouteDecision(Route.PRIMARY_CODER, ("x",), "apple", 0.9, "primary")
    assert coder.effective_intent is Intent.MODIFY
    assert RouteDecision(Route.APPLE, ("x",), "apple", 0.9).effective_intent is Intent.INVESTIGATE
    fallback = RouteDecision(Route.TOOLS_THEN_CODER, ("x",), "fallback", 0.4, "small")
    assert fallback.effective_intent is Intent.INVESTIGATE


async def test_an_override_does_not_need_a_prompt_to_match() -> None:
    from dfm.routing import Route

    decision = await Router().route(
        "qwerty", RoutingContext(override=Route.PRIMARY_CODER, apple_available=False)
    )
    assert decision.effective_intent is Intent.MODIFY and decision.source == "override"


def test_policy_and_the_shell_never_read_the_routing_intent() -> None:
    """An intent picks a path; it cannot loosen policy because policy cannot see it."""
    offenders: list[str] = []
    for package in ("policy", "security", "tools/shell"):
        for path in (ROOT / "dfm" / package).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                names = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else []
                )
                if any(name.startswith("dfm.routing") for name in names):
                    offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_profile_counts_the_intent_of_each_request() -> None:
    events = [
        {
            "type": "route_decision",
            "route": "PRIMARY_CODER",
            "source": "deterministic",
            "intent": "modify",
        },
        {
            "type": "route_decision",
            "route": "TOOLS_ONLY",
            "source": "deterministic",
            "intent": "investigate",
        },
        {
            "type": "route_decision",
            "route": "TOOLS_ONLY",
            "source": "deterministic",
            "intent": "modify",
        },
    ]
    summary = summarize({"session_id": "s"}, events, None, 1.0)
    assert summary["routing"] == {"modify": 2, "investigate": 1}
