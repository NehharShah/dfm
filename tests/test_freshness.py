import json
from collections.abc import Callable
from pathlib import Path

import pytest

from dfm.integrations.repocanon import DegradedCanon, RepoCanonProvider
from dfm.repository.freshness import (
    FreshnessState,
    SnapshotCache,
    layout,
    snapshot,
    structural_fingerprint,
)
from tests import repo_fixtures as fx


def fp(repo: Path) -> str:
    return structural_fingerprint(repo, ["test"])


@pytest.mark.parametrize(
    ("change", "stale"),
    [
        (lambda r: (r / "src/calc/ops.py").write_text("def add(a, b):\n    return a + b\n"), False),
        (lambda r: (r / "tests/test_more.py").write_text("def test_x(): pass\n"), False),
        (lambda r: (r / "README.md").write_text("# hi\n"), False),
        (lambda r: (r / "pyproject.toml").write_text('[project]\nname = "calc2"\n'), True),
        (lambda r: (r / "uv.lock").write_text("version = 2\n"), True),
        (lambda r: fx.write(r, {".github/workflows/ci.yml": "on: push\n"}), True),
        (lambda r: fx.write(r, {"packages/new/package.json": "{}"}), True),
        (lambda r: (r / "docs").mkdir(), True),
        (lambda r: fx.write(r, {".repocanon/config.toml": "[scan]\n"}), True),
        (lambda r: fx.write(r, {"node_modules/x/package.json": "{}"}), False),
        (lambda r: fx.write(r, {".venv/lib/pyproject.toml": "x"}), False),
    ],
)
def test_fingerprint_invalidation_matrix(
    tmp_path: Path, change: Callable[[Path], object], stale: bool
) -> None:
    repo = fx.python_uv(tmp_path / "r")
    before = fp(repo)
    change(repo)
    assert (fp(repo) != before) is stale


def test_salt_changes_fingerprint(tmp_path: Path) -> None:
    repo = fx.rust(tmp_path / "r")
    assert structural_fingerprint(repo, ["a"]) != structural_fingerprint(repo, ["b"])


def test_layout_ignores_build_dirs(tmp_path: Path) -> None:
    repo = fx.write(tmp_path / "r", {"src/a.py": "", "target/x": "", "pkg/sub/f": ""})
    assert layout(repo) == ["pkg/", "pkg/sub/", "src/"]


def test_snapshot_cache_detects_tampering(tmp_path: Path) -> None:
    cache = SnapshotCache(tmp_path / "cache", "repocanon", "abc")
    cache.save(snapshot("repocanon", "1", tmp_path, "fp"), '{"x": 1}')
    loaded = cache.load()
    assert loaded is not None and loaded[1] == '{"x": 1}'
    (cache.dir / "payload.json").write_text('{"x": 2}')
    assert cache.load() is None


async def test_provider_disk_cache_and_states(tmp_path: Path) -> None:
    repo = fx.python_uv(tmp_path / "r")
    cache_root = tmp_path / "cache"
    provider = RepoCanonProvider(cache_root=cache_root)
    assert provider.state(repo) is FreshnessState.MISSING
    first = await provider.analyze(repo)
    assert provider.state(repo) is FreshnessState.FRESH

    # A new provider (new DFM process) loads the disk cache instead of re-analyzing.
    second_provider = RepoCanonProvider(cache_root=cache_root)
    assert second_provider.state(repo) is FreshnessState.FRESH
    second = await second_provider.analyze(repo)
    assert not isinstance(first, DegradedCanon) and not isinstance(second, DegradedCanon)
    assert second.model_dump() == first.model_dump()

    (repo / "src/calc/ops.py").write_text("changed = True\n")
    assert provider.state(repo) is FreshnessState.FRESH
    (repo / "package.json").write_text(json.dumps({"name": "x", "scripts": {"test": "vitest"}}))
    assert provider.state(repo) is FreshnessState.STALE
    refreshed = await provider.analyze(repo)
    assert provider.state(repo) is FreshnessState.FRESH
    assert refreshed is not first


async def test_cache_lives_outside_repo(tmp_path: Path) -> None:
    repo = fx.python_uv(tmp_path / "r")
    await RepoCanonProvider(cache_root=tmp_path / "cache").analyze(repo)
    assert not (repo / ".dfm").exists()
    assert not (repo / ".repocanon").exists()


def test_degraded_state(tmp_path: Path) -> None:
    assert RepoCanonProvider(enabled=False).state(tmp_path) is FreshnessState.DEGRADED
