"""Which model does the work, and what happens when one fails.

Helper chain:  Apple FM  ->  dfm-small  ->  none (callers use deterministic paths).
Coder chain:   dfm-primary -> dfm-small.

A circuit breaker takes Apple FM out of rotation after repeated outage-type
failures (unavailable, timeout, CLI drift) and re-probes after a cooldown.
Per-request failures (context too long, guardrail refusal, bad JSON) fall
through to the next model for that request only. Every fallback is reported
through `on_recovery` so the session trace records it.
"""

import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Literal

from dfm.models.base import (
    GenerateRequest,
    GenerateResult,
    Model,
    ModelError,
    ModelHealth,
    ModelIncompatible,
    ModelTimeout,
    ModelUnavailable,
)

_OUTAGE = (ModelUnavailable, ModelTimeout, ModelIncompatible)

RecoveryListener = Callable[[str, str, str], None]  # (failed model, fallback, reason)


@dataclass
class CircuitBreaker:
    threshold: int = 2
    cooldown_s: float = 120.0
    clock: Callable[[], float] = time.monotonic
    failures: int = 0
    opened_at: float | None = None
    last_reason: str = ""

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if self.clock() - self.opened_at >= self.cooldown_s:
            self.opened_at = None  # half-open: allow one probe
            self.failures = self.threshold - 1
            return False
        return True

    def success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def failure(self, reason: str, *, outage: bool) -> None:
        self.last_reason = reason
        if not outage:
            return
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = self.clock()


@dataclass
class _Slot:
    model: Model
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)


class FallbackModel:
    """A Model that tries each slot in order. Exposed as the pool's helper/coder."""

    def __init__(self, name: str, slots: list[_Slot], on_recovery: RecoveryListener | None) -> None:
        self.name = name
        self._slots = slots
        self._on_recovery = on_recovery
        self.last_used: str | None = None

    def _available(self) -> list[_Slot]:
        return [s for s in self._slots if not s.breaker.is_open]

    async def healthcheck(self) -> ModelHealth:
        for slot in self._available():
            health = await slot.model.healthcheck()
            if health.ready:
                slot.breaker.success()
                return ModelHealth(
                    self.name, True, f"{slot.model.name}: {health.detail}", health.latency_ms
                )
            slot.breaker.failure(health.detail, outage=True)
            slot.breaker.failure(health.detail, outage=True)  # unhealthy = open now
        return ModelHealth(self.name, False, "no model available")

    async def generate(self, request: GenerateRequest) -> GenerateResult:
        errors: list[str] = []
        slots = self._available()
        for i, slot in enumerate(slots):
            try:
                result = await slot.model.generate(request)
            except ModelError as exc:
                reason = f"{type(exc).__name__}: {exc}"
                slot.breaker.failure(reason, outage=isinstance(exc, _OUTAGE))
                errors.append(f"{slot.model.name}: {reason}")
                nxt = slots[i + 1].model.name if i + 1 < len(slots) else "none"
                if self._on_recovery is not None:
                    self._on_recovery(slot.model.name, nxt, reason)
                continue
            slot.breaker.success()
            self.last_used = slot.model.name
            return result
        raise ModelUnavailable("; ".join(errors) or f"{self.name}: no model available")

    async def stream(self, request: GenerateRequest) -> AsyncIterator[str]:
        # Streaming does not fall back mid-answer; use the first available model.
        slots = self._available()
        if not slots:
            raise ModelUnavailable(f"{self.name}: no model available")
        async for chunk in slots[0].model.stream(request):
            yield chunk

    def status(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for slot in self._slots:
            if slot.breaker.opened_at is not None:
                out[slot.model.name] = f"unavailable ({slot.breaker.last_reason})"
            else:
                out[slot.model.name] = "ready"
        return out


class ModelPool:
    def __init__(
        self,
        *,
        apple: Model | None = None,
        small: Model | None = None,
        primary: Model | None = None,
        on_recovery: RecoveryListener | None = None,
        breaker: Callable[[], CircuitBreaker] = CircuitBreaker,
    ) -> None:
        self.apple = apple
        self.small = small
        self.primary = primary
        self._on_recovery = on_recovery
        self._slots: dict[str, _Slot] = {
            m.name: _Slot(m, breaker()) for m in (apple, small, primary) if m is not None
        }

    def _chain(self, *models: Model | None) -> list[_Slot]:
        return [self._slots[m.name] for m in models if m is not None]

    def helper(self) -> FallbackModel | None:
        """Light text work: Apple FM, else dfm-small, else None (deterministic only)."""
        slots = self._chain(self.apple, self.small)
        return FallbackModel("helper", slots, self._on_recovery) if slots else None

    def coder(self, tier: Literal["small", "primary"] = "primary") -> FallbackModel:
        chain = (self.primary, self.small) if tier == "primary" else (self.small, self.primary)
        slots = self._chain(*chain)
        if not slots:
            raise ModelUnavailable("no local coding model configured (run `dfm setup`)")
        return FallbackModel(f"coder-{tier}", slots, self._on_recovery)

    def status(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for name, slot in self._slots.items():
            out[name] = "unavailable" if slot.breaker.is_open else "ready"
        return out
