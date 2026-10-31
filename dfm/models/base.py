"""Provider-neutral model interface (ADR 0002).

The orchestrator only ever sees `Model`. Provider specifics (fm CLI flags, MLX
generation parameters) stay inside the adapters. Every failure is a typed
`ModelError`, so callers can fall back instead of crashing.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Role = Literal["system", "user", "assistant", "tool"]
Purpose = Literal["general", "classify", "transform", "code"]
Reasoning = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True)
class GenerateRequest:
    messages: tuple[Message, ...]
    instructions: str | None = None
    schema: dict[str, Any] | None = None  # JSON Schema for structured output
    max_tokens: int | None = None
    temperature: float | None = None  # 0 means greedy where supported
    timeout_s: float = 60.0
    stop: tuple[str, ...] = ()
    # Hint for adapters: "transform" = summarize/compress given content (Apple FM uses its
    # content-transformation guardrails for it), "classify" = short structured answer.
    purpose: Purpose = "general"
    # How hard a reasoning model thinks (its chat template's reasoning effort), and the most
    # tokens it may think before it must answer. None: the model's own default.
    reasoning: Reasoning | None = None
    thinking_budget: int | None = None
    # Native tool definitions (OpenAI-style function schemas). A model whose chat template
    # supports tools gets these instead of JSON-schema instructions and answers in its own
    # trained tool-call format; others fall back to `schema`.
    tools: tuple[dict[str, Any], ...] | None = None

    @staticmethod
    def prompt(text: str, **kw: Any) -> "GenerateRequest":
        return GenerateRequest(messages=(Message("user", text),), **kw)


@dataclass(frozen=True)
class GenerateResult:
    text: str
    model: str
    latency_ms: int
    parsed: dict[str, Any] | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    tokens_cached: int | None = None  # of tokens_in, reused from an earlier request's KV cache
    stop_reason: str | None = None  # "stop" | "length" | "loop" (cut off: it repeated itself)
    thermal_rest_s: float = 0.0  # time spent cooling the device during this call


@dataclass(frozen=True)
class ModelHealth:
    name: str
    ready: bool
    detail: str
    latency_ms: int | None = None
    extra: dict[str, Any] = field(default_factory=lambda: {})


class ModelError(Exception):
    """Base class: the model could not produce a usable answer."""


class ModelUnavailable(ModelError):
    """Not installed, disabled, not loaded, or out of memory."""


class ModelTimeout(ModelError):
    pass


class ContextLimitExceeded(ModelError):
    pass


class StructuredOutputError(ModelError):
    """Output did not parse or did not match the requested schema."""


class ModelRefusal(ModelError):
    """The model declined (e.g. Apple FM guardrails)."""


class ModelIncompatible(ModelError):
    """The local model runtime's interface changed (unknown flags, protocol drift)."""


class RemoteModelRefused(ModelError):
    """DFM never sends a request to a remote model."""


@runtime_checkable
class Model(Protocol):
    name: str

    async def healthcheck(self) -> ModelHealth: ...

    async def generate(self, request: GenerateRequest) -> GenerateResult: ...

    def stream(self, request: GenerateRequest) -> AsyncIterator[str]: ...


def render_transcript(request: GenerateRequest) -> str:
    """Flatten messages for single-prompt runtimes. System messages go to instructions."""
    turns = [m for m in request.messages if m.role != "system"]
    if len(turns) == 1 and turns[0].role == "user":
        return turns[0].content
    labels = {"user": "User", "assistant": "Assistant", "tool": "Tool output"}
    return "\n\n".join(f"{labels[m.role]}:\n{m.content}" for m in turns)


def merged_instructions(request: GenerateRequest) -> str | None:
    parts = [m.content for m in request.messages if m.role == "system"]
    if request.instructions:
        parts.insert(0, request.instructions)
    return "\n\n".join(parts) if parts else None
