"""OS-level limits for repository code DFM runs.

Policy decides whether a command line may run; the sandbox limits what the
code it starts can do: tests, build scripts and package hooks are repository
code and may be hostile. On macOS this is a `sandbox-exec` (SBPL) profile
generated per run:

- reads denied in protected home locations (keys, cloud creds, keychains,
  wallets, browser profiles), even when a test tries;
- writes allowed only in the repository, DFM's state dirs, temp dirs and the
  usual toolchain caches;
- outbound network denied unless the command was authorized for a network
  category (loopback and Unix sockets always allowed, so local services work).

`sandbox-exec` is deprecated by Apple but still shipped; when it is missing the
wrap is a no-op and doctor reports the degraded state (env scrubbing still applies).
"""

from __future__ import annotations

import ctypes
import functools
import os
import platform
import re
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from dfm.config.paths import cache_home
from dfm.execution import DEFAULT_DENIED_HOME_PATHS, ExecutionEnvironment
from dfm.policy import ActionClass
from dfm.policy.network import NetworkMode
from dfm.tools.shell.classify import Classification

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Relative to $HOME: caches toolchains write during normal builds/tests.
TOOLCHAIN_WRITE_DIRS = (
    ".cache",
    "Library/Caches",
    ".cargo/registry",
    ".cargo/git",
    ".rustup/tmp",
    ".npm",
    ".pnpm-store",
    ".local/share/pnpm",
    ".yarn",
    ".bun/install/cache",
    "go/pkg",
    ".gradle",
    ".m2",
    ".swiftpm",
    "Library/Developer/Xcode/DerivedData",
    "Library/org.swift.swiftpm",
    ".foundry/cache",
    ".svm",
    ".scarb",
    ".local/share/uv",
)
SYSTEM_WRITE_DIRS = ("/private/tmp", "/private/var/folders", "/dev")
_NO_CODE = frozenset({ActionClass.SAFE_READ, ActionClass.SAFE_LOCAL})  # cannot open a socket
# Read-only by classification, yet able to start another program or write a file given the
# right script or option (`sed e`, `awk | "sh"`, `less !cmd`, `find -exec`, `rg --pre`, `git`
# helpers). The rules catch the known spellings; the sandbox does not take their word for it
# when deciding whether a compound command may have the network.
_CAN_RUN_PROGRAMS = frozenset(
    {
        "sed",
        "gsed",
        "awk",
        "gawk",
        "nawk",
        "mawk",
        "less",
        "more",
        "man",
        "find",
        "fd",
        "rg",
        "git",
        "tar",
        "vim",
        "vi",
        "nvim",
        "emacs",
        "nano",
    }
)


def dfm_private_paths(env: ExecutionEnvironment) -> list[Path]:
    """Paths repository code must never write, even though they sit inside areas it may
    otherwise write: what lands there is run or trusted by DFM later, outside the sandbox.

    - `<repo>/.dfm`: checkpoint originals that `undo` restores, repository policy
    - `~/.cache/dfm`: the persisted KV cache the worker restores as the model's prefix
    - DFM's own interpreter and its venv (`~/.local/share/uv/...` is a toolchain cache
      otherwise): a `sitecustomize.py` there runs in every later `dfm` invocation
    """
    return [
        env.repo_root / ".dfm",
        cache_home(env.source_env),
        Path(sys.prefix),
        Path(sys.base_prefix),
    ]


