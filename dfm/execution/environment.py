"""Where and how tools run: cwd, filesystem boundary, scrubbed env, network mode, limits.

Every filesystem path a tool touches goes through `resolve`, which follows
symlinks first and then checks the real path against the boundary, so a link
inside the repository cannot reach `~/.ssh`.
"""

import os
import re
import shutil
import sys
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from dfm.config import ConfigOverrides, DFMConfig, StateDirs, state_dirs
from dfm.policy.network import NetworkMode, network_mode


class BoundaryViolation(PermissionError):
    """A path outside the repository boundary, or inside a denied location."""


# Relative to the home directory. Denied even if the user approves a parent path.
DEFAULT_DENIED_HOME_PATHS = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".docker",
    ".azure",
    ".config/gcloud",
    ".config/gh",
    ".config/solana",
    ".netrc",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
    ".password-store",
    ".cargo/credentials",
    ".cargo/credentials.toml",
    ".terraform.d/credentials.tfrc.json",
    ".foundry/keystores",
    ".starkli-wallets",
    ".ethereum/keystore",
    ".near-credentials",
    "Library/Keychains",
    "Library/Cookies",
    "Library/Safari",
    "Library/Application Support/Google/Chrome",
    "Library/Application Support/BraveSoftware",
    "Library/Application Support/Firefox",
    "Library/Application Support/Arc",
    "Library/Application Support/Microsoft Edge",
    "Library/Application Support/1Password",
    ".mozilla",
    ".config/google-chrome",
    ".config/chromium",
)


def _under(path: Path, root: Path) -> bool:
    """`path` is `root` or inside it. Case-insensitive on macOS, as its file system is by
    default: `.GIT/hooks` is `.git/hooks` there."""
    if sys.platform == "darwin":
        p, r = str(path).casefold(), str(root).casefold().rstrip("/")
        return p == r or p.startswith(r + "/")
    return path == root or path.is_relative_to(root)


class FsBoundary:
    def __init__(self, roots: Sequence[Path], *, home: Path, denied: Sequence[Path] = ()) -> None:
        self._roots = [Path(os.path.realpath(r)) for r in roots]
        denied_paths = [home / p for p in DEFAULT_DENIED_HOME_PATHS] + list(denied)
        self._denied = [Path(os.path.realpath(d)) for d in denied_paths]

    @property
    def roots(self) -> tuple[Path, ...]:
        return tuple(self._roots)

    def denied_reason(self, real: Path) -> str | None:
        for denied in self._denied:
            if _under(real, denied):
                return f"{real} is in a protected location ({denied})"
        return None

    def check(self, path: Path) -> Path:
        real = Path(os.path.realpath(path))
        reason = self.denied_reason(real)
        if reason:
            raise BoundaryViolation(reason)
        if not any(real == root or real.is_relative_to(root) for root in self._roots):
            raise BoundaryViolation(f"{real} is outside the repository boundary")
        return real

    def contains(self, path: Path) -> bool:
        try:
            self.check(path)
        except BoundaryViolation:
            return False
        return True

    def approve(self, path: Path) -> Path:
        """Add a session-approved external path. Protected locations cannot be approved."""
        real = Path(os.path.realpath(path))
        reason = self.denied_reason(real)
        if reason:
            raise BoundaryViolation(reason)
        # Approving a parent of a protected location is fine: check() still denies inside it.
        self._roots.append(real)
        return real


@dataclass(frozen=True)
class ResourceLimits:
    wall_time_s: float = 600.0
    output_bytes: int = 8 * 1024 * 1024
    read_text_bytes: int = 256 * 1024  # larger files are stubbed unless a line range is asked for
    read_range_lines: int = 2000  # the most lines one ranged read of a large file returns


_BASE_ENV_ALLOW = re.compile(
    r"^(PATH|HOME|USER|LOGNAME|SHELL|LANG|LC_[A-Z]+|TERM|COLORTERM|TZ|TMPDIR|PWD"
    r"|XDG_[A-Z_]+|CARGO_HOME|RUSTUP_HOME|RUSTUP_TOOLCHAIN|RUSTFLAGS|CARGO_TARGET_DIR"
    r"|GOPATH|GOROOT|GOMODCACHE|GOCACHE|GOFLAGS|GOPROXY|GOPRIVATE|GONOSUMDB|GOTOOLCHAIN"
    r"|VIRTUAL_ENV|UV_[A-Z_]+|PYTHONPATH|PYTHONDONTWRITEBYTECODE|PIP_[A-Z_]+|CONDA_PREFIX"
    r"|NVM_DIR|NODE_PATH|NODE_ENV|PNPM_HOME|BUN_INSTALL|COREPACK_HOME|NPM_CONFIG_PREFIX"
    r"|JAVA_HOME|ANDROID_HOME|DEVELOPER_DIR|SDKROOT|HOMEBREW_[A-Z_]+|SSL_CERT_FILE"
    r"|SSL_CERT_DIR|REQUESTS_CA_BUNDLE|NODE_EXTRA_CA_CERTS|CI|FOUNDRY_PROFILE|SCARB_[A-Z_]+"
    r"|DOCKER_HOST|DOCKER_CONTEXT|COMPOSE_[A-Z_]+|NO_COLOR|FORCE_COLOR|KUBECONFIG)$"
)
_SECRET_NAME = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS$|CREDENTIAL|AUTH|COOKIE|SESSION|MNEMONIC"
    r"|SEED|PRIVATE|_URL$|_URI$|DSN)",
    re.I,
)

