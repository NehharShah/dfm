"""Apple Foundation Model via the on-device `fm` CLI (ADR 0007).

Only flags verified against the installed CLI are used (docs/integrations.md):
`fm respond -m system --no-stream|--stream [-i T] [--schema F] [-g]
[--guardrails permissive-content-transformations]` with the prompt on stdin,
`fm available`, `fm count-tokens -q`. The model is always `system`: on-device.
There is no code path to Private Cloud Compute or any remote model.

Measured on macOS 27.0.1: a short structured call takes ~1 s; the context window
(prompt + answer) is ~8192 tokens, beyond which fm exits 1 with "The session's
transcript exceeded the model's context size."
"""

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

from dfm.execution import ExecutionEnvironment
from dfm.models.base import (
    ContextLimitExceeded,
    GenerateRequest,
    GenerateResult,
    ModelError,
    ModelHealth,
    ModelIncompatible,
    ModelRefusal,
    ModelTimeout,
    ModelUnavailable,
    RemoteModelRefused,
    StructuredOutputError,
    merged_instructions,
    render_transcript,
)
from dfm.processes import ProcessManager, ProcessResult

ON_DEVICE_MODEL = "system"
DEFAULT_CONTEXT_TOKENS = 8192
ANSWER_RESERVE_TOKENS = 1024
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _clean(text: str) -> str:
    return _ANSI.sub("", text).strip()


def classify_failure(result: ProcessResult) -> ModelError:
    """Map an fm failure to a typed error."""
    if result.timed_out:
        return ModelTimeout(f"fm did not answer within the timeout ({result.duration_ms} ms)")
    text = _clean(result.stdout + "\n" + result.stderr)
    lower = text.lower()
    # CLI drift first: an error about the --guardrails flag is not a guardrail refusal.
    if any(
        k in lower for k in ("is invalid for", "unknown option", "unexpected argument", "usage:")
    ):
        return ModelIncompatible(text)
    if "context size" in lower or ("exceeded" in lower and "context" in lower):
        return ContextLimitExceeded(text)
    if "guardrail" in lower or "unsafe" in lower:
        return ModelRefusal(text)
    if "license" in lower:
        return ModelUnavailable(f"{text} (run `fm license` once to accept the terms)")
    if any(
        k in lower
        for k in (
            "not available",
            "unavailable",
            "apple intelligence",
            "not enabled",
            "not ready",
            "downloading",
        )
    ):
        return ModelUnavailable(text)
    return ModelError(text or f"fm exited with {result.exit_code}")


