"""Local MLX coding models behind the Model protocol.

The model runs in a worker process (`dfm.models.mlx.worker`), started lazily on
first use and stopped after `idle_unload_s` without requests, so a crash or OOM
kill surfaces as `ModelUnavailable` instead of taking DFM down. The worker gets
`HF_HUB_OFFLINE=1` and, on macOS, a sandbox profile that denies outbound
network: model inference is local by construction.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import signal
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from dfm.config.paths import cache_home
from dfm.execution import ExecutionEnvironment
from dfm.models.base import (
    ContextLimitExceeded,
    GenerateRequest,
    GenerateResult,
    ModelError,
    ModelHealth,
    ModelTimeout,
    ModelUnavailable,
    merged_instructions,
)
from dfm.models.mlx.discovery import describe_dir
from dfm.models.mlx.text import final_text, named_call_action, tag_action, tool_call_as_action
from dfm.models.structured import first_json_object
from dfm.models.thermal import monitor
from dfm.security.sandbox import SANDBOX_EXEC, available, build_profile

if TYPE_CHECKING:
    from dfm.models.resources import ModelResourceManager

Role = Literal["primary", "small"]


@dataclass(frozen=True)
class LocalModelSpec:
    role: Role
    path: Path
    name: str = ""
    context: int = 32768
    max_tokens: int = 4096
    thermal_limit: int | None = 1  # pace generation from this pressure level (1 moderate); None off
    seed: int | None = None  # reproducible sampling (benchmarks)

    @property
    def model_name(self) -> str:
        return "dfm-primary" if self.role == "primary" else "dfm-small"


THERMAL_WAIT_S = 120.0
THERMAL_POLL_S = 5.0
CANCEL_GRACE_S = 5.0  # a cancelled worker has this long to answer before it is killed


# How to get the local coder, whichever way DFM was installed.
MLX_INSTALL_HINT = (
    "install the mlx extra: `uv tool install 'dfm-agent[mlx]'` "
    "(or `uv sync --extra mlx` in a checkout)"
)


def mlx_available() -> bool:
    try:
        import mlx_lm  # noqa: F401  # pyright: ignore[reportUnusedImport, reportMissingImports]
    except ImportError:
        return False
    return True


def _schema_instructions(schema: dict[str, Any]) -> str:
    return (
        "Respond with exactly one JSON object and nothing else (no prose, no code fences). "
        f"It must match this JSON Schema:\n{json.dumps(schema)}"
    )


class MLXModel:
    def __init__(
        self,
        spec: LocalModelSpec,
        env: ExecutionEnvironment,
        *,
        idle_unload_s: float = 600.0,
        python: str = sys.executable,
        sandbox: bool = True,
        resources: ModelResourceManager | None = None,
    ) -> None:
        self.spec = spec
        self.name = spec.model_name
        self.env = env
        self.idle_unload_s = idle_unload_s
        self.python = python
        self.sandbox = sandbox and available()
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._loaded = False
        self._last_used = 0.0
        self._idle_task: asyncio.Task[None] | None = None
        self.stats: dict[str, Any] = {}
        self.resources = resources
        self.thermal = monitor()
        self.native_tools = False  # learned at load: the chat template takes tool definitions

    @property
    def loaded(self) -> bool:
        return self._loaded and self._proc is not None and self._proc.returncode is None

    def _kv_dir(self) -> Path:
        """Persisted KV snapshots (ADR 0013). Under `~/.cache` so toolchain cache rules apply to
        the parent, but denied to repository code by the sandbox (`dfm_private_paths`)."""
        return cache_home(self.env.source_env) / "kv"

    # ---------------------------------------------------------------- worker process

    async def _start(self) -> asyncio.subprocess.Process:
        if self._proc is not None and self._proc.returncode is None:
            return self._proc
        env = self.env.child_env()
        env["HF_HUB_OFFLINE"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[3])
        argv = [self.python, "-m", "dfm.models.mlx.worker"]
        if self.sandbox:
            # the worker alone may write the persisted KV cache; repository code may not
            profile = build_profile(self.env, network=False, extra_write=[self._kv_dir()])
            argv = [SANDBOX_EXEC, "-p", profile.text, *argv]
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            cwd=self.env.tmp,
            start_new_session=True,
            limit=64 * 1024 * 1024,
        )
        self._loaded = False
        return self._proc

    async def _write(self, proc: asyncio.subprocess.Process, message: dict[str, Any]) -> None:
        assert proc.stdin is not None  # noqa: S101
        proc.stdin.write((json.dumps(message) + "\n").encode())
        try:
            await proc.stdin.drain()
        except (ConnectionError, BrokenPipeError) as exc:
            raise ModelUnavailable(f"{self.name} worker exited") from exc

    async def _read_reply(
        self, proc: asyncio.subprocess.Process, request_id: int, on_chunk: Any = None
    ) -> dict[str, Any]:
        """The final reply to `request_id`; chunks go to `on_chunk`, other ids are skipped."""
        assert proc.stdout is not None  # noqa: S101
        while True:
            line = await proc.stdout.readline()
            if not line:
                raise ModelUnavailable(
                    f"{self.name} worker exited (exit {proc.returncode}); likely out of memory"
                )
            try:
                message = cast(dict[str, Any], json.loads(line))
            except ValueError:
                # a library printing to stdout, or a line cut short by a crash (EOF follows and
                # is reported as the worker exiting); either way not a protocol message
                continue
            if message.get("id") != request_id:
                continue
            if "chunk" in message:
                if on_chunk is not None:
                    await on_chunk(str(message["chunk"]))
                continue
            return message

    async def _interrupt(self, proc: asyncio.subprocess.Process, request_id: int) -> None:
        """Stop request `request_id` in the worker and keep the worker (and its loaded weights)
        if it answers within CANCEL_GRACE_S; otherwise it is stuck and gets killed."""
        if proc.returncode is not None:
            return
        try:
            await asyncio.wait_for(self._cancel_and_drain(proc, request_id), CANCEL_GRACE_S)
        except (TimeoutError, ModelError):
            await self._kill()

    async def _cancel_and_drain(self, proc: asyncio.subprocess.Process, request_id: int) -> None:
        await self._write(proc, {"op": "cancel", "target": request_id})
        await self._read_reply(proc, request_id)  # its answer, with stop_reason "cancelled"

    async def _call(
        self, op: str, timeout: float, on_chunk: Any = None, **params: Any
    ) -> dict[str, Any]:
        proc = await self._start()
        if proc.stdin is None or proc.stdout is None:
            raise ModelUnavailable("worker pipes missing")
        self._next_id += 1
        request_id = self._next_id

        async def exchange() -> dict[str, Any]:
            # the write is inside the timeout too: a request larger than the pipe buffer to a
            # worker that is not reading would otherwise block here with no limit at all
            await self._write(proc, {"id": request_id, "op": op, **params})
            return await self._read_reply(proc, request_id, on_chunk)

        try:
            reply = await asyncio.wait_for(exchange(), timeout)
        except TimeoutError as exc:
            await self._interrupt(proc, request_id)
            raise ModelTimeout(f"{self.name}: {op} took longer than {timeout}s") from exc
        except asyncio.CancelledError:
            # Ctrl-C in the REPL, or a consumer that stopped streaming: the worker must stop
            # generating too, or it burns the GPU to max_tokens and the next request queues
            await asyncio.shield(self._interrupt(proc, request_id))
            raise
        if not reply.get("ok", False):
            kind = reply.get("kind")
            error = str(reply.get("error", "worker error"))
            if kind == "context":
                raise ContextLimitExceeded(error)
            if kind in {"oom", "missing", "not_loaded"}:
                raise ModelUnavailable(error)
            raise ModelError(error)
        return reply

    async def _kill(self) -> None:
        proc = self._proc
        self._proc = None
        self._loaded = False
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), 5)

    async def load(self) -> None:
        if self.loaded:
            return
        if not self.spec.path.is_dir():
            raise ModelUnavailable(f"{self.name}: model directory not found: {self.spec.path}")
        if self.resources is not None:
            candidate = describe_dir(self.spec.path, self.spec.name or None)
            if candidate is not None:
                admission = await asyncio.to_thread(
                    self.resources.admit, candidate, self.spec.context
                )
                self.stats["admission"] = admission.reason
                if admission.decision in {"deny", "use_small"}:
                    raise ModelUnavailable(f"{self.name}: {admission.reason}")
                if admission.decision == "reduce_context":
                    self.spec = dataclasses.replace(self.spec, context=admission.context)
        kv_dir = self._kv_dir()
        reply = await self._call("load", timeout=900, path=str(self.spec.path), kv_dir=str(kv_dir))
        self._loaded = True
        self.stats.update(load_s=reply.get("load_s"), peak_gb=reply.get("peak_gb"))
        self.native_tools = bool(reply.get("native_tools"))
        self.stats["restored_tokens"] = reply.get("restored_tokens", 0)
        self._schedule_idle_unload()

    async def unload(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            with contextlib.suppress(ModelError, OSError):
                await self._call("unload", timeout=30, exit=True)
        await self._kill()

    def _schedule_idle_unload(self) -> None:
        self._last_used = time.monotonic()
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_watch())

    async def _idle_watch(self) -> None:
        while self.loaded:
            await asyncio.sleep(min(30.0, self.idle_unload_s))
            # under the lock: a generate() scheduled in the same tick must not write to a
            # worker that is about to exit
            async with self._lock:
                if self.loaded and time.monotonic() - self._last_used >= self.idle_unload_s:
                    await self.unload()

    # ---------------------------------------------------------------- Model protocol

    async def healthcheck(self) -> ModelHealth:
        if not mlx_available():
            return ModelHealth(self.name, False, "mlx-lm not installed (see `dfm doctor`)")
        if not self.spec.path.is_dir():
            return ModelHealth(self.name, False, f"model not found: {self.spec.path}")
        try:
            async with self._lock:  # one reader on the worker's stdout at a time
                reply = await self._call("ping", timeout=60)
        except ModelError as exc:
            return ModelHealth(self.name, False, f"{type(exc).__name__}: {exc}")
        state = "loaded" if self.loaded else "sleeping (loads on first use)"
        return ModelHealth(
            self.name, True, f"{self.spec.name or self.spec.path.name}: {state}", extra=reply
        )

    def _uses_tools(self, request: GenerateRequest) -> bool:
        return bool(request.tools) and self.native_tools

    def _messages(self, request: GenerateRequest) -> list[dict[str, str]]:
        instructions = merged_instructions(request) or ""
        if self._uses_tools(request):
            # the template lists the tools and their call format, as the model was trained
            instructions = (instructions + "\n\n" if instructions else "") + (
                "Answer every turn with exactly one call to one of the provided functions."
            )
        elif request.schema is not None:
            instructions = (instructions + "\n\n" if instructions else "") + _schema_instructions(
                request.schema
            )
        messages: list[dict[str, str]] = []
        if instructions:
            messages.append({"role": "system", "content": instructions})
        for m in request.messages:
            if m.role == "system":
                continue
            role = "user" if m.role == "tool" else m.role
            messages.append({"role": role, "content": m.content})
        return messages

    async def _cool_down(self) -> float:
        """Above the thermal limit, wait (up to THERMAL_WAIT_S) before starting more work."""
        limit = self.spec.thermal_limit
        waited = 0.0
        while limit is not None and self.thermal.level() > limit and waited < THERMAL_WAIT_S:
            await asyncio.sleep(THERMAL_POLL_S)
            waited += THERMAL_POLL_S
        return waited

    def _generate_params(self, request: GenerateRequest) -> tuple[float, dict[str, Any]]:
        """(timeout, worker params) for one generate request, streamed or not."""
        # Thermal rests get their own allowance on top of the request's timeout, so pacing a hot
        # machine never trips the timeout (which kills the worker and its loaded model).
        rest_budget = request.timeout_s if self.spec.thermal_limit is not None else 0.0
        return request.timeout_s + rest_budget, {
            "messages": self._messages(request),
            "max_tokens": request.max_tokens or self.spec.max_tokens,
            "temperature": request.temperature,  # None: the model's own sampling
            "stop": list(request.stop),
            "context_limit": self.spec.context,
            "thermal_limit": self.spec.thermal_limit,
            "rest_budget_s": rest_budget,
            "seed": self.spec.seed,
            "reasoning": request.reasoning,
            "thinking_budget": request.thinking_budget,
            "tools": list(request.tools or ()) if self._uses_tools(request) else None,
        }

    async def generate(self, request: GenerateRequest) -> GenerateResult:
        async with self._lock:
            waited = await self._cool_down()
            await self.load()
            timeout, params = self._generate_params(request)
            reply = await self._call("generate", timeout=timeout, **params)
            self._schedule_idle_unload()
        raw = str(reply.get("text", ""))
        text = final_text(raw)
        allowed = (request.schema or {}).get("properties", {}).get("action", {}).get("enum")
        if isinstance(allowed, list):
            actions = [str(a) for a in cast(list[Any], allowed)]
            translated: str | None = None
            if not text:
                translated = tool_call_as_action(raw, actions)
            elif "{" not in text or text.lstrip().startswith("<"):
                # markup first: a Qwen XML call's file content may hold braces of its own
                translated = tag_action(text, actions)
            if translated is not None:
                text = translated
                self.stats["tool_call_translated"] = True
        self.stats.update(
            tps=reply.get("generation_tps"),
            peak_gb=reply.get("peak_gb"),
            cached_tokens=reply.get("cached_tokens"),
            stop_reason=reply.get("stop_reason"),
            thermal_level=reply.get("thermal_level"),
            thinking_tokens=reply.get("thinking_tokens"),
            thinking_cut=reply.get("thinking_cut"),
        )
        rested = waited + float(reply.get("thermal_pause_s") or 0.0)
        parsed: dict[str, Any] | None = None
        if request.schema is not None:
            parsed = first_json_object(text)
            if parsed is not None and isinstance(allowed, list):
                parsed = (
                    named_call_action(parsed, [str(a) for a in cast(list[Any], allowed)]) or parsed
                )
        return GenerateResult(
            text=text,
            model=self.name,
            latency_ms=int(float(reply.get("seconds", 0)) * 1000),
            parsed=parsed,
            tokens_in=reply.get("prompt_tokens"),
            tokens_out=reply.get("generation_tokens"),
            tokens_cached=reply.get("cached_tokens"),
            stop_reason=reply.get("stop_reason"),
            thermal_rest_s=rested,
        )

    async def stream(self, request: GenerateRequest) -> AsyncIterator[str]:
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def on_chunk(chunk: str) -> None:
            await queue.put(chunk)

        async def run() -> None:
            try:
                async with self._lock:
                    await self._cool_down()
                    await self.load()
                    timeout, params = self._generate_params(request)
                    await self._call(
                        "generate", timeout=timeout, on_chunk=on_chunk, stream=True, **params
                    )
            finally:
                await queue.put(None)

        task = asyncio.create_task(run())
        try:
            while (chunk := await queue.get()) is not None:
                yield chunk
            await task
        finally:
            if not task.done():
                task.cancel()  # the consumer stopped early: stop the worker too (see _call)
                with contextlib.suppress(asyncio.CancelledError, ModelError):
                    await task
