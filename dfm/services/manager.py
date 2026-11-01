"""Start, check and stop local services for tests.

    ensure(spec): healthy already (port + kind-specific probe) → reuse it, not owned
                  else start it, preferring:
                    compose (`docker compose up -d <svc>`, when the repo has the service)
                    → the server binary in a DFM temp data dir (postgres, redis, nats, anvil, devnet)
                    → `brew services start` (a shared, user-level service: last resort)
                  every start is classified and authorized (LOCAL_STATE_CHANGE: asks by default)
                  and recorded as DFM-owned; on a failed start the logs come back in the error
    release(h):   stops only what DFM started (owned registry / process registry)

Only loopback hosts are touched. DFM never stops a service it did not start.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from dfm.execution import ExecutionEnvironment
from dfm.policy import Decision, Policy
from dfm.policy.network import is_loopback
from dfm.processes import ProcessManager
from dfm.processes.manager import ManagedProcess, port_ready
from dfm.security.sandbox import Sandbox
from dfm.services.catalog import ServiceSpec
from dfm.tools.docker import ComposeHandle, Docker
from dfm.tools.errors import ToolError
from dfm.tools.shell.classify import classify
from dfm.tools.shell.runner import CommandRunner, EventSink


class ServiceError(Exception):
    def __init__(self, message: str, logs: str = "") -> None:
        super().__init__(message)
        self.logs = logs


@dataclass(frozen=True)
class ServiceHandle:
    id: str
    spec: ServiceSpec
    owned: bool
    method: str  # existing | compose | binary | brew
    process_id: str | None = None
    detail: str = ""

    @property
    def url(self) -> str:
        scheme = {"postgres": "postgresql", "anvil": "http", "starknet-devnet": "http"}.get(
            self.spec.kind, self.spec.kind
        )
        return f"{scheme}://{self.spec.host}:{self.spec.port}"


async def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


_BREW_FORMULA = {"postgres": "postgresql", "redis": "redis", "nats": "nats-server"}


class ServiceManager:
    def __init__(
        self,
        env: ExecutionEnvironment,
        pm: ProcessManager,
        policy: Policy,
        registry_dir: Path,
        *,
        on_event: EventSink | None = None,
        sandbox: bool = True,
    ) -> None:
        self.env = env
        self.pm = pm
        self.policy = policy
        self.registry = registry_dir / "services.json"
        self.wrap = Sandbox(enabled=sandbox).wrap
        # probes, initdb and the servers themselves run sandboxed
        self.runner = CommandRunner(
            env, pm, policy, on_event=on_event, wrap=self.wrap, proposed_by="dfm"
        )
        # docker/brew are clients of daemons that do the work; the sandbox hides ~/.docker
        # (the daemon socket lives there), so they run unwrapped, still classified and gated
        self.host_runner = CommandRunner(env, pm, policy, on_event=on_event, proposed_by="dfm")
        self.handles: dict[str, ServiceHandle] = {}
        self.docker = Docker(env, pm, policy, pm.session_id or "dfm", on_event=on_event)

    # ---------------------------------------------------------------- health

    async def healthy(self, spec: ServiceSpec) -> bool:
        if not await port_open(spec.host, spec.port):
            return False
        probe: list[str] | None = None
        if spec.kind == "postgres" and self.env.which("pg_isready"):
            probe = ["pg_isready", "-h", spec.host, "-p", str(spec.port), "-t", "2"]
        elif spec.kind == "redis" and self.env.which("redis-cli"):
            probe = ["redis-cli", "-h", spec.host, "-p", str(spec.port), "ping"]
        if probe is None:
            return True  # the port answers; no protocol-level probe for this kind
        outcome = await self.runner.run(probe, timeout=10)
        if outcome.result is None or outcome.result.exit_code != 0:
            return False
        return spec.kind != "redis" or "PONG" in outcome.result.stdout

    async def wait_healthy(self, spec: ServiceSpec, timeout: float = 60.0) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if await self.healthy(spec):
                return True
            await asyncio.sleep(0.3)
        return False

    # ---------------------------------------------------------------- ownership registry

    def _load(self) -> dict[str, dict[str, object]]:
        try:
            return json.loads(self.registry.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict[str, dict[str, object]]) -> None:
        self.registry.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.registry.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.registry)

    def _own(self, handle: ServiceHandle) -> ServiceHandle:
        data = self._load()
        data[handle.id] = {
            "spec": asdict(handle.spec),
            "method": handle.method,
            "process_id": handle.process_id,
            "detail": handle.detail,
            "session": self.pm.session_id,
        }
        self._save(data)
        self.handles[handle.id] = handle
        return handle

    def owned(self) -> list[ServiceHandle]:
        return [
            ServiceHandle(
                key,
                ServiceSpec(**rec["spec"]),  # type: ignore[arg-type]
                True,
                str(rec["method"]),
                rec.get("process_id"),  # type: ignore[arg-type]
                str(rec.get("detail", "")),
            )
            for key, rec in self._load().items()
        ]

    # ---------------------------------------------------------------- start

    async def _authorized(self, argv: list[str]) -> str | None:
        """None when policy allows every part of `argv`; else the refusal reason."""
        classification = classify(argv, inside=self.env.is_inside)
        for action in classification.to_actions("dfm"):
            verdict = await self.policy.authorize(action)
            if verdict.decision is not Decision.ALLOW:
                return verdict.reason
        return None

    def _binary(
        self, spec: ServiceSpec, data_dir: Path
    ) -> tuple[list[list[str]], list[str]] | None:
        """(setup commands, server argv) to run `spec` from its own binary, if installed."""
        port = str(spec.port)
        if spec.kind == "postgres" and self.env.which("postgres") and self.env.which("initdb"):
            setup = [
                ["initdb", "-D", str(data_dir), "-U", "postgres", "--auth=trust", "-E", "UTF8"]
            ]
            server = [
                "postgres",
                "-D",
                str(data_dir),
                "-p",
                port,
                "-c",
                "listen_addresses=127.0.0.1",
                # TCP only: socket paths over 103 bytes fail on macOS, and tests connect via TCP
                "-c",
                "unix_socket_directories=",
            ]
            return setup, server
        if spec.kind == "redis" and self.env.which("redis-server"):
            return [], [
                "redis-server",
                "--port",
                port,
                "--bind",
                "127.0.0.1",
                "--save",
                "",
                "--appendonly",
                "no",
                "--dir",
                str(data_dir),
            ]
        if spec.kind == "nats" and self.env.which("nats-server"):
            return [], ["nats-server", "-a", "127.0.0.1", "-p", port]
        if spec.kind == "anvil" and self.env.which("anvil"):
            return [], ["anvil", "--host", "127.0.0.1", "--port", port]
        if spec.kind == "starknet-devnet" and self.env.which("starknet-devnet"):
            return [], ["starknet-devnet", "--host", "127.0.0.1", "--port", port, "--seed", "0"]
        return None

    async def _docker_ready(self) -> bool:
        if not self.env.which("docker"):
            return False
        return await self.docker.available()

    def _compose_handle(self, spec: ServiceSpec, override: str | None = None) -> ComposeHandle:
        docker = self.docker
        assert spec.compose_file and spec.compose_service  # noqa: S101
        path = (
            Path(override)
            if override
            else docker.override_file(spec.compose_file, [spec.compose_service])
        )
        return ComposeHandle(docker.project, spec.compose_file, path, [spec.compose_service])

    async def _start_compose(self, spec: ServiceSpec) -> ServiceHandle:
        """In DFM's own compose project (`dfm-<session>`), the container labelled `dfm.session`."""
        assert spec.compose_file and spec.compose_service  # noqa: S101
        try:
            compose = await self.docker.compose_up(spec.compose_file, [spec.compose_service])
        except ToolError as exc:
            raise ServiceError(f"{spec.name}: not started ({exc})") from exc
        handle = self._own(
            ServiceHandle(
                uuid.uuid4().hex[:10], spec, True, "compose", detail=str(compose.override)
            )
        )
        if not await self.wait_healthy(spec):
            logs = await self.docker.compose_logs(compose)
            await self.release(handle)
            raise ServiceError(f"{spec.name}: started but never became healthy", logs[-3000:])
        return handle

    async def _start_binary(self, spec: ServiceSpec) -> ServiceHandle | None:
        data_dir = self.env.tmp / "services" / f"{spec.kind}-{spec.port}"
        plan = self._binary(spec, data_dir)
        if plan is None:
            return None
        setup, server = plan
        data_dir.mkdir(parents=True, exist_ok=True)
        for argv in setup:
            outcome = await self.runner.run(argv, timeout=300)
            if outcome.result is None:
                raise ServiceError(f"{spec.name}: not started ({outcome.verdicts[-1].reason})")
            if outcome.result.exit_code != 0:
                raise ServiceError(
                    f"{spec.name}: `{argv[0]}` failed",
                    outcome.result.stdout[-1500:] + outcome.result.stderr[-1500:],
                )
        refused = await self._authorized(server)
        if refused is not None:
            raise ServiceError(f"{spec.name}: not started ({refused})")
        classification = classify(server, inside=self.env.is_inside)
        command = list(self.wrap(server, self.env, classification))
        try:
            proc: ManagedProcess = await self.pm.start_background(
                command,
                self.env,
                name=f"service:{spec.kind}:{spec.port}",
                ready=port_ready(spec.port),
                ready_timeout=60,
            )
        except Exception as exc:  # readiness timeout or exec failure
            raise ServiceError(f"{spec.name}: {spec.kind} did not start: {exc}") from exc
        handle = self._own(
            ServiceHandle(uuid.uuid4().hex[:10], spec, True, "binary", proc.id, " ".join(server))
        )
        if not await self.wait_healthy(spec, timeout=30):
            logs = proc.output.text()[-3000:]
            await self.release(handle)
            raise ServiceError(f"{spec.name}: started but never became healthy", logs)
        return handle

    async def _start_brew(self, spec: ServiceSpec) -> ServiceHandle | None:
        formula = _BREW_FORMULA.get(spec.kind)
        if formula is None or not self.env.which("brew"):
            return None
        argv = ["brew", "services", "start", formula]
        outcome = await self.host_runner.run(argv, timeout=120)
        if outcome.result is None or outcome.result.exit_code != 0:
            return None
        handle = self._own(ServiceHandle(uuid.uuid4().hex[:10], spec, True, "brew", detail=formula))
        if not await self.wait_healthy(spec):
            await self.release(handle)
            raise ServiceError(f"{spec.name}: brew service {formula} never became healthy")
        return handle

    async def ensure(self, spec: ServiceSpec) -> ServiceHandle:
        if not is_loopback(spec.host):
            raise ServiceError(
                f"{spec.name}: {spec.host} is not local; DFM only manages local services"
            )
        if await self.healthy(spec):
            handle = ServiceHandle(uuid.uuid4().hex[:10], spec, False, "existing")
            self.handles[handle.id] = handle
            return handle
        if await port_open(spec.host, spec.port):
            raise ServiceError(
                f"{spec.name}: port {spec.port} is taken by something that is not a healthy {spec.kind}"
            )
        if spec.compose_file and spec.compose_service and await self._docker_ready():
            return await self._start_compose(spec)
        started = await self._start_binary(spec)
        if started is None:
            started = await self._start_brew(spec)
        if started is None:
            how = (
                "docker compose (Docker not running)"
                if spec.compose_file
                else f"a local {spec.kind} binary"
            )
            raise ServiceError(f"{spec.name}: no way to start it here (tried {how})")
        return started

    # ---------------------------------------------------------------- stop

    async def release(self, handle: ServiceHandle) -> None:
        """Stop `handle` if DFM started it; services DFM found running are left alone."""
        self.handles.pop(handle.id, None)
        if not handle.owned:
            return
        data = self._load()
        if handle.id not in data:
            return  # not in DFM's ownership registry: not ours to stop
        if handle.method == "binary" and handle.process_id:
            await self.pm.stop(handle.process_id)  # ownership-checked by the process registry
        elif (
            handle.method == "compose" and handle.spec.compose_file and handle.spec.compose_service
        ):
            with contextlib.suppress(ToolError):  # SAFE_LOCAL: only DFM's own project
                await self.docker.compose_down(
                    self._compose_handle(handle.spec, handle.detail or None)
                )
        elif handle.method == "brew":
            await self.host_runner.run(["brew", "services", "stop", handle.detail], timeout=120)
        data.pop(handle.id, None)
        self._save(data)

    async def release_all(self) -> None:
        for handle in list(self.handles.values()):
            await self.release(handle)
