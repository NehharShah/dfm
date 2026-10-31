from collections.abc import AsyncIterator

import pytest

from dfm.models import GenerateRequest, GenerateResult, ModelHealth, ModelUnavailable
from dfm.routing import Route, RouteDecision, Router, RoutingContext

R = Route

TABLE: list[tuple[str, Route, str | None]] = [
    # the routing examples from the design spec
    ("git status", R.TOOLS_ONLY, None),
    ("write a commit message for these changes", R.APPLE, None),
    ("fix the typo in the README", R.SMALL_CODER, "small"),
    ("fix the borrow checker error in src/parser.rs", R.PRIMARY_CODER, "primary"),
    ("refactor the auth subsystem to use the new session store", R.PRIMARY_CODER, "primary"),
    ("explain this repository", R.APPLE, None),
    # tools only
    ("git log --oneline -5", R.TOOLS_ONLY, None),
    ("what branch am I on", R.TOOLS_ONLY, None),
    ("show me the diff", R.TOOLS_ONLY, None),
    ("what changed since yesterday", R.TOOLS_ONLY, None),
    ("run the tests", R.TOOLS_ONLY, None),
    ("where is parse_config defined", R.TOOLS_ONLY, None),
    ("find all usages of SessionStore", R.TOOLS_ONLY, None),
    ("resume what we were doing yesterday", R.TOOLS_ONLY, None),
    ("doctor", R.TOOLS_ONLY, None),
    # Apple text tasks
    ("draft a PR description", R.APPLE, None),
    ("summarize the diff", R.APPLE, None),
    ("write release notes for v1.2", R.APPLE, None),
    ("draft an issue for the flaky login test", R.APPLE, None),
    # repo explanation
    ("what does this repo do?", R.APPLE, None),
    ("how is this project structured", R.APPLE, None),
    ("how do I test this repo", R.APPLE, None),
    # review
    ("review the current diff", R.TOOLS_THEN_CODER, "primary"),
    ("review my changes", R.TOOLS_THEN_CODER, "primary"),
    # tests / CI / investigate
    ("fix the failing tests", R.TOOLS_THEN_CODER, "small"),
    ("fix CI", R.TOOLS_THEN_CODER, "primary"),
    ("the build is red on github actions, fix it", R.TOOLS_THEN_CODER, "primary"),
    ("investigate this bug in the reconnect logic", R.TOOLS_THEN_CODER, "primary"),
    ("why does the websocket reconnect race", R.TOOLS_THEN_CODER, "primary"),
    # edits
    ("add a unit test for parse_args", R.SMALL_CODER, "small"),
    ("rename the variable cnt to count in utils.py", R.SMALL_CODER, "small"),
    ("implement issue #23", R.APPLE_THEN_CODER, "primary"),
    ("fix the reconnect race", R.PRIMARY_CODER, "primary"),
    ("optimize the hot loop in the tokenizer", R.PRIMARY_CODER, "primary"),
    ("add input validation to the signup endpoint", R.SMALL_CODER, "small"),
    ("update a.py b.py c.py d.py to the new logger", R.PRIMARY_CODER, "primary"),
    ("fix the security issue in the token check", R.PRIMARY_CODER, "primary"),
    ("run the tests and fix any failures", R.TOOLS_THEN_CODER, "small"),
    ("where is parse_config defined? rename it to load_config", R.SMALL_CODER, "small"),
    ("resume and fix the remaining failing test", R.TOOLS_THEN_CODER, "small"),
]


@pytest.mark.parametrize(("request_text", "route", "coder"), TABLE)
async def test_deterministic_table(request_text: str, route: Route, coder: str | None) -> None:
    decision = await Router().route(request_text)
    assert (decision.route, decision.coder) == (route, coder), decision
    assert decision.source == "deterministic"


def test_table_size() -> None:
    assert len(TABLE) >= 30


async def test_long_request_structured_by_apple() -> None:
    issue = "add support for exporting sessions " + "with lots of detail " * 30
    decision = await Router().route(issue)
    assert decision.route is R.APPLE_THEN_CODER


async def test_without_apple_text_tasks_go_to_small_coder() -> None:
    ctx = RoutingContext(apple_available=False)
    assert (await Router().route("write a commit message", ctx)).route is R.SMALL_CODER
    assert (await Router().route("explain this repository", ctx)).route is R.TOOLS_THEN_CODER


async def test_big_diff_skips_apple() -> None:
    decision = await Router().route("summarize the diff", RoutingContext(diff_lines=5000))
    assert decision.route is R.SMALL_CODER


async def test_failing_rust_tests_use_primary() -> None:
    ctx = RoutingContext(failing_tests=True, languages=("Rust",))
    decision = await Router().route("make the tests pass", ctx)
    assert decision.coder == "primary"


async def test_override() -> None:
    decision = await Router().route("git status", RoutingContext(override=R.PRIMARY_CODER))
    assert (decision.route, decision.source, decision.coder) == (
        R.PRIMARY_CODER,
        "override",
        "primary",
    )


class FakeHelper:
    name = "fake-apple"

    def __init__(self, text: str | None) -> None:
        self.text = text
        self.calls = 0

    async def healthcheck(self) -> ModelHealth:
        return ModelHealth(self.name, True, "ok")

    async def generate(self, request: GenerateRequest) -> GenerateResult:
        self.calls += 1
        if self.text is None:
            raise ModelUnavailable("off")
        return GenerateResult(text=self.text, model=self.name, latency_ms=1)

    async def stream(self, request: GenerateRequest) -> AsyncIterator[str]:
        yield ""


async def test_apple_asked_only_when_rules_are_inconclusive() -> None:
    helper = FakeHelper('{"route": "APPLE", "reason": "prose", "confidence": 0.9}')
    router = Router(helper)
    await router.route("git status")
    assert helper.calls == 0
    decision = await router.route("ponder the meaning of our module boundaries")
    assert helper.calls == 1
    assert (decision.route, decision.source) == (R.APPLE, "apple")


async def test_low_confidence_apple_falls_back() -> None:
    helper = FakeHelper('{"route": "APPLE", "reason": "eh", "confidence": 0.1}')
    decision = await Router(helper).route("ponder the module boundaries")
    assert decision.source == "fallback"
    assert decision.route is R.TOOLS_THEN_CODER


async def test_apple_failure_falls_back() -> None:
    decision = await Router(FakeHelper(None)).route("ponder the module boundaries")
    assert decision.source == "fallback"


async def test_adjusters_downgrade_but_never_drop_coding() -> None:
    def to_small(d: RouteDecision, request: str, ctx: RoutingContext) -> RouteDecision:
        return d.with_coder("small", "memory pressure", "resources") if d.coder == "primary" else d

    def to_tools(d: RouteDecision, request: str, ctx: RoutingContext) -> RouteDecision:
        return RouteDecision(R.TOOLS_ONLY, ("bad",), "reflex", 1.0)

    seen: list[RouteDecision] = []
    router = Router(adjusters=[to_small, to_tools], on_decision=seen.append)
    decision = await router.route("refactor the auth subsystem")
    assert decision.route is R.SMALL_CODER
    assert decision.source == "resources"
    assert "memory pressure" in decision.reasons
    assert seen == [decision]
