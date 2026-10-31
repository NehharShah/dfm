"""Tiny repositories for integration tests. Created in tmp dirs, never in the user's repo."""

import json
from pathlib import Path


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def python_uv(root: Path) -> Path:
    return write(
        root,
        {
            "pyproject.toml": (
                '[project]\nname = "calc"\nversion = "0.1.0"\n'
                '[dependency-groups]\ndev = ["pytest", "ruff"]\n'
                "[tool.ruff]\nline-length = 100\n[tool.pyright]\n"
            ),
            "uv.lock": "version = 1\n",
            "src/calc/__init__.py": "",
            "src/calc/ops.py": "def add(a, b):\n    return a - b\n",
            "tests/test_ops.py": (
                "from calc.ops import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
            ),
        },
    )


def node_pnpm(root: Path) -> Path:
    pkg = {"name": "web", "scripts": {"test": "vitest run", "build": "tsc", "lint": "eslint ."}}
    return write(
        root,
        {
            "package.json": json.dumps(pkg),
            "pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
            "tsconfig.json": "{}",
            "src/index.ts": "export const x = 1;\n",
        },
    )


def pnpm_monorepo(root: Path) -> Path:
    return write(
        root,
        {
            "package.json": json.dumps(
                {"name": "mono", "private": True, "scripts": {"test": "turbo test"}}
            ),
            "pnpm-workspace.yaml": "packages:\n  - packages/*\n",
            "pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
            "packages/web/package.json": json.dumps(
                {"name": "@mono/web", "scripts": {"test": "vitest run"}}
            ),
            "packages/web/src/app.ts": "export const app = 1;\n",
            "packages/api/package.json": json.dumps(
                {"name": "@mono/api", "scripts": {"test": "jest"}}
            ),
            "packages/api/src/server.ts": "export const server = 1;\n",
        },
    )


def rust(root: Path) -> Path:
    return write(
        root,
        {
            "Cargo.toml": '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n',
            "src/lib.rs": "pub fn add(a: i32, b: i32) -> i32 { a + b }\n",
        },
    )


def go(root: Path) -> Path:
    return write(
        root, {"go.mod": "module example.com/demo\n\ngo 1.22\n", "main.go": "package main\n"}
    )


def foundry(root: Path) -> Path:
    return write(
        root, {"foundry.toml": "[profile.default]\nsrc = 'src'\n", "src/A.sol": "contract A {}\n"}
    )


def makefile_only(root: Path) -> Path:
    return write(root, {"Makefile": "test:\n\t./run-tests.sh\nbuild:\n\tcc main.c\n", "main.c": ""})
