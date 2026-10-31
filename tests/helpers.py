"""Shared test doubles and the sandboxed runner live toolchain tests use."""

import json
import sys
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

from dfm.config import DFMConfig, state_dirs
from dfm.execution import ExecutionEnvironment
from dfm.models import GenerateRequest, GenerateResult, ModelHealth
from dfm.policy import Policy
from dfm.processes import ProcessManager, ProcessRegistry
from dfm.security.sandbox import Sandbox
from dfm.tools.shell.runner import CommandRunner

BUGGY = "def add(a, b):\n    return a - b\n"
FIX = {
    "action": "patch",
    "reason": "fix the operator",
    "path": "src/calc/ops.py",
    "search": "return a - b",
    "replace": "return a + b",
}
DONE = {"action": "done", "reason": "tests pass", "summary": "fixed add"}


class ScriptedCoder:
    name = "scripted-coder"

    def __init__(self, *actions: Mapping[str, object] | str) -> None:
        self.actions = list(actions)
        self.prompts: list[str] = []
        self.requests: list[GenerateRequest] = []

    async def healthcheck(self) -> ModelHealth:
        return ModelHealth(self.name, True, "ok")

    async def generate(self, request: GenerateRequest) -> GenerateResult:
        self.requests.append(request)
        self.prompts.append("\n\n".join(m.content for m in request.messages))
        step = self.actions.pop(0) if len(self.actions) > 1 else self.actions[0]
        text = step if isinstance(step, str) else json.dumps(step)
        return GenerateResult(text=text, model=self.name, latency_ms=1)

    async def stream(self, request: GenerateRequest) -> AsyncIterator[str]:
        yield ""


def sandboxed_runner(
    repo: Path, tmp_path: Path, *bin_dirs: str, env: Mapping[str, str] | None = None
) -> CommandRunner:
    """A CommandRunner exactly as the agent loop builds one: policy plus the macOS sandbox
    (network off unless a command carries an authorized network category)."""
    path = ":".join([*bin_dirs, str(Path(sys.executable).parent), "/usr/bin", "/bin"])
    environ = {"HOME": str(tmp_path / "home"), "PATH": path, **(env or {})}
    execution = ExecutionEnvironment.create(
        repo, DFMConfig(), session_id="live", environ=environ, dirs=state_dirs(environ)
    )
    return CommandRunner(
        execution,
        ProcessManager(ProcessRegistry(execution.tmp / "p")),
        Policy(DFMConfig()),
        wrap=Sandbox().wrap,
    )


def configure_fake(bin_dir: Path, **settings: str) -> None:
    """Settings for tests/fake_gh.py (it reads them from a file: DFM scrubs unknown env vars)."""
    path = bin_dir / "fake_gh.json"
    current = json.loads(path.read_text()) if path.exists() else {}
    path.write_text(json.dumps(current | settings))


def calls(log: Path) -> list[list[str]]:
    """The argv lists the fake gh was called with."""
    return [json.loads(line) for line in log.read_text().splitlines()]
