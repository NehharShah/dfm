"""Docker and Compose the safe way.

Everything DFM creates carries `dfm.session=<id>`; compose projects are namespaced
`dfm-<session>`. Cleanup only ever targets those: `docker compose -p dfm-… down` and
label-filtered prunes, which the shell rules classify as SAFE_LOCAL. Anything wider
(`docker system prune`, `rm` by id, `compose down` of another project) stays
destructive and asks with a hard confirmation. Privileged containers and sensitive
host mounts are PRIVILEGED (see the docker rule). Image pulls are REMOTE_READ in the
container_registries network category.

The CLI is a client of a daemon outside the sandbox (Docker Desktop, OrbStack,
colima) whose socket lives under `~/.docker` or the runtime's own directory, which
the sandbox hides; so docker runs through a policy-gated runner without the sandbox wrap.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from dfm.execution import ExecutionEnvironment
from dfm.policy import Policy
from dfm.processes import ProcessManager
from dfm.tools.errors import ToolError
from dfm.tools.shell.runner import CommandRunner, EventSink

LABEL = "dfm.session"


def detect_runtime(home: Path, environ: dict[str, str] | None = None) -> tuple[str, str | None]:
    """(runtime name, socket path) for the local Docker engine, from DOCKER_HOST or known sockets."""
    env = environ if environ is not None else dict(os.environ)
    host = env.get("DOCKER_HOST", "")
    if host.startswith("unix://"):
        path = host.removeprefix("unix://")
        name = "orbstack" if ".orbstack" in path else "colima" if ".colima" in path else "custom"
        return name, path
    for name, path in (
        ("orbstack", home / ".orbstack" / "run" / "docker.sock"),
        ("colima", home / ".colima" / "default" / "docker.sock"),
        ("docker-desktop", home / ".docker" / "run" / "docker.sock"),
        ("default", Path("/var/run/docker.sock")),
    ):
        if path.exists():
            return name, str(path)
    return "none", None


def project_name(session_id: str) -> str:
    """Compose project names allow [a-z0-9_-] and must start with a letter or digit."""
    return "dfm-" + re.sub(r"[^a-z0-9_-]", "-", session_id.lower())


@dataclass
class ComposeHandle:
    project: str
    file: str
    override: Path
    services: list[str] = field(default_factory=lambda: [])


class Docker:
    def __init__(
        self,
        env: ExecutionEnvironment,
        pm: ProcessManager,
        policy: Policy,
        session_id: str,
        *,
        on_event: EventSink | None = None,
    ) -> None:
        self.env = env
        self.session_id = session_id
        self.project = project_name(session_id)
        self.runner = CommandRunner(env, pm, policy, on_event=on_event, proposed_by="dfm")

    async def _run(self, argv: list[str], timeout: float = 600) -> str:
        outcome = await self.runner.run(argv, timeout=timeout)
        if outcome.result is None:
            raise ToolError(
                f"policy refused `{' '.join(argv[:4])} …`: {outcome.verdicts[-1].reason}"
            )
        if outcome.result.exit_code != 0:
            raise ToolError(
                f"`{' '.join(argv[:4])} …` failed: {outcome.result.stderr.strip()[-400:]}"
            )
        return outcome.result.stdout

    async def available(self) -> bool:
        if self.env.which("docker") is None:
            return False
        try:
            await self._run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=20)
        except ToolError:
            return False
        return True

    def override_file(self, file: str, services: list[str]) -> Path:
        """A compose override adding the session label to every service it starts."""
        target = self.env.tmp / "docker" / f"{self.project}.override.yml"
        target.parent.mkdir(parents=True, exist_ok=True)
        doc = {"services": {s: {"labels": {LABEL: self.session_id}} for s in services}}
        target.write_text(yaml.safe_dump(doc, sort_keys=True))
        return target

    def _compose(self, handle: ComposeHandle, *args: str) -> list[str]:
        return [
            "docker",
            "compose",
            "-p",
            handle.project,
            "-f",
            handle.file,
            "-f",
            str(handle.override),
            *args,
        ]

    async def compose_up(self, file: str, services: list[str]) -> ComposeHandle:
        """`docker compose up -d` in DFM's own project, every container labelled (asks first)."""
        handle = ComposeHandle(
            self.project, file, self.override_file(file, services), list(services)
        )
        await self._run(self._compose(handle, "up", "-d", *services))
        return handle

    async def compose_logs(self, handle: ComposeHandle, tail: int = 60) -> str:
        try:
            return await self._run(
                self._compose(handle, "logs", "--tail", str(tail), *handle.services), timeout=60
            )
        except ToolError as exc:
            return str(exc)

    async def compose_down(self, handle: ComposeHandle) -> None:
        """Stop and remove DFM's project (SAFE_LOCAL: only `dfm-…` projects qualify)."""
        if not handle.project.startswith("dfm-"):
            raise ToolError(f"{handle.project} is not a DFM project; not touching it")
        await self._run(self._compose(handle, "down"))

    async def cleanup(self) -> None:
        """Remove stopped containers, volumes and networks this session labelled."""
        for kind in ("container", "volume", "network"):
            await self._run(
                ["docker", kind, "prune", "-f", "--filter", f"label={LABEL}={self.session_id}"]
            )

    def run_argv(self, image: str, *args: str, name: str | None = None) -> list[str]:
        """`docker run` for a DFM container: attached (stopped through the process registry),
        removed on exit, labelled. Privileged flags in `args` are classified PRIVILEGED."""
        argv = ["docker", "run", "--rm", "--init", "--label", f"{LABEL}={self.session_id}"]
        if name:
            argv += ["--name", f"{self.project}-{name}"]
        return [*argv, *args, image]
