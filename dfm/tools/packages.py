"""Missing-package failures → the exact install command to ask about.

DFM never installs silently. A missing package found in test or build output becomes an
`InstallProposal`: the command for this project's own tooling (`uv add`, `pnpm add`,
`cargo add`, `go get`, `forge install`, ...). Running it goes through policy like any
other PACKAGE_NETWORK command, so the developer is asked (and offline, it is only
reported). Names close to a popular package but not equal to it get a typo-squatting
warning. Imports of the repository's own modules and of standard libraries are ignored.
"""

from __future__ import annotations

import difflib
import re
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class InstallProposal:
    ecosystem: str
    package: str
    command: tuple[str, ...] | None  # None when the fix is a manifest edit DFM cannot infer
    reason: str
    typo_warning: str | None = None
    lockfile: str | None = None

    def describe(self) -> str:
        how = f"`{' '.join(self.command)}`" if self.command else "add it to the manifest"
        warn = f" (warning: {self.typo_warning})" if self.typo_warning else ""
        return f"missing {self.ecosystem} package {self.package}: {how}{warn}"


# Import name → distribution name where they differ.
PY_DIST = {
    "yaml": "pyyaml",
    "cv2": "opencv-python",
    "PIL": "pillow",
    "sklearn": "scikit-learn",
    "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil",
    "jwt": "pyjwt",
    "dotenv": "python-dotenv",
    "attr": "attrs",
    "Crypto": "pycryptodome",
    "magic": "python-magic",
    "fitz": "pymupdf",
    "google.protobuf": "protobuf",
    "serial": "pyserial",
    "zmq": "pyzmq",
    "OpenSSL": "pyopenssl",
    "git": "gitpython",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "multipart": "python-multipart",
}

POPULAR: dict[str, set[str]] = {
    "python": {
        "requests",
        "numpy",
        "pandas",
        "pyyaml",
        "pytest",
        "django",
        "flask",
        "fastapi",
        "pydantic",
        "click",
        "typer",
        "rich",
        "httpx",
        "aiohttp",
        "sqlalchemy",
        "boto3",
        "pillow",
        "scipy",
        "matplotlib",
        "jinja2",
        "attrs",
        "python-dateutil",
        "pyjwt",
        "cryptography",
        "urllib3",
        "certifi",
        "setuptools",
        "uvicorn",
        "redis",
        "celery",
        "beautifulsoup4",
        "lxml",
        "openpyxl",
        "psycopg2",
        "pymongo",
        "tqdm",
        "colorama",
    },
    "npm": {
        "react",
        "react-dom",
        "lodash",
        "express",
        "axios",
        "typescript",
        "vitest",
        "jest",
        "zod",
        "next",
        "vue",
        "chalk",
        "commander",
        "dotenv",
        "uuid",
        "moment",
        "dayjs",
        "webpack",
        "vite",
        "eslint",
        "prettier",
        "@types/node",
        "@types/react",
        "debug",
        "cross-env",
        "rimraf",
        "yargs",
        "ws",
        "socket.io",
        "mongoose",
        "pg",
        "redis",
    },
    "cargo": {
        "serde",
        "serde_json",
        "tokio",
        "anyhow",
        "thiserror",
        "clap",
        "rand",
        "regex",
        "reqwest",
        "log",
        "env_logger",
        "tracing",
        "chrono",
        "itertools",
        "once_cell",
        "futures",
        "async-trait",
        "bytes",
        "hyper",
        "axum",
        "uuid",
        "lazy_static",
    },
}

NODE_BUILTINS = {
    "assert",
    "buffer",
    "child_process",
    "crypto",
    "dns",
    "events",
    "fs",
    "fs/promises",
    "http",
    "https",
    "net",
    "os",
    "path",
    "process",
    "querystring",
    "readline",
    "stream",
    "string_decoder",
    "timers",
    "tls",
    "url",
    "util",
    "worker_threads",
    "zlib",
    "test",
}
RUST_ROOTS = {"std", "core", "alloc", "crate", "self", "super", "proc_macro", "test"}
FORGE_LIBS = {
    "forge-std": "foundry-rs/forge-std",
    "@openzeppelin": "OpenZeppelin/openzeppelin-contracts",
    "openzeppelin-contracts": "OpenZeppelin/openzeppelin-contracts",
    "solmate": "transmissions11/solmate",
    "solady": "Vectorized/solady",
}


def typo_warning(ecosystem: str, name: str) -> str | None:
    popular = POPULAR.get(ecosystem, set())
    if not popular or name.lower() in popular:
        return None
    close = difflib.get_close_matches(name.lower(), popular, n=1, cutoff=0.8)
    return (
        f"`{name}` is close to the popular `{close[0]}`; check it is not a typo-squat"
        if close
        else None
    )


def _is_local_python(repo: Path, module: str) -> bool:
    top = module.split(".")[0]
    for base in (repo, repo / "src"):
        if (base / top).is_dir() or (base / f"{top}.py").is_file():
            return True
    return False


