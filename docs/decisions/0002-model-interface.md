# 0002 — Provider-neutral Model protocol

Status: accepted (2026-10-25)

Decision: the orchestrator depends only on `dfm.models.base.Model`
(`healthcheck`, `generate`, `stream`) with `GenerateRequest`/`GenerateResult`
(see [docs/architecture.md](../architecture.md)). Adapters: `AppleFMModel` and `MLXModel` (one
instance per configured coder). Structured output is a JSON Schema on the request; each adapter
validates the result and raises `StructuredOutputError` on mismatch.

Consequences: the router and agent loop are testable with a fake `Model`, and providers can
change without touching orchestration.
