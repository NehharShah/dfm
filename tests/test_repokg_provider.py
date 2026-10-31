import io
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import dfm.integrations.repokg.provider as provider_mod
from dfm.cli.app import app
from dfm.integrations.repokg import (
    RepoKGProvider,
    blast_radius,
    dependencies,
    graph_summary,
    module_for,
    neighbors,
    reverse_dependencies,
)
from dfm.repository.freshness import FreshnessState
from tests import repo_fixtures as fx

LAYERED = {
    "core/__init__.py": "",
    "core/model.py": "VALUE = 1\n",
    "api/__init__.py": "",
    "api/routes.py": "from core import model\n",
    "web/__init__.py": "",
    "web/app.py": "from api import routes\n",
    "tools/__init__.py": "",
    "tools/cli.py": "import os\n",
    ".github/workflows/ci.yml": "name: CI\non: push\n",
}


@pytest.fixture
def layered(git_repo: Path, git: Callable[..., str]) -> Path:
    fx.write(git_repo, LAYERED)
    git(git_repo, "add", ".")
    git(git_repo, "commit", "-qm", "layers")
    return git_repo


async def test_graph_and_navigation(layered: Path) -> None:
    kg = await RepoKGProvider().graph(layered)
    assert kg is not None
    assert module_for(kg, "api/routes.py") == "api"
    assert dependencies(kg, "api") == ["core"]
    assert reverse_dependencies(kg, "core") == ["api"]
    assert neighbors(kg, "core", depth=2) == ["api", "web"]
    radius = blast_radius(kg, ["core/model.py", "nowhere.txt"])
    assert radius.changed == ["core"]
    assert radius.direct_dependents == ["api"]
    assert radius.transitive_dependents == ["web"]
    summary = graph_summary(kg)
    assert any(e.startswith("api -> core") for e in summary["edges"])
    assert summary["ops"].get("workflows") == 1


async def test_silent_and_no_network(layered: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    providers = provider_mod._module("repokg.providers")  # pyright: ignore[reportPrivateUsage]
    calls: list[object] = []
    monkeypatch.setattr(providers, "collect", lambda *a, **k: calls.append(a) or ([], ""))
    captured = io.StringIO()
    with redirect_stdout(captured):
        kg = await RepoKGProvider().graph(layered)
    assert kg is not None
    assert captured.getvalue() == ""
    assert calls == []


async def test_nothing_written_into_repo(
    layered: Path, git: Callable[..., str], tmp_path: Path
) -> None:
    await RepoKGProvider(cache_root=tmp_path / "cache").graph(layered)
    assert git(layered, "status", "--porcelain", "--untracked-files=all") == ""
    assert (tmp_path / "cache" / "repokg").exists()


async def test_freshness_and_incremental_cache(layered: Path, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    provider = RepoKGProvider(cache_root=cache)
    assert provider.state(layered) is FreshnessState.MISSING
    first = await provider.graph(layered)
    assert provider.state(layered) is FreshnessState.FRESH
    assert await provider.graph(layered) is first
    other_process = RepoKGProvider(cache_root=cache)
    assert other_process.state(layered) is FreshnessState.FRESH
    (layered / "web" / "app.py").write_text("from core import model\n")
    assert provider.state(layered) is FreshnessState.STALE
    updated = await provider.graph(layered)
    assert updated is not None
    assert dependencies(updated, "web") == ["core"]
    assert any((cache / "repokg").rglob("cache.json"))  # RepoKG's own incremental scan cache


async def test_mark_stale(layered: Path) -> None:
    provider = RepoKGProvider()
    first = await provider.graph(layered)
    provider.mark_stale(layered)
    assert await provider.graph(layered) is not first


async def test_not_a_git_repo_degrades(tmp_path: Path) -> None:
    provider = RepoKGProvider()
    assert await provider.graph(fx.python_uv(tmp_path / "r")) is None
    assert "Git" in (provider.last_error or "")
    assert provider.state(tmp_path / "r") is FreshnessState.DEGRADED


async def test_missing_repokg_degrades(layered: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_mod, "repokg_available", lambda: False)
    provider = RepoKGProvider()
    assert await provider.graph(layered) is None
    assert not (await provider.healthcheck()).available


async def test_healthcheck_reports_api_path() -> None:
    health = await RepoKGProvider().healthcheck()
    assert health.available
    expected = "public api" if provider_mod.has_public_api() else "patch not applied"
    assert expected in health.detail


async def test_prefers_public_api_when_present(
    layered: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_build_graph(repo: str, **kwargs: object) -> dict[str, object]:
        seen.update(repo=repo, **kwargs)
        return {"repokg_version": 1, "modules": [], "edges": []}

    real_module = provider_mod._module  # pyright: ignore[reportPrivateUsage]

    def fake_module(name: str) -> object:
        if name == "repokg.api":
            return SimpleNamespace(build_graph=fake_build_graph)
        return real_module(name)

    monkeypatch.setattr(provider_mod, "_module", fake_module)
    kg = await RepoKGProvider(cache_root=tmp_path / "c", github=False).graph(layered)
    assert kg == {"repokg_version": 1, "modules": [], "edges": []}
    assert seen["github"] is False and seen["pr_limit"] == 0
    assert str(seen["out"]).startswith(str(tmp_path / "c"))


def test_cli_repo_graph(layered: Path, tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path / "home")}
    runner = CliRunner()
    whole = runner.invoke(app, ["repo", "graph", "--repo", str(layered), "--json"], env=env)
    assert whole.exit_code == 0, whole.output
    assert '"api -> core' in whole.stdout
    focused = runner.invoke(
        app, ["repo", "graph", "core/model.py", "--repo", str(layered), "--json"], env=env
    )
    assert '"dependents"' in focused.stdout and '"api"' in focused.stdout
    missing = runner.invoke(app, ["repo", "graph", "--repo", str(tmp_path)], env=env)
    assert missing.exit_code == 1