class AppleFMModel:
    name = "apple-fm"

    def __init__(
        self,
        pm: ProcessManager,
        env: ExecutionEnvironment,
        *,
        binary: str = "fm",
        model: str = ON_DEVICE_MODEL,
        timeout_s: float = 30.0,
        context_tokens: int = DEFAULT_CONTEXT_TOKENS,
    ) -> None:
        if model != ON_DEVICE_MODEL:
            raise RemoteModelRefused(
                f"Apple FM model {model!r} refused: only the on-device 'system' model is used"
            )
        self._pm = pm
        self._env = env
        self._binary = binary
        self._timeout = timeout_s
        self.context_tokens = context_tokens

    async def _fm(
        self, args: list[str], *, stdin: str | None = None, timeout: float | None = None
    ) -> ProcessResult:
        try:
            return await self._pm.run(
                [self._binary, *args], self._env, timeout=timeout or self._timeout, stdin=stdin
            )
        except FileNotFoundError as exc:
            raise ModelUnavailable(f"{self._binary} not found") from exc

    async def available(self) -> tuple[bool, str]:
        result = await self._fm(["available"], timeout=15)
        text = _clean(result.stdout + result.stderr)
        ok = result.exit_code == 0 and "available" in text.lower() and "not" not in text.lower()
        return ok, text

    async def count_tokens(self, prompt: str, instructions: str | None = None) -> int:
        args = ["count-tokens", "-q"]
        if instructions:
            args += ["-i", instructions]
        result = await self._fm(args, stdin=prompt, timeout=15)
        if result.exit_code != 0:
            raise classify_failure(result)
        try:
            return int(_clean(result.stdout).split()[-1])
        except (ValueError, IndexError) as exc:
            raise ModelIncompatible(f"unexpected count-tokens output: {result.stdout!r}") from exc

    async def healthcheck(self) -> ModelHealth:
        try:
            ok, text = await self.available()
            if not ok:
                return ModelHealth(self.name, False, text or "fm reports the model unavailable")
            started = time.monotonic()
            result = await self.generate(
                GenerateRequest.prompt(
                    "Reply with the single word OK.", temperature=0, timeout_s=20
                )
            )
            latency = int((time.monotonic() - started) * 1000)
            return ModelHealth(self.name, True, f"ready ({result.text[:20]!r})", latency)
        except ModelError as exc:
            return ModelHealth(self.name, False, f"{type(exc).__name__}: {exc}")

    def _args(
        self, request: GenerateRequest, schema_path: Path | None, *, stream: bool
    ) -> list[str]:
        args = ["respond", "-m", ON_DEVICE_MODEL, "--stream" if stream else "--no-stream"]
        instructions = merged_instructions(request)
        if instructions:
            args += ["-i", instructions]
        if schema_path:
            args += ["--schema", str(schema_path)]
        greedy = request.temperature is not None and request.temperature <= 0
        if greedy or request.purpose == "classify":
            args.append("-g")
        if request.purpose == "transform":
            args += ["--guardrails", "permissive-content-transformations"]
        return args

    async def _precheck(self, prompt: str, instructions: str | None) -> int | None:
        if len(prompt) + len(instructions or "") < 8000:  # well under the window; skip a call
            return None
        tokens = await self.count_tokens(prompt, instructions)
        budget = self.context_tokens - ANSWER_RESERVE_TOKENS
        if tokens > budget:
            raise ContextLimitExceeded(f"prompt is {tokens} tokens; Apple FM fits ~{budget}")
        return tokens

    def _schema_file(self, schema: dict[str, Any] | None) -> Path | None:
        if schema is None:
            return None
        path = self._env.tmp / f"fm-schema-{uuid.uuid4().hex[:8]}.json"
        path.write_text(json.dumps(schema))
        return path

    async def generate(self, request: GenerateRequest) -> GenerateResult:
        prompt = render_transcript(request)
        tokens_in = await self._precheck(prompt, merged_instructions(request))
        schema_path = self._schema_file(request.schema)
        try:
            result = await self._fm(
                self._args(request, schema_path, stream=False),
                stdin=prompt,
                timeout=request.timeout_s,
            )
        finally:
            if schema_path:
                schema_path.unlink(missing_ok=True)
        if result.exit_code != 0 or result.timed_out:
            raise classify_failure(result)
        text = _clean(result.stdout)
        if text.startswith("Error:"):
            raise classify_failure(result)
        parsed: dict[str, Any] | None = None
        if request.schema is not None:
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise StructuredOutputError(
                    f"fm returned non-JSON for a schema request: {text[:200]!r}"
                ) from exc
            if not isinstance(value, dict):
                raise StructuredOutputError(f"expected a JSON object, got {type(value).__name__}")
            parsed = cast(dict[str, Any], value)
        return GenerateResult(
            text=text,
            model=self.name,
            latency_ms=result.duration_ms,
            parsed=parsed,
            tokens_in=tokens_in,
        )

    async def stream(self, request: GenerateRequest) -> AsyncIterator[str]:
        prompt = render_transcript(request)
        await self._precheck(prompt, merged_instructions(request))
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def produce() -> ProcessResult:
            try:
                return await self._pm.run(
                    [self._binary, *self._args(request, None, stream=True)],
                    self._env,
                    timeout=request.timeout_s,
                    stdin=prompt,
                    on_output=lambda _stream, line: queue.put_nowait(line),
                )
            finally:
                queue.put_nowait(None)

        task = asyncio.create_task(produce())
        try:
            while (line := await queue.get()) is not None:
                cleaned = _ANSI.sub("", line)
                if cleaned.startswith("Error:"):
                    continue
                yield cleaned
            result = await task
        except BaseException:
            task.cancel()
            raise
        if result.exit_code != 0 or result.timed_out:
            raise classify_failure(result)
