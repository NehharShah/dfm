"""Typed answers from any Model: schema in, validated pydantic object out.

One retry on malformed or invalid output, told what was wrong (the same prompt again mostly
gets the same answer), then a `StructuredOutputError`. The retry keeps the request's sampling:
forcing greedy decoding sends reasoning models into repetition loops.
Validated fields are still untrusted input: callers check them against what they
expect (e.g. a route against the Route enum, which the schema and pydantic
already enforce).
"""

import dataclasses
import json
from typing import Any, cast

from pydantic import BaseModel, ValidationError

from dfm.models.base import (
    GenerateRequest,
    GenerateResult,
    Message,
    Model,
    StructuredOutputError,
)
from dfm.models.schema import fm_schema
from dfm.observability.metrics import metric

_DECODER = json.JSONDecoder()


def first_json_object(text: str) -> dict[str, Any] | None:
    """The first complete JSON object in `text`: models wrap it in prose, or send two."""
    start = text.find("{")
    while start != -1:
        try:
            value, _ = _DECODER.raw_decode(text, start)
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if isinstance(value, dict):
            return cast(dict[str, Any], value)
        start = text.find("{", start + 1)
    return None


def _parse(result: GenerateResult) -> Any:
    if result.parsed is not None:
        return result.parsed
    if result.stop_reason == "loop":
        raise StructuredOutputError(
            f"the reply kept repeating itself and was cut off after {result.tokens_out or 0} "
            "tokens, without an answer"
        )
    text = result.text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise StructuredOutputError(f"no JSON object in output: {result.text[:200]!r}")
    value = first_json_object(text)
    if value is None:
        try:
            json.loads(text[start : end + 1])  # for the decoder's own message
        except json.JSONDecodeError as exc:
            raise StructuredOutputError(f"invalid JSON: {exc}") from exc
        raise StructuredOutputError(f"no JSON object in output: {result.text[:200]!r}")
    return value


def record_usage(result: GenerateResult) -> None:
    """Token counts (when the backend reports them) for the session's metrics."""
    if result.tokens_in:
        metric("model.tokens_in", result.tokens_in, model=result.model)
    if result.tokens_out:
        metric("model.tokens_out", result.tokens_out, model=result.model)
    if result.tokens_cached:
        metric("model.tokens_cached", result.tokens_cached, model=result.model)
    if result.stop_reason == "loop":
        metric("model.loop_cutoff", 1, model=result.model)
    if result.thermal_rest_s:
        metric("model.thermal_rest_s", result.thermal_rest_s, model=result.model)


async def structured[T: BaseModel](model: Model, request: GenerateRequest, out: type[T]) -> T:
    request = dataclasses.replace(request, schema=fm_schema(out))
    attempt_request = request
    last: Exception | None = None
    for _ in range(2):
        result: GenerateResult | None = None
        try:
            result = await model.generate(attempt_request)
            record_usage(result)
            return out.model_validate(_parse(result))
        except (StructuredOutputError, ValidationError) as exc:
            last = exc
            how = (
                "with exactly one call to one of the provided functions"
                if request.tools
                else "with exactly one JSON object that matches the schema"
            )
            correction = (
                f"That reply could not be used: {str(exc)[:400]}\n"
                f"Answer again {how}, and nothing else."
            )
            attempt_request = dataclasses.replace(
                request,
                messages=(
                    *request.messages,
                    Message("assistant", (result.text if result else "")[:1000]),
                    Message("user", correction),
                ),
            )
    raise StructuredOutputError(f"{out.__name__}: {last}") from last
