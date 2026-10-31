"""`dfm doctor`: what works on this machine, what is degraded, and how to fix it.

Read-only and offline: no network calls, no paid APIs, nothing written. Each
probe goes through `Probe` so tests can fake the machine.
"""

import importlib.util
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

from dfm.config import ConfigError, LoadedConfig, load_config
from dfm.security.sandbox import already_sandboxed


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class CheckResult:
    group: str
    name: str
    status: Status
    detail: str
    remedy: str = ""
    blocking: bool = False  # a FAIL here means DFM cannot do core coding work


@dataclass
class Probe:
    which: Callable[[str], str | None] = shutil.which
    run: Callable[[Sequence[str]], tuple[int, str]] = field(default_factory=lambda: _run)
    find_module: Callable[[str], bool] = field(
        default_factory=lambda: lambda name: importlib.util.find_spec(name) is not None
    )
    system: Callable[[], str] = platform.system
    machine: Callable[[], str] = platform.machine
    mac_version: Callable[[], str] = field(default_factory=lambda: lambda: platform.mac_ver()[0])
    python_version: tuple[int, int] = field(default_factory=lambda: sys.version_info[:2])
    disk_free: Callable[[Path], int] = field(
        default_factory=lambda: lambda p: shutil.disk_usage(p).free
    )
    home: Path = field(default_factory=Path.home)


