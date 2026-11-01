"""Shared test doubles and the sandboxed runner live toolchain tests use."""

import json
import os
import sys
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

from dfm.config import ConfigOverrides, DFMConfig, state_dirs
from dfm.execution import ExecutionEnvironment, ResourceLimits
from dfm.models import GenerateRequest, GenerateResult, ModelHealth
from dfm.models.mlx.discovery import default_roots, discover
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

LIVE_MODEL_ENV = "DFM_TEST_MODEL"
LIVE_MODEL_SKIP = (
    f"needs mlx-lm and {LIVE_MODEL_ENV} set to a local MLX model (a directory, or a name "
    "from `dfm models` such as mlx-community/Qwen3.8-27B-4bit)"
)


def live_model() -> Path | None:
    """The local model live tests run on. Opt-in: a 27B model heats a laptop, so a plain
    `pytest` run never loads one."""
    value = os.environ.get(LIVE_MODEL_ENV, "").strip()
    if not value:
        return None
    path = Path(value).expanduser()
    if (path / "config.json").is_file():
        return path
    for candidate in discover(default_roots()):
        if value in (candidate.name, candidate.name.rsplit("/", 1)[-1]):
            return candidate.path
    return None


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


# The PATH tests give subprocesses: the interpreter running the tests (so `pytest`, `ruff`,
# `pyright` resolve to this venv however pytest was invoked) and the system directories.
# Never the developer's shell PATH: tests must not find the real `gh`, `kubectl` or `cargo`.
VENV_BIN = str(Path(sys.executable).parent)
TEST_PATH = f"{VENV_BIN}:/usr/bin:/bin:/usr/sbin:/sbin"


def make_env(
    repo: Path,
    tmp_path: Path,
    *,
    home: Path | None = None,
    path: str = TEST_PATH,
    environ: Mapping[str, str] | None = None,
    config: DFMConfig | None = None,
    session_id: str = "test",
    limits: ResourceLimits | None = None,
    overrides: ConfigOverrides | None = None,
) -> ExecutionEnvironment:
    """An ExecutionEnvironment for `repo` with HOME and DFM's state under `tmp_path`."""
    merged = {"HOME": str(home or tmp_path / "home"), "PATH": path, **(environ or {})}
    return ExecutionEnvironment.create(
        repo,
        config or DFMConfig(),
        session_id=session_id,
        environ=merged,
        dirs=state_dirs(merged),
        limits=limits,
        overrides=overrides,
    )


def sandboxed_runner(
    repo: Path,
    tmp_path: Path,
    *bin_dirs: str,
    env: Mapping[str, str] | None = None,
    config: DFMConfig | None = None,
) -> CommandRunner:
    """A CommandRunner exactly as the agent loop builds one: policy plus the macOS sandbox
    (network off unless a command carries an authorized network category)."""
    path = ":".join([*bin_dirs, TEST_PATH])
    execution = make_env(repo, tmp_path, path=path, environ=env, config=config, session_id="live")
    return CommandRunner(
        execution,
        ProcessManager(ProcessRegistry(execution.tmp / "p")),
        Policy(config or DFMConfig()),
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
