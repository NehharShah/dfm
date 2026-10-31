"""Benchmark tasks: small fixture repositories with an independent check.

Each task has a request for DFM, a `check` command that decides success on its own
(the benchmark never trusts DFM's verdict), the files a correct fix touches, the tools
it needs (missing tools → the task is skipped, not failed), and a reference solution:
the action script a perfect coder would send. The reference solutions let the harness
be tested without a model and prove every fixture is solvable.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Action = dict[str, Any]
DONE: Action = {"action": "done", "reason": "checks pass", "summary": "fixed"}


def patch(path: str, search: str, replace: str) -> Action:
    return {"action": "patch", "reason": "fix", "path": path, "search": search, "replace": replace}


def create(path: str, content: str) -> Action:
    return {"action": "create", "reason": "add", "path": path, "content": content}


@dataclass(frozen=True)
class BenchTask:
    id: str
    title: str
    language: str
    files: Mapping[str, str]
    request: str
    check: tuple[str, ...]
    expected: frozenset[str]
    reference: tuple[Action, ...]
    needs: tuple[str, ...] = ()
    kind: Literal["implement", "ci"] = "implement"
    setup: Callable[[Path], None] | None = None
    ci_log: str | None = None  # `kind == "ci"`: the failed-run log (gh --log-failed format)
    notes: list[str] = field(default_factory=lambda: [])


PYPROJECT = '[project]\nname = "bench"\nversion = "0"\n[tool.pytest.ini_options]\npythonpath = ["src", "."]\n'
PYTEST = ("python", "-m", "pytest", "-q", "-p", "no:cacheprovider")


def _gh_log(job: str, script: str, output: list[str]) -> str:
    stamp = "2026-10-26T10:00:00.0000000Z"
    lines = [
        f"{job}\tUNKNOWN STEP\t{stamp} ##[group]Run {script}",
        f"{job}\tUNKNOWN STEP\t{stamp} ^[[36;1m{script}^[[0m",
        f"{job}\tUNKNOWN STEP\t{stamp} ##[endgroup]",
        *(f"{job}\tUNKNOWN STEP\t{stamp} {line}" for line in output),
        f"{job}\tUNKNOWN STEP\t{stamp} ##[error]Process completed with exit code 1.",
    ]
    return "\n".join(lines) + "\n"


def _big_log(repo: Path) -> None:
    """~2 MB of service log with a recurring error: too large to read whole (054/055)."""
    lines: list[str] = []
    for i in range(30_000):
        lines.append(
            f"2026-10-26T10:{(i // 600) % 60:02d}:{(i // 10) % 60:02d}Z INFO batch {i} processed in {i % 97}ms"
        )
        if i % 1500 == 7:
            lines.append(
                f"2026-10-26T10:{(i // 600) % 60:02d}:00Z ERROR average() failed for batch {i}: ZeroDivisionError: division by zero (empty batch)"
            )
    (repo / "logs").mkdir(exist_ok=True)
    (repo / "logs" / "app.log").write_text("\n".join(lines) + "\n")


TASKS: tuple[BenchTask, ...] = (
    BenchTask(
        "python-bug",
        "Python bug",
        "python",
        {
            "pyproject.toml": PYPROJECT,
            "src/calc/__init__.py": "",
            "src/calc/ops.py": "def add(a, b):\n    return a - b\n",
            "tests/test_ops.py": "from calc.ops import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        },
        "fix the failing test",
        PYTEST,
        frozenset({"src/calc/ops.py"}),
        (patch("src/calc/ops.py", "    return a - b\n", "    return a + b\n"), DONE),
    ),
    BenchTask(
        "ts-feature",
        "TypeScript feature",
        "typescript",
        {
            "package.json": json.dumps(
                {
                    "name": "calc",
                    "private": True,
                    "type": "module",
                    "scripts": {"test": "node --test"},
                },
                indent=2,
            ),
            "src/calc.ts": "export function add(a: number, b: number): number {\n  return a + b;\n}\n",
            "src/calc.test.ts": (
                'import assert from "node:assert/strict";\nimport { test } from "node:test";\n'
                'import { add, mul } from "./calc.ts";\n\n'
                'test("add", () => assert.equal(add(2, 3), 5));\ntest("mul", () => assert.equal(mul(2, 3), 6));\n'
            ),
        },
        "Add a `mul(a, b)` function to src/calc.ts so the tests pass.",
        ("node", "--test"),
        frozenset({"src/calc.ts"}),
        (
            patch(
                "src/calc.ts",
                "  return a + b;\n}\n",
                "  return a + b;\n}\n\nexport function mul(a: number, b: number): number {\n  return a * b;\n}\n",
            ),
            DONE,
        ),
        needs=("node",),
    ),
    BenchTask(
        "rust-compiler",
        "Rust compiler error",
        "rust",
        {
            "Cargo.toml": '[package]\nname = "parse"\nversion = "0.1.0"\nedition = "2021"\n',
            "src/lib.rs": (
                'pub fn parse(s: &str) -> i32 {\n    s.trim().parse().unwrap_or("0")\n}\n\n'
                '#[cfg(test)]\nmod tests {\n    use super::*;\n\n    #[test]\n    fn parses() {\n        assert_eq!(parse(" 4 "), 4);\n        assert_eq!(parse("x"), 0);\n    }\n}\n'
            ),
        },
        "The crate does not compile; fix it so the tests pass.",
        ("cargo", "test", "--quiet"),
        frozenset({"src/lib.rs"}),
        (
            patch(
                "src/lib.rs",
                '    s.trim().parse().unwrap_or("0")\n',
                "    s.trim().parse().unwrap_or(0)\n",
            ),
            DONE,
        ),
        needs=("cargo",),
    ),
    BenchTask(
        "go-refactor",
        "Go refactor",
        "go",
        {
            "go.mod": "module example.com/calc\n\ngo 1.22\n",
            "calc/calc.go": "package calc\n\n// Add returns a + b.\nfunc Add(a, b int) int {\n\treturn a + b\n}\n",
            "calc/report.go": 'package calc\n\nimport "fmt"\n\n// Report prints a sum.\nfunc Report(a, b int) string {\n\treturn fmt.Sprintf("%d", Add(a, b))\n}\n',
            "calc/calc_test.go": (
                'package calc\n\nimport "testing"\n\nfunc TestSum(t *testing.T) {\n\tif Sum(2, 3) != 5 {\n\t\tt.Fatal("sum")\n\t}\n'
                '\tif Report(2, 3) != "5" {\n\t\tt.Fatal("report")\n\t}\n}\n'
            ),
        },
        "Rename the function Add to Sum everywhere (keep behaviour).",
        ("go", "test", "./..."),
        frozenset({"calc/calc.go", "calc/report.go"}),
        (
            patch(
                "calc/calc.go",
                "// Add returns a + b.\nfunc Add(a, b int) int {\n",
                "// Sum returns a + b.\nfunc Sum(a, b int) int {\n",
            ),
            patch(
                "calc/report.go",
                '\treturn fmt.Sprintf("%d", Add(a, b))\n',
                '\treturn fmt.Sprintf("%d", Sum(a, b))\n',
            ),
            DONE,
        ),
        needs=("go",),
    ),
    BenchTask(
        "monorepo-edit",
        "Monorepo edit",
        "typescript",
        {
            "package.json": json.dumps(
                {
                    "name": "shop",
                    "private": True,
                    "type": "module",
                    "workspaces": ["packages/*"],
                    "scripts": {"test": "node --test"},
                },
                indent=2,
            ),
            "packages/api/package.json": json.dumps({"name": "@shop/api", "type": "module"}),
            "packages/api/src/price.ts": "export function withTax(cents: number): number {\n  return cents + cents * 20;\n}\n",
            "packages/api/src/price.test.ts": (
                'import assert from "node:assert/strict";\nimport { test } from "node:test";\nimport { withTax } from "./price.ts";\n\n'
                'test("20% tax", () => assert.equal(withTax(100), 120));\n'
            ),
            "packages/web/package.json": json.dumps({"name": "@shop/web", "type": "module"}),
            "packages/web/src/format.ts": "export const euros = (cents: number): string => `${(cents / 100).toFixed(2)} EUR`;\n",
        },
        "withTax in the api package adds far too much tax; it should add 20%.",
        ("node", "--test"),
        frozenset({"packages/api/src/price.ts"}),
        (
            patch(
                "packages/api/src/price.ts",
                "  return cents + cents * 20;\n",
                "  return cents + cents * 0.2;\n",
            ),
            DONE,
        ),
        needs=("node",),
    ),
    BenchTask(
        "ci-failure",
        "CI failure",
        "python",
        {
            "pyproject.toml": PYPROJECT,
            ".github/workflows/ci.yml": (
                "name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
                "      - uses: actions/checkout@v4\n      - name: Run tests\n        run: python -m pytest -q -p no:cacheprovider\n"
            ),
            "src/slug.py": 'def slugify(text):\n    return text.lower().replace(" ", "_")\n',
            "tests/test_slug.py": 'from slug import slugify\n\n\ndef test_slugify():\n    assert slugify("Hello World") == "hello-world"\n',
        },
        "fix CI",
        PYTEST,
        frozenset({"src/slug.py"}),
        (
            patch(
                "src/slug.py",
                '    return text.lower().replace(" ", "_")\n',
                '    return text.lower().replace(" ", "-")\n',
            ),
            DONE,
        ),
        kind="ci",
        ci_log=_gh_log(
            "test",
            "python -m pytest -q -p no:cacheprovider",
            [
                "F",
                "FAILED tests/test_slug.py::test_slugify - AssertionError: assert 'hello_world' == 'hello-world'",
                "1 failed in 0.02s",
            ],
        ),
    ),
    BenchTask(
        "security-regression",
        "Security regression",
        "python",
        {
            "pyproject.toml": PYPROJECT,
            "src/users.py": (
                "import sqlite3\n\n\ndef find_user(conn: sqlite3.Connection, name: str):\n"
                "    return conn.execute(f\"SELECT id, name FROM users WHERE name = '{name}'\").fetchone()\n"
            ),
            "tests/test_users.py": (
                "import sqlite3\n\nfrom users import find_user\n\n\ndef db():\n    conn = sqlite3.connect(':memory:')\n"
                "    conn.execute('create table users (id integer, name text)')\n    conn.execute(\"insert into users values (1, 'alice')\")\n    return conn\n\n\n"
                "def test_finds_alice():\n    assert find_user(db(), 'alice') == (1, 'alice')\n\n\n"
                "def test_injection_finds_nobody():\n    assert find_user(db(), \"x' OR '1'='1\") is None\n"
            ),
        },
        "find_user is vulnerable to SQL injection; fix it.",
        PYTEST,
        frozenset({"src/users.py"}),
        (
            patch(
                "src/users.py",
                "    return conn.execute(f\"SELECT id, name FROM users WHERE name = '{name}'\").fetchone()\n",
                '    return conn.execute("SELECT id, name FROM users WHERE name = ?", (name,)).fetchone()\n',
            ),
            DONE,
        ),
    ),
    BenchTask(
        "large-log-debugging",
        "Large-log debugging",
        "python",
        {
            "pyproject.toml": PYPROJECT,
            "src/stats.py": "def average(items):\n    return sum(items) / len(items)\n",
            "tests/test_stats.py": "from stats import average\n\n\ndef test_average():\n    assert average([2, 4]) == 3\n\n\ndef test_empty_batch():\n    assert average([]) == 0.0\n",
        },
        "The service keeps logging errors (logs/app.log is large). Find the cause and fix it.",
        PYTEST,
        frozenset({"src/stats.py"}),
        (
            patch(
                "src/stats.py",
                "    return sum(items) / len(items)\n",
                "    if not items:\n        return 0.0\n    return sum(items) / len(items)\n",
            ),
            DONE,
        ),
        setup=_big_log,
    ),
    BenchTask(
        "solidity-failure",
        "Solidity test failure",
        "solidity",
        {
            "foundry.toml": "[profile.default]\nsrc = 'src'\ntest = 'test'\n",
            "src/Counter.sol": "// SPDX-License-Identifier: MIT\npragma solidity ^0.8.20;\n\ncontract Counter {\n    uint256 public count;\n\n    function increment() external {\n        count += 2;\n    }\n}\n",
            "test/Counter.t.sol": (
                '// SPDX-License-Identifier: MIT\npragma solidity ^0.8.20;\n\nimport {Counter} from "../src/Counter.sol";\n\n'
                "contract CounterTest {\n    function test_increment() public {\n        Counter c = new Counter();\n        c.increment();\n"
                '        require(c.count() == 1, "count");\n    }\n}\n'
            ),
        },
        "fix the failing test",
        ("forge", "test"),
        frozenset({"src/Counter.sol"}),
        (patch("src/Counter.sol", "        count += 2;\n", "        count += 1;\n"), DONE),
        needs=("forge", "solc"),
    ),
    BenchTask(
        "review-comment-repair",
        "Review-comment repair",
        "python",
        {
            "pyproject.toml": PYPROJECT,
            "src/mathops.py": "def mul(a, b):\n    return a + b\n\n\ndef div(a, b):\n    return a / b if b else 0\n",
            "REVIEW.md": (
                "# Review\n\n1. src/mathops.py: `mul` adds instead of multiplying.\n"
                "2. src/mathops.py: `div` silently returns 0 for b == 0; it must raise ZeroDivisionError.\n"
            ),
            "tests/test_mathops.py": (
                "import pytest\n\nfrom mathops import div, mul\n\n\ndef test_mul():\n    assert mul(3, 4) == 12\n\n\n"
                "def test_div_by_zero_raises():\n    with pytest.raises(ZeroDivisionError):\n        div(1, 0)\n"
            ),
        },
        "Address the review comments in REVIEW.md.",
        PYTEST,
        frozenset({"src/mathops.py"}),
        (
            patch("src/mathops.py", "    return a + b\n", "    return a * b\n"),
            patch("src/mathops.py", "    return a / b if b else 0\n", "    return a / b\n"),
            DONE,
        ),
    ),
    BenchTask(
        "architecture-refactor",
        "Architecture refactor",
        "python",
        {
            "pyproject.toml": PYPROJECT,
            "app/__init__.py": "",
            "app/views.py": (
                "import re\n\n\ndef slugify(text):\n    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')\n\n\n"
                "def article_url(title):\n    return '/a/' + slugify(title)\n"
            ),
            "tests/test_layout.py": (
                "import app.text\nimport app.views\n\n\ndef test_slugify_lives_in_text():\n    assert app.text.slugify('Hello, World') == 'hello-world'\n"
                "    assert 'def slugify' not in open(app.views.__file__).read()\n\n\n"
                "def test_views_still_work():\n    assert app.views.article_url('Hi There') == '/a/hi-there'\n"
            ),
        },
        "Move slugify out of app/views.py into a new module app/text.py; views should import it from there.",
        PYTEST,
        frozenset({"app/text.py", "app/views.py"}),
        (
            create(
                "app/text.py",
                "import re\n\n\ndef slugify(text):\n    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')\n",
            ),
            patch(
                "app/views.py",
                "import re\n\n\ndef slugify(text):\n    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')\n",
                "from app.text import slugify\n",
            ),
            DONE,
        ),
    ),
)

BY_ID = {t.id: t for t in TASKS}