def _python(repo: Path, module: str) -> InstallProposal | None:
    top = module.split(".")[0]
    if top in sys.stdlib_module_names or _is_local_python(repo, module):
        return None
    dist = PY_DIST.get(module) or PY_DIST.get(top) or top.replace("_", "-").lower()
    if (repo / "uv.lock").exists() or "[tool.uv" in _read(repo / "pyproject.toml"):
        command, lock = ("uv", "add", dist), "uv.lock"
    elif (repo / "poetry.lock").exists():
        command, lock = ("poetry", "add", dist), "poetry.lock"
    else:
        command, lock = ("python", "-m", "pip", "install", dist), None
    return InstallProposal(
        "python", dist, command, f"No module named '{module}'", typo_warning("python", dist), lock
    )


def _manager(repo: Path) -> tuple[str, str | None]:
    for lock, manager in (
        ("pnpm-lock.yaml", "pnpm"),
        ("bun.lock", "bun"),
        ("bun.lockb", "bun"),
        ("yarn.lock", "yarn"),
        ("package-lock.json", "npm"),
    ):
        if (repo / lock).exists():
            return manager, lock
    return "npm", "package-lock.json"


def _npm(repo: Path, spec: str, *, types_only: bool = False) -> InstallProposal | None:
    if spec.startswith((".", "/", "~", "#")):
        return None
    bare = spec.removeprefix("node:")
    if bare in NODE_BUILTINS or bare.split("/")[0] in NODE_BUILTINS:
        if not types_only:
            return None
        package, dev = "@types/node", True
    else:
        parts = spec.split("/")
        package = "/".join(parts[:2]) if spec.startswith("@") else parts[0]
        dev = package.startswith("@types/")
    manager, lock = _manager(repo)
    verb = {"npm": ("install",), "pnpm": ("add",), "yarn": ("add",), "bun": ("add",)}[manager]
    command = (manager, *verb, *(("-D",) if dev else ()), package)
    return InstallProposal(
        "npm", package, command, f"Cannot find module '{spec}'", typo_warning("npm", package), lock
    )


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


_PY = re.compile(r"(?:ModuleNotFoundError|ImportError): No module named '?([\w.]+)'?")
_PYRIGHT = re.compile(r'Import "([\w.]+)" could not be resolved(?! from source)')
_JS = re.compile(
    r"Cannot find module '([^']+)'(?: or its corresponding type declarations)?"
    r"|Can't resolve '([^']+)'|Cannot find package '([^']+)'"
)
_RUST = re.compile(
    r"unresolved import `(\w+)|use of undeclared crate or module `(\w+)`"
    r"|can't find crate for `(\w+)`"
)
_GO = re.compile(
    r"no required module provides package ([\w./~-]+);[^\n]*\n\s*go get ([\w./@~-]+)"
    r"|cannot find module providing package ([\w./~-]+)"
)
_SWIFT = re.compile(r"no such module '(\w+)'")
_SOL = re.compile(r'Source "([^"]+)" not found')


def missing_packages(text: str, repo: Path) -> list[InstallProposal]:
    """Every distinct missing package mentioned in tool output (test, build, type check)."""
    found: dict[tuple[str, str], InstallProposal] = {}

    def add(proposal: InstallProposal | None) -> None:
        if proposal is not None:
            found.setdefault((proposal.ecosystem, proposal.package), proposal)

    for m in _PY.finditer(text):
        add(_python(repo, m.group(1)))
    for m in _PYRIGHT.finditer(text):
        add(_python(repo, m.group(1)))
    for m in _JS.finditer(text):
        spec = next(g for g in m.groups() if g)
        add(_npm(repo, spec, types_only="corresponding type declarations" in m.group(0)))
    for m in _RUST.finditer(text):
        crate = next(g for g in m.groups() if g)
        if crate not in RUST_ROOTS:
            add(
                InstallProposal(
                    "cargo",
                    crate,
                    ("cargo", "add", crate),
                    m.group(0),
                    typo_warning("cargo", crate),
                    "Cargo.lock",
                )
            )
    for m in _GO.finditer(text):
        package = m.group(2) or m.group(3)
        add(
            InstallProposal(
                "go", package, ("go", "get", package), m.group(0).split("\n")[0], None, "go.sum"
            )
        )
    for m in _SWIFT.finditer(text):
        add(
            InstallProposal(
                "swift",
                m.group(1),
                None,
                f"no such module '{m.group(1)}': add the package to Package.swift dependencies",
                None,
                "Package.resolved",
            )
        )
    for m in _SOL.finditer(text):
        root = m.group(1).split("/")[0]
        repo_name = FORGE_LIBS.get(root)
        if repo_name:
            add(
                InstallProposal(
                    "forge", repo_name, ("forge", "install", repo_name), m.group(0), None, None
                )
            )
    return list(found.values())


LOCKFILES = (
    "uv.lock",
    "poetry.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "Cargo.lock",
    "go.sum",
    "Package.resolved",
    "Scarb.lock",
    "foundry.lock",
)