def _quote(path: str | Path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass(frozen=True)
class SandboxProfile:
    text: str
    network: bool


@functools.cache
def already_sandboxed() -> bool:
    """This process runs inside a sandbox already (DFM run by DFM, e.g. its own test suite).
    macOS cannot nest them: sandbox-exec fails with "sandbox_apply: Operation not permitted"."""
    if platform.system() != "Darwin":
        return False
    try:
        lib = ctypes.CDLL("/usr/lib/libSystem.dylib")
        lib.sandbox_check.restype = ctypes.c_int
        lib.sandbox_check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        return lib.sandbox_check(os.getpid(), None, 0) != 0
    except (OSError, AttributeError):
        return False


def available() -> bool:
    """A new sandbox can be applied. Inside one already, the outer sandbox's limits keep
    applying to everything started here; a second one cannot be added."""
    return (
        platform.system() == "Darwin"
        and Path(SANDBOX_EXEC).exists()
        and shutil.which(SANDBOX_EXEC) is not None
        and not already_sandboxed()
    )


def build_profile(
    env: ExecutionEnvironment,
    *,
    network: bool,
    extra_write: Sequence[Path] = (),
    read_only_repo: bool = False,
) -> SandboxProfile:
    """`read_only_repo`: the repository itself is not writable (plan mode, review mode).
    `extra_write`: paths this one process may write even where repository code may not (the
    model worker's KV cache directory)."""
    home = Path(os.path.realpath(env.home))
    denied = [home / p for p in DEFAULT_DENIED_HOME_PATHS]
    roots = [r for r in env.boundary.roots if not (read_only_repo and r == env.repo_root)]
    writable = [
        *roots,
        Path(os.path.realpath(env.tmp)),
        *(home / d for d in TOOLCHAIN_WRITE_DIRS),
        *(Path(d) for d in SYSTEM_WRITE_DIRS),
        *extra_write,
    ]
    lines = ["(version 1)", "(allow default)"]
    deny_paths = " ".join(f"(subpath {_quote(os.path.realpath(p))})" for p in denied)
    lines.append(f"(deny file-read* file-write* {deny_paths})")
    allow_writes = " ".join(f"(subpath {_quote(os.path.realpath(p))})" for p in writable)
    lines.append(f"(deny file-write* (require-not (require-any {allow_writes})))")
    # Later SBPL rules win. Repository code must not rewrite DFM's config or state (policy,
    # process registry), apart from this session's temp dir.
    private = " ".join(
        f"(subpath {_quote(os.path.realpath(p))})"
        for p in (*env.state_roots, *dfm_private_paths(env))
    )
    lines.append(f"(deny file-write* {private})")
    # and never reads DFM's policy, traces, logs, metrics, registry or caches
    if env.private_state:
        unreadable = " ".join(f"(subpath {_quote(p)})" for p in env.private_state)
        lines.append(f"(deny file-read* {unreadable})")
    lines.append(f"(allow file-write* (subpath {_quote(os.path.realpath(env.tmp))}))")
    for path in extra_write:
        lines.append(f"(allow file-write* (subpath {_quote(os.path.realpath(path))}))")
    # Nor plant code that Git runs later, outside the sandbox, as DFM or the developer: hooks and
    # config (core.fsmonitor, aliases, filters), in the repo, its submodules and linked worktrees.
    git = re.escape(os.path.realpath(env.repo_root / ".git"))
    lines.append(
        f'(deny file-write* (regex #"^{git}(/(modules|worktrees)/.+)?/'
        f'(config|config\\.worktree|hooks(/.*)?)$"))'
    )
    if read_only_repo:
        lines.append(f"(deny file-write* (subpath {_quote(os.path.realpath(env.repo_root))}))")
    if not network:
        lines.append("(deny network-outbound)")
        lines.append('(allow network-outbound (remote ip "localhost:*"))')
        lines.append("(allow network-outbound (remote unix-socket))")
    return SandboxProfile("\n".join(lines), network)


class Sandbox:
    """`CommandRunner(wrap=Sandbox().wrap)`."""

    def __init__(self, *, enabled: bool = True, read_only_repo: bool = False) -> None:
        self.enabled = enabled and available()
        self.read_only_repo = read_only_repo

    def wrap(
        self, argv: Sequence[str], env: ExecutionEnvironment, classification: Classification
    ) -> Sequence[str]:
        if not self.enabled:
            return argv
        # One process gets one network setting. If any part runs code without a network
        # approval of its own (`git fetch && python exfil.py`), the whole command stays offline.
        unapproved_code = any(
            part.network is None
            and (
                part.action not in _NO_CODE
                or (part.argv and part.argv[0].rsplit("/", 1)[-1] in _CAN_RUN_PROGRAMS)
            )
            for part in classification.parts
        )
        network = (
            classification.network is not None
            and not unapproved_code
            and env.network is not NetworkMode.OFFLINE
        )
        profile = build_profile(env, network=network, read_only_repo=self.read_only_repo)
        return [SANDBOX_EXEC, "-p", profile.text, *argv]
