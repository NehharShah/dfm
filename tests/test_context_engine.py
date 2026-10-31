import os
from collections.abc import Callable
from pathlib import Path

import pytest

from dfm.config import DFMConfig, state_dirs
from dfm.context import Collector, ContextEngine, Task, TokenBudget, mentioned_paths
from dfm.execution import ExecutionEnvironment
from dfm.integrations.repocanon import RepoCanonProvider
from dfm.integrations.repokg import RepoKGProvider
from dfm.provenance import ContextItem
from dfm.repository.intelligence import gather
from tests import repo_fixtures as fx


@pytest.fixture
def repo(git_repo: Path, git: Callable[..., str]) -> Path:
    fx.python_uv(git_repo)
    fx.write(
        git_repo,
        {
            "src/calc/big.py": "\n".join(f"x{i} = {i}" for i in range(1000)),
            ".env": "API_KEY=supersecretvalue\nDEBUG=1\n",
            "blob.bin": "",
        },
    )
    (git_repo / "blob.bin").write_bytes(b"\x00\x01binary")
    git(git_repo, "add", ".")
    git(git_repo, "commit", "-qm", "calc")
    return git_repo


async def engine_for(repo: Path, tmp_path: Path) -> ContextEngine:
    environ = {"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"}
    env = ExecutionEnvironment.create(
        repo, DFMConfig(), session_id="c", environ=environ, dirs=state_dirs(environ)
    )
    intel = await gather(repo, RepoCanonProvider(), RepoKGProvider())
    return ContextEngine(env, intel)


def test_mentioned_paths() -> None:
    found = mentioned_paths("fix src/calc/ops.py:2 and tests/test_ops.py, see https://x.io/a.html")
    assert ("src/calc/ops.py", (2, 2)) in found
    assert ("tests/test_ops.py", None) in found
    assert all(not p.startswith("http") for p, _ in found)


async def test_priorities_and_sources(repo: Path, tmp_path: Path) -> None:
    (repo / "src/calc/ops.py").write_text("def add(a, b):\n    return a * b\n")  # uncommitted diff
    engine = await engine_for(repo, tmp_path)
    failure = ContextItem.make(
        "test", "FAILED tests/test_ops.py::test_add", "failing test", priority=2
    )
    bundle = await engine.build(Task("fix src/calc/ops.py", failures=[failure]), TokenBudget(6000))
    sources = [i.source for i in bundle.items]
    assert sources[0] == "repo" and bundle.items[0].path == "src/calc/ops.py"
    assert sources[1] == "test"
    assert "git" in sources and "repokg" in sources and "repocanon" in sources
    priorities = [i.priority for i in bundle.items]
    assert priorities == sorted(priorities)
    assert any(i.path == "tests/test_ops.py" for i in bundle.items)  # related test found
    assert bundle.tokens <= 6000
    assert all("content" not in p for p in bundle.provenance())


async def test_budget_respected(repo: Path, tmp_path: Path) -> None:
    engine = await engine_for(repo, tmp_path)
    bundle = await engine.build(
        Task("look at src/calc/big.py and src/calc/ops.py"), TokenBudget(400)
    )
    assert bundle.tokens <= 400
    assert bundle.dropped


async def test_big_file_read_as_head(repo: Path, tmp_path: Path) -> None:
    engine = await engine_for(repo, tmp_path)
    item, _ = engine.read_item("src/calc/big.py", reason="r", priority=1)
    assert item is not None and item.line_range == (1, 200)
    assert "more lines" in item.content
    ranged, _ = engine.read_item("src/calc/big.py", reason="r", priority=1, lines=(500, 505))
    assert ranged is not None and ranged.line_range == (480, 525)


async def test_secrets_binary_and_boundary(repo: Path, tmp_path: Path) -> None:
    engine = await engine_for(repo, tmp_path)
    bundle = await engine.build(
        Task("check .env and blob.bin and ~/.ssh/id_rsa and ../escape.py"), TokenBudget(4000)
    )
    rendered = bundle.render()
    assert "supersecretvalue" not in rendered
    assert "API_KEY=[REDACTED]" in rendered
    reasons = {d.get("path"): d.get("reason") for d in bundle.dropped}
    assert reasons.get("blob.bin") == "binary (8 bytes): not sent to a model"
    assert "outside boundary" in (reasons.get("../escape.py") or "")


async def test_render_frames_untrusted(repo: Path, tmp_path: Path) -> None:
    engine = await engine_for(repo, tmp_path)
    bundle = await engine.build(Task("explain src/calc/ops.py"), TokenBudget(3000))
    assert bundle.render().count("<repository-data") == len(bundle.items)


async def test_extra_collectors_and_failures_are_isolated(repo: Path, tmp_path: Path) -> None:
    engine = await engine_for(repo, tmp_path)

    async def reflex(task: Task) -> list[ContextItem]:
        return [
            ContextItem.make(
                "reflex", "Run uv run pytest -q before committing", "practice", priority=6
            )
        ]

    async def broken(task: Task) -> list[ContextItem]:
        raise RuntimeError("lsp crashed")

    engine.extra = list[Collector]([reflex, broken])
    bundle = await engine.build(Task("fix src/calc/ops.py"), TokenBudget(5000))
    assert any(i.historical for i in bundle.items)
    assert any("lsp crashed" in str(d.get("reason")) for d in bundle.dropped)


async def test_mentioned_paths_outside_the_repo_list_no_siblings(
    repo: Path, tmp_path: Path
) -> None:
    secret_dir = tmp_path / "dotssh"
    secret_dir.mkdir()
    (secret_dir / "id_rsa_private_name").write_text("x")
    rel = os.path.relpath(secret_dir / "id_rsa", repo)  # ../dotssh/id_rsa: lexically "inside"
    engine = await engine_for(repo, tmp_path)
    bundle = await engine.build(Task(f"look at {rel}"), TokenBudget(6000))
    assert "id_rsa_private_name" not in bundle.render()
