"""`dfm setup`: from a fresh machine to a working DFM.

Idempotent. Reads first, then asks before it writes (config, state dirs) and
never downloads anything on its own: missing packages and weights are reported
with the exact command, or fetched only when the developer passes an explicit
flag and approves the network action. No hosted-model API keys are involved.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dfm.config import ConfigOverrides, DFMConfig, load_config, state_dirs
from dfm.config.writer import set_user_value
from dfm.execution import ExecutionEnvironment
from dfm.integrations.repocanon import RepoCanonProvider
from dfm.integrations.repokg import RepoKGProvider
from dfm.models.mlx.discovery import ModelCandidate, choose, default_roots, discover, usable
from dfm.models.mlx.model import MLX_INSTALL_HINT, LocalModelSpec, MLXModel, mlx_available
from dfm.models.resources import ModelResourceManager
from dfm.policy import NetworkCategory, Policy
from dfm.policy.network import NetworkPolicy

StepStatus = Literal["ok", "warn", "fail", "skip", "changed"]


@dataclass
class SetupStep:
    name: str
    status: StepStatus
    detail: str
    action: str = ""  # what the developer can run


@dataclass
class SetupReport:
    steps: list[SetupStep] = field(default_factory=lambda: [])
    primary: ModelCandidate | None = None
    small: ModelCandidate | None = None

    def add(self, name: str, status: StepStatus, detail: str, action: str = "") -> None:
        self.steps.append(SetupStep(name, status, detail, action))

    @property
    def ok(self) -> bool:
        return not any(s.status == "fail" for s in self.steps)


Confirm = Callable[[str], bool]


async def setup(
    *,
    confirm: Confirm,
    dry_run: bool = False,
    evaluate_models: bool = False,
    repo: Path | None = None,
    resources: ModelResourceManager | None = None,
    roots: list[Path] | None = None,
) -> SetupReport:
    report = SetupReport()
    resources = resources or ModelResourceManager()
    dirs = state_dirs()

    hw = resources.hardware()
    report.add(
        "machine",
        "ok" if hw.arch == "arm64" else "warn",
        f"{hw.chip}, {hw.memory_gb:.0f} GB, macOS {hw.macos}, {hw.disk_free_bytes / 1024**3:.0f} GB free disk",
    )

    fm = shutil.which("fm")
    if fm is None:
        report.add(
            "Apple FM",
            "warn",
            "fm CLI not found: DFM uses local coders for light tasks",
            "requires macOS 26+ with Apple Intelligence",
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            fm, "available", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await proc.communicate()
        text = out.decode(errors="replace").strip()
        good = proc.returncode == 0 and "available" in text.lower() and "not" not in text.lower()
        report.add(
            "Apple FM",
            "ok" if good else "warn",
            text or "unavailable",
            "" if good else "enable Apple Intelligence; run `fm license`",
        )

    report.add(
        "uv",
        "ok" if shutil.which("uv") else "warn",
        shutil.which("uv") or "not found",
        "brew install uv",
    )
    if mlx_available():
        report.add("MLX", "ok", "mlx-lm installed")
    else:
        report.add(
            "MLX",
            "fail",
            "mlx-lm not installed: no local coding model can run",
            MLX_INSTALL_HINT,
        )

    for name, health in (
        ("RepoCanon", await RepoCanonProvider().healthcheck()),
        ("RepoKG", await RepoKGProvider().healthcheck()),
    ):
        report.add(
            name,
            "ok" if health.available else "warn",
            f"{health.version or ''} {health.detail}".strip(),
        )
    reflex = shutil.which("reflex")
    report.add(
        "Reflex",
        "ok" if reflex else "warn",
        reflex or "not found (no historical memory)",
        "" if reflex else "npm i -g reflex-skills",
    )

    config = load_config(None).config
    context = config.models.primary.context
    candidates = discover(roots or default_roots(data_dir=dirs.data))
    budget = resources.budget_gb()
    total_budget = (
        hw.memory_gb * (1 - resources.headroom_fraction) - 6
    )  # what fits when the machine is quiet
    primary, small = choose(candidates, max(budget, total_budget), context)
    report.primary, report.small = primary, small
    if primary is None or small is None:
        usable_names = ", ".join(c.name for c in candidates if usable(c)) or "none"
        report.add(
            "local coder",
            "fail",
            f"no usable local MLX model fits (found: {usable_names})",
            "download an MLX coding model (20-35B, 4-bit) into the Hugging Face cache, then rerun `dfm setup`",
        )
    else:
        need = primary.memory_gb(context)
        report.add(
            "dfm-primary",
            "ok",
            f"{primary.describe()}; needs ~{need:.1f} GB at {context // 1024}k context, {primary.size_gb:.1f} GB on disk",
        )
        report.add(
            "dfm-small",
            "ok",
            small.describe() + (" (same weights as primary)" if small.path == primary.path else ""),
        )
        current = (config.models.primary.model, config.models.fallback.model)
        wanted = (str(primary.path), str(small.path))
        if current == wanted:
            report.add("config", "ok", "models already configured")
        elif dry_run:
            report.add(
                "config",
                "skip",
                "dry run: would set models.primary.model and models.fallback.model",
            )
        elif confirm(f"Use {primary.name} as dfm-primary and {small.name} as dfm-small?"):
            set_user_value("models.primary.model", wanted[0])
            path = set_user_value("models.fallback.model", wanted[1])
            report.add("config", "changed", f"written to {path}")
        else:
            report.add("config", "skip", "not changed", "dfm models use primary <name>")

    if dry_run:
        report.add("state dirs", "skip", f"dry run: would create {dirs.data}")
    else:
        dirs.ensure()
        report.add("state dirs", "ok", str(dirs.data))

    offline = load_config(None, ConfigOverrides(offline=True)).config
    net = NetworkPolicy(offline)
    remote_denied = all(net.check(c, "x").decision.value == "deny" for c in NetworkCategory)
    inference_denied = Policy(DFMConfig()).config.network.remote_inference == "deny"
    report.add(
        "offline mode",
        "ok" if remote_denied and inference_denied else "fail",
        "all network denied with --offline; remote inference denied by default",
    )

    if evaluate_models and primary is not None and mlx_available():
        from dfm.models.mlx.evaluate import evaluate

        env = ExecutionEnvironment.create(repo or Path.cwd(), DFMConfig(), session_id="setup-eval")
        try:
            result = await evaluate(
                MLXModel(
                    LocalModelSpec("small", primary.path, primary.name, context=16384),
                    env,
                    resources=resources,
                )
            )
        finally:
            env.close()
        status: StepStatus = "ok" if result.score >= 2 / 3 else "warn"
        report.add(
            "coder check",
            status,
            f"{len(result.passed)}/3 checks, {result.tokens_per_s} tok/s, peak {result.peak_gb} GB",
        )
    return report
