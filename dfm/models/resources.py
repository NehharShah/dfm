"""ModelResourceManager: don't freeze the developer's machine.

Hardware and live memory are detected, never assumed. Before a local model
loads, `admit` decides: load at the requested context, load with a smaller
context, use dfm-small instead, or refuse (helper / deterministic paths only).
DFM never kills other processes to make room.
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dfm.models.mlx.discovery import GB, ModelCandidate
from dfm.routing import RouteDecision, RoutingContext

Runner = Callable[[Sequence[str]], str]


def _run(argv: Sequence[str]) -> str:
    try:
        return subprocess.run(  # noqa: S603  (fixed sysctl/vm_stat argv)
            list(argv), capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


@dataclass(frozen=True)
class HardwareInfo:
    chip: str
    arch: str
    cpu_cores: int
    performance_cores: int
    efficiency_cores: int
    memory_bytes: int
    macos: str
    disk_free_bytes: int

    @property
    def memory_gb(self) -> float:
        return self.memory_bytes / GB


@dataclass(frozen=True)
class ResourceSnapshot:
    total_bytes: int
    # free + inactive + speculative + purgeable pages: what macOS gives back without compressing
    # the developer's running apps (kern.memorystatus_level counts their memory as free too)
    available_bytes: int
    swap_used_bytes: int
    swap_total_bytes: int

    @property
    def available_gb(self) -> float:
        return self.available_bytes / GB

    @property
    def swap_used_gb(self) -> float:
        return self.swap_used_bytes / GB


Decision = Literal["ok", "reduce_context", "use_small", "deny"]


@dataclass(frozen=True)
class Admission:
    decision: Decision
    context: int
    need_gb: float
    available_gb: float
    reason: str


def _int(text: str) -> int:
    digits = re.sub(r"[^0-9]", "", text)
    return int(digits) if digits else 0


def _size(token: str) -> int:
    match = re.match(r"([\d.]+)([KMGT])", token.strip())
    if not match:
        return 0
    return int(
        float(match.group(1))
        * {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[match.group(2)]
    )


class ModelResourceManager:
    def __init__(
        self,
        runner: Runner = _run,
        *,
        # on top of `available`, which already excludes running apps: a smaller reserve than
        # the original 15%, which blocked a 27B model on a 48 GB Mac with a browser open
        headroom_fraction: float = 0.08,
        min_headroom_gb: float = 4.0,
        min_context: int = 4096,
        home: Path | None = None,
    ) -> None:
        self.run = runner
        self.headroom_fraction = headroom_fraction
        self.min_headroom_gb = min_headroom_gb
        self.min_context = min_context
        self.home = home or Path.home()

    def hardware(self) -> HardwareInfo:
        def sysctl(key: str) -> str:
            return self.run(["sysctl", "-n", key]).strip()

        return HardwareInfo(
            chip=sysctl("machdep.cpu.brand_string") or platform.processor() or "unknown",
            arch=platform.machine(),
            cpu_cores=_int(sysctl("hw.ncpu")),
            performance_cores=_int(sysctl("hw.perflevel0.physicalcpu")),
            efficiency_cores=_int(sysctl("hw.perflevel1.physicalcpu")),
            memory_bytes=_int(sysctl("hw.memsize")),
            macos=platform.mac_ver()[0],
            disk_free_bytes=shutil.disk_usage(self.home).free if self.home.exists() else 0,
        )

    def snapshot(self) -> ResourceSnapshot:
        vm = self.run(["vm_stat"])
        page_match = re.search(r"page size of (\d+)", vm)
        page = int(page_match.group(1)) if page_match else 16384
        pages: dict[str, int] = {}
        for line in vm.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                pages[key.strip()] = _int(value)
        available = (
            sum(
                pages.get(k, 0)
                for k in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable")
            )
            * page
        )
        total = _int(self.run(["sysctl", "-n", "hw.memsize"]))
        swap = self.run(["sysctl", "-n", "vm.swapusage"])
        total_swap = re.search(r"total = (\S+)", swap)
        used_swap = re.search(r"used = (\S+)", swap)
        return ResourceSnapshot(
            total_bytes=total,
            available_bytes=available,
            swap_used_bytes=_size(used_swap.group(1)) if used_swap else 0,
            swap_total_bytes=_size(total_swap.group(1)) if total_swap else 0,
        )

    def headroom_gb(self, snap: ResourceSnapshot) -> float:
        return max(self.min_headroom_gb, snap.total_bytes / GB * self.headroom_fraction)

    def budget_gb(
        self, snap: ResourceSnapshot | None = None, *, already_loaded_gb: float = 0.0
    ) -> float:
        snap = snap or self.snapshot()
        return snap.available_gb + already_loaded_gb - self.headroom_gb(snap)

    def admit(
        self,
        model: ModelCandidate,
        context: int,
        *,
        small: ModelCandidate | None = None,
        snap: ResourceSnapshot | None = None,
        already_loaded_gb: float = 0.0,
    ) -> Admission:
        snap = snap or self.snapshot()
        budget = self.budget_gb(snap, already_loaded_gb=already_loaded_gb)
        need = model.memory_gb(context)
        if need <= budget:
            return Admission(
                "ok",
                context,
                need,
                budget,
                f"{need:.1f} GB needed, {budget:.1f} GB available after headroom",
            )
        reduced = context
        while reduced // 2 >= self.min_context:
            reduced //= 2
            if model.memory_gb(reduced) <= budget:
                return Admission(
                    "reduce_context",
                    reduced,
                    model.memory_gb(reduced),
                    budget,
                    f"context reduced to {reduced} to fit {budget:.1f} GB",
                )
        if (
            small is not None
            and small.path != model.path
            and small.memory_gb(min(context, 16384)) <= budget
        ):
            return Admission(
                "use_small",
                min(context, 16384),
                small.memory_gb(min(context, 16384)),
                budget,
                f"{model.name} needs {need:.1f} GB; only {budget:.1f} GB free: using dfm-small",
            )
        return Admission(
            "deny",
            0,
            need,
            budget,
            f"not enough memory for a local coder ({budget:.1f} GB available after headroom)",
        )

    def router_adjuster(
        self,
        primary: ModelCandidate | None,
        small: ModelCandidate | None,
        context: int,
        *,
        primary_loaded: Callable[[], bool] = lambda: False,
    ) -> Callable[[RouteDecision, str, RoutingContext], RouteDecision]:
        """Downgrade PRIMARY to small when memory says so (a router adjuster).

        A primary that is already loaded fits by definition; its own memory must not count
        against it (that would load the small coder next to it).
        """

        def adjust(decision: RouteDecision, request: str, ctx: RoutingContext) -> RouteDecision:
            if decision.coder != "primary" or primary is None or primary_loaded():
                return decision
            admission = self.admit(primary, context, small=small)
            if admission.decision == "use_small":
                return decision.with_coder("small", admission.reason, "resources")
            return decision

        return adjust