_OFFLINE_ENV = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "UV_OFFLINE": "1",
    "PIP_NO_INDEX": "1",
    "CARGO_NET_OFFLINE": "true",
    "GOPROXY": "off",
    "GOFLAGS": "-mod=mod",
    "npm_config_offline": "true",
    "YARN_ENABLE_NETWORK": "0",
}
_ALWAYS_ENV = {
    # Edits and test runs happen within seconds; never leave bytecode that could go stale.
    "PYTHONDONTWRITEBYTECODE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "DO_NOT_TRACK": "1",
    "NEXT_TELEMETRY_DISABLED": "1",
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
}


def scrub_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Allowlisted variables minus anything whose name looks secret."""
    return {
        k: v for k, v in environ.items() if _BASE_ENV_ALLOW.match(k) and not _SECRET_NAME.search(k)
    }


@dataclass
class ExecutionEnvironment:
    repo_root: Path
    cwd: Path
    boundary: FsBoundary
    network: NetworkMode
    tmp: Path
    home: Path
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    source_env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    # DFM's own config and data dirs: inside the boundary for DFM, never writable by the code it
    # runs (it could rewrite DFM's policy or forge its process registry)
    state_roots: tuple[Path, ...] = ()

    @classmethod
    def create(
        cls,
        repo_root: Path,
        config: DFMConfig,
        *,
        session_id: str,
        overrides: ConfigOverrides | None = None,
        environ: Mapping[str, str] | None = None,
        dirs: StateDirs | None = None,
        limits: ResourceLimits | None = None,
    ) -> "ExecutionEnvironment":
        environ = dict(os.environ if environ is None else environ)
        dirs = dirs or state_dirs(environ)
        home = Path(environ.get("HOME") or Path.home())
        repo = Path(os.path.realpath(repo_root))
        tmp = dirs.data / "tmp" / session_id
        tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
        roots = (
            [repo, dirs.data, dirs.config]
            if config.security.repo_filesystem_boundary
            else [Path("/")]
        )
        return cls(
            repo_root=repo,
            cwd=repo,
            boundary=FsBoundary(roots, home=home),
            network=network_mode(config, overrides),
            tmp=Path(os.path.realpath(tmp)),
            home=home,
            limits=limits
            or ResourceLimits(
                read_text_bytes=config.limits.read_text_bytes,
                read_range_lines=config.limits.read_range_lines,
            ),
            source_env=environ,
            state_roots=(
                Path(os.path.realpath(dirs.data)),
                Path(os.path.realpath(dirs.config)),
            ),
        )

    def _absolute(self, p: str | Path) -> Path:
        text = str(p)
        if text == "~" or text.startswith("~/"):
            return self.home / text[2:]
        path = Path(text)
        return path if path.is_absolute() else self.cwd / path

    def resolve(self, p: str | Path, *, write: bool = False) -> Path:
        real = self.boundary.check(self._absolute(p))
        if write and _under(real, self.repo_root / ".git"):
            raise BoundaryViolation(f"{real}: Git internals are written only through git")
        return real

    def is_inside(self, p: str) -> bool:
        """Predicate for the shell classifier."""
        try:
            self.resolve(p)
        except BoundaryViolation:
            return False
        return True

    def approve_path(self, p: str | Path) -> Path:
        return self.boundary.approve(self._absolute(p))

    def child_env(self, extra_secret_names: Sequence[str] = ()) -> dict[str, str]:
        """Environment for a subprocess. Secrets appear only when a tool asks for them by name."""
        env = scrub_env(self.source_env)
        env.update(_ALWAYS_ENV)
        env["TMPDIR"] = str(self.tmp)
        env["PYTHONPYCACHEPREFIX"] = str(self.tmp / "pycache")  # bytecode never lands in the repo
        env["PWD"] = str(self.cwd)
        if self.network is NetworkMode.OFFLINE:
            env.update(_OFFLINE_ENV)
        for name in extra_secret_names:
            if name in self.source_env:
                env[name] = self.source_env[name]
        return env

    def close(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    @contextmanager
    def session(self) -> Generator["ExecutionEnvironment"]:
        try:
            yield self
        finally:
            self.close()