def _run(argv: Sequence[str]) -> tuple[int, str]:
    try:
        out = subprocess.run(  # noqa: S603
            list(argv), capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    text = re.sub(r"\x1b\[[0-9;]*m", "", out.stdout + out.stderr)
    return out.returncode, text.strip()


def _first_line(text: str) -> str:
    return text.splitlines()[0].strip() if text.strip() else ""


def _sysctl(probe: Probe, key: str) -> str:
    code, out = probe.run(["sysctl", "-n", key])
    return out.strip() if code == 0 else ""


GB = 1024**3

TOOLCHAINS = (
    ("python3", ["python3", "--version"]),
    ("cargo", ["cargo", "--version"]),
    ("rustfmt", ["rustfmt", "--version"]),
    ("clippy", ["cargo", "clippy", "--version"]),
    ("go", ["go", "version"]),
    ("node", ["node", "--version"]),
    ("pnpm", ["pnpm", "--version"]),
    ("npm", ["npm", "--version"]),
    ("bun", ["bun", "--version"]),
    ("swift", ["swift", "--version"]),
    ("forge", ["forge", "--version"]),
    ("solc", ["solc", "--version"]),
    ("scarb", ["scarb", "--version"]),
    ("snforge", ["snforge", "--version"]),
)
# (server, version argv): rustup installs a rust-analyzer proxy even without the component
LSP_SERVERS: tuple[tuple[str, list[str] | None], ...] = (
    ("rust-analyzer", ["rust-analyzer", "--version"]),
    ("gopls", ["gopls", "version"]),
    ("typescript-language-server", None),
    ("pyright-langserver", None),
    ("sourcekit-lsp", None),
)


def run_checks(probe: Probe | None = None, loaded: LoadedConfig | None = None) -> list[CheckResult]:
    probe = probe or Probe()
    results: list[CheckResult] = []
    add = results.append

    # ---- configuration
    if loaded is None:
        try:
            loaded = load_config(None)
        except ConfigError as exc:
            add(
                CheckResult(
                    "config", "config", Status.FAIL, str(exc), "fix ~/.config/dfm/config.toml", True
                )
            )
    if loaded is not None:
        add(CheckResult("config", "config", Status.OK, f"{loaded.user_path}"))
        rule = loaded.config.network.remote_inference
        if rule == "deny":
            add(CheckResult("policy", "remote inference", Status.OK, "DENIED (local models only)"))
        else:
            add(
                CheckResult(
                    "policy",
                    "remote inference",
                    Status.WARN,
                    f"network.remote_inference = {rule}",
                    "set it back to 'deny' unless you mean it",
                )
            )

    # ---- machine
    if probe.system() == "Darwin":
        add(CheckResult("machine", "macOS", Status.OK, probe.mac_version() or "unknown version"))
    else:
        add(CheckResult("machine", "macOS", Status.WARN, f"{probe.system()}: DFM targets macOS"))
    chip = _sysctl(probe, "machdep.cpu.brand_string")
    if probe.machine() == "arm64":
        add(CheckResult("machine", "Apple Silicon", Status.OK, chip or "arm64"))
    else:
        add(
            CheckResult(
                "machine",
                "Apple Silicon",
                Status.WARN,
                f"{probe.machine()}: MLX needs Apple Silicon",
            )
        )
    mem = _sysctl(probe, "hw.memsize")
    if mem.isdigit():
        gb = int(mem) / GB
        status = Status.OK if gb >= 32 else Status.WARN
        remedy = "" if gb >= 32 else "a 20-35B coder needs ~32 GB; dfm-small only"
        add(CheckResult("machine", "memory", status, f"{gb:.0f} GB unified memory", remedy))
    else:
        add(CheckResult("machine", "memory", Status.SKIP, "unknown"))
    free = probe.disk_free(probe.home) / GB
    add(
        CheckResult(
            "machine",
            "disk",
            Status.OK if free >= 40 else Status.WARN,
            f"{free:.0f} GB free",
            "" if free >= 40 else "local models need 15-40 GB",
        )
    )

    # ---- Apple FM
    if probe.which("fm") is None:
        add(
            CheckResult(
                "models",
                "fm CLI",
                Status.WARN,
                "not found",
                "Apple FM needs macOS 26+ with Apple Intelligence; DFM falls back to local coders",
            )
        )
    else:
        code, out = probe.run(["fm", "available"])
        if code == 0 and "available" in out.lower() and "not" not in out.lower():
            add(CheckResult("models", "Apple FM", Status.OK, _first_line(out)))
        else:
            add(
                CheckResult(
                    "models",
                    "Apple FM",
                    Status.WARN,
                    _first_line(out) or f"exit {code}",
                    "enable Apple Intelligence; run `fm license` once",
                )
            )

    # ---- Python / MLX / local models
    py_ok = probe.python_version >= (3, 12)
    add(
        CheckResult(
            "runtime",
            "Python",
            Status.OK if py_ok else Status.FAIL,
            ".".join(map(str, probe.python_version)),
            "" if py_ok else "DFM needs Python 3.12+",
            blocking=not py_ok,
        )
    )
    uv = probe.which("uv")
    if uv:
        add(CheckResult("runtime", "uv", Status.OK, _first_line(probe.run(["uv", "--version"])[1])))
    else:
        add(CheckResult("runtime", "uv", Status.WARN, "not found", "brew install uv"))
    for module, label in (("mlx", "MLX"), ("mlx_lm", "mlx-lm")):
        if probe.find_module(module):
            add(CheckResult("models", label, Status.OK, "installed"))
        else:
            add(
                CheckResult(
                    "models",
                    label,
                    Status.WARN,
                    "not installed",
                    "run `dfm setup` (uv sync --extra mlx)",
                )
            )
    if loaded is not None:
        for role, settings in (
            ("dfm-primary", loaded.config.models.primary),
            ("dfm-small", loaded.config.models.fallback),
        ):
            if not settings.model:
                add(
                    CheckResult(
                        "models",
                        role,
                        Status.WARN,
                        "not configured",
                        "run `dfm setup` or `dfm models use`",
                    )
                )
            elif Path(settings.model).expanduser().exists():
                add(CheckResult("models", role, Status.OK, settings.model))
            else:
                add(
                    CheckResult(
                        "models",
                        role,
                        Status.WARN,
                        f"{settings.model}: not found locally",
                        "run `dfm models`",
                    )
                )

    # ---- integrations
    for module, label in (("repocanon", "RepoCanon"), ("repokg", "RepoKG")):
        if probe.find_module(module):
            add(CheckResult("integrations", label, Status.OK, "importable"))
        else:
            add(
                CheckResult(
                    "integrations",
                    label,
                    Status.WARN,
                    "not installed (degraded discovery)",
                    "uv sync (sibling checkout ../" + module + ")",
                )
            )
    if probe.which("reflex"):
        add(
            CheckResult(
                "integrations",
                "Reflex",
                Status.OK,
                _first_line(probe.run(["reflex", "--version"])[1]),
            )
        )
    else:
        add(
            CheckResult(
                "integrations",
                "Reflex",
                Status.WARN,
                "not found (no historical memory)",
                "npm i -g reflex-skills",
            )
        )

    # ---- tools
    if probe.which("git"):
        add(CheckResult("tools", "git", Status.OK, _first_line(probe.run(["git", "--version"])[1])))
    else:
        add(
            CheckResult(
                "tools", "git", Status.FAIL, "not found", "xcode-select --install", blocking=True
            )
        )
    for tool, remedy in (
        ("gh", "brew install gh (GitHub features)"),
        ("rg", "brew install ripgrep (faster search)"),
        ("fd", "brew install fd (faster file search)"),
        ("docker", "Docker Desktop / OrbStack (services)"),
        ("sandbox-exec", "sandboxing of repository code is degraded"),
    ):
        path = probe.which(tool)
        add(
            CheckResult(
                "tools",
                tool,
                Status.OK if path else Status.WARN,
                path or "not found",
                "" if path else remedy,
            )
        )

    if already_sandboxed():
        add(
            CheckResult(
                "security",
                "sandbox",
                Status.OK,
                "running inside a sandbox already",
                "its limits apply to everything DFM starts (macOS cannot nest sandboxes)",
            )
        )
    elif probe.which("sandbox-exec"):
        code, out = probe.run(["sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"])
        if code == 0:
            add(CheckResult("security", "sandbox", Status.OK, "sandbox-exec profiles apply"))
        else:
            add(
                CheckResult(
                    "security",
                    "sandbox",
                    Status.WARN,
                    _first_line(out) or f"exit {code}",
                    "repository code runs with env scrubbing only",
                )
            )
    else:
        add(
            CheckResult(
                "security",
                "sandbox",
                Status.WARN,
                "sandbox-exec not available",
                "repository code runs with env scrubbing only",
            )
        )

    for name, argv in TOOLCHAINS:
        if probe.which(argv[0]):
            code, out = probe.run(argv)
            add(
                CheckResult(
                    "toolchains", name, Status.OK if code == 0 else Status.WARN, _first_line(out)
                )
            )
        else:
            add(CheckResult("toolchains", name, Status.SKIP, "not installed"))
    for server, version in LSP_SERVERS:
        path = probe.which(server)
        if path and version is not None and probe.run(version)[0] != 0:
            add(
                CheckResult(
                    "lsp",
                    server,
                    Status.WARN,
                    f"{path} does not run",
                    "install the component (rustup component add rust-analyzer)"
                    if server == "rust-analyzer"
                    else "",
                )
            )
            continue
        add(CheckResult("lsp", server, Status.OK if path else Status.SKIP, path or "not installed"))
    return results


def exit_code(results: Sequence[CheckResult]) -> int:
    return 1 if any(r.blocking and r.status is Status.FAIL for r in results) else 0


def as_json(results: Sequence[CheckResult]) -> list[dict[str, object]]:
    return [{**asdict(r), "status": r.status.value} for r in results]
