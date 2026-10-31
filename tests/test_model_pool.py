from collections.abc import AsyncIterator

import pytest

from dfm.models import (
    ContextLimitExceeded,
    GenerateRequest,
    GenerateResult,
    ModelError,
    ModelHealth,
    ModelIncompatible,
    ModelRefusal,
    ModelTimeout,
    ModelUnavailable,
)
from dfm.models.pool import CircuitBreaker, ModelPool
from dfm.routing import Route, Router

Step = str | ModelError


class Fake:
    def __init__(self, name: str, *behaviour: Step) -> None:
        self.name = name
        self.behaviour: list[Step] = list(behaviour)
        self.calls = 0

    async def healthcheck(self) -> ModelHealth:
        ok = not self.behaviour or not isinstance(self.behaviour[0], ModelError)
        return ModelHealth(self.name, ok, "ok" if ok else "down")

    async def generate(self, request: GenerateRequest) -> GenerateResult:
        self.calls += 1
        step = self.behaviour.pop(0) if len(self.behaviour) > 1 else self.behaviour[0]
        if isinstance(step, ModelError):
            raise step
        return GenerateResult(text=step, model=self.name, latency_ms=1)

    async def stream(self, request: GenerateRequest) -> AsyncIterator[str]:
        yield "chunk"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


REQ = GenerateRequest.prompt("hi")


async def test_helper_prefers_apple() -> None:
    pool = ModelPool(apple=Fake("apple-fm", "a"), small=Fake("dfm-small", "s"))
    helper = pool.helper()
    assert helper is not None
    assert (await helper.generate(REQ)).text == "a"


@pytest.mark.parametrize(
    "error",
    [
        ModelUnavailable("Apple Intelligence disabled"),
        ModelTimeout("slow"),
        ModelIncompatible("unknown option"),
        ContextLimitExceeded("too long"),
        ModelRefusal("guardrails"),
    ],
)
async def test_each_apple_failure_falls_back_to_small(error: ModelError) -> None:
    recoveries: list[tuple[str, str, str]] = []
    pool = ModelPool(
        apple=Fake("apple-fm", error),
        small=Fake("dfm-small", "s"),
        on_recovery=lambda failed, nxt, why: recoveries.append((failed, nxt, why)),
    )
    helper = pool.helper()
    assert helper is not None
    assert (await helper.generate(REQ)).text == "s"
    assert recoveries[0][:2] == ("apple-fm", "dfm-small")
    assert type(error).__name__ in recoveries[0][2]


async def test_no_helper_when_nothing_configured() -> None:
    assert ModelPool().helper() is None


async def test_helper_without_small_raises_typed_error() -> None:
    helper = ModelPool(apple=Fake("apple-fm", ModelUnavailable("off"))).helper()
    assert helper is not None
    with pytest.raises(ModelUnavailable):
        await helper.generate(REQ)


async def test_breaker_opens_on_outages_not_per_request_errors() -> None:
    clock = Clock()
    apple = Fake(
        "apple-fm",
        ContextLimitExceeded("x"),
        ContextLimitExceeded("x"),
        ContextLimitExceeded("x"),
        "a",
    )
    pool = ModelPool(
        apple=apple,
        small=Fake("dfm-small", "s"),
        breaker=lambda: CircuitBreaker(threshold=2, cooldown_s=60, clock=clock),
    )
    helper = pool.helper()
    assert helper is not None
    for _ in range(3):
        await helper.generate(REQ)
    assert pool.status()["apple-fm"] == "ready"  # context errors never trip the breaker

    apple.behaviour = list[Step]([ModelUnavailable("off")])
    await helper.generate(REQ)
    await helper.generate(REQ)
    assert pool.status()["apple-fm"] == "unavailable"
    calls = apple.calls
    await helper.generate(REQ)
    assert apple.calls == calls  # skipped while open
    clock.now = 61
    apple.behaviour = list[Step](["back"])
    assert (await helper.generate(REQ)).text == "back"  # half-open probe succeeds
    assert pool.status()["apple-fm"] == "ready"


async def test_coder_primary_falls_back_to_small() -> None:
    pool = ModelPool(
        primary=Fake("dfm-primary", ModelUnavailable("OOM")), small=Fake("dfm-small", "s")
    )
    coder = pool.coder("primary")
    assert (await coder.generate(REQ)).text == "s"
    assert coder.last_used == "dfm-small"


async def test_no_coder_configured() -> None:
    with pytest.raises(ModelUnavailable, match="dfm setup"):
        ModelPool(apple=Fake("apple-fm", "a")).coder()


async def test_router_survives_apple_outage() -> None:
    pool = ModelPool(apple=Fake("apple-fm", ModelUnavailable("Apple Intelligence disabled")))
    router = Router(pool.helper())
    deterministic = await router.route("git status")
    assert deterministic.route is Route.TOOLS_ONLY
    fallback = await router.route("ponder the module boundaries")
    assert fallback.source == "fallback"
