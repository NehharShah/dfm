from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

import dfm.integrations.repocanon.provider as provider_mod
from dfm.cli.app import app
from dfm.integrations.repocanon import (
    DegradedCanon,
    RepoCanonProvider,
    commands,
    degraded_discovery,
    is_degraded,
    package_for,
    scoped_commands,
    summary,
)
from tests import repo_fixtures as fx


async def test_real_repocanon_python(tmp_path: Path) -> None:
    repo = fx.python_uv(tmp_path / "py")
    result = await RepoCanonProvider().analyze(repo)
    assert not is_degraded(result)
    assert any("pytest" in c for c in commands(result, "test"))
    assert "Python" in summary(result)["languages"][0]


async def test_real_repocanon_monorepo_package_commands(tmp_path: Path) -> None:
    repo = fx.pnpm_monorepo(tmp_path / "mono")
    result = await RepoCanonProvider().analyze(repo)
    web = package_for(result, "packages/web/src/app.ts")
    assert web is not None and web.path == "packages/web"
    web_tests = scoped_commands(result, "test", "packages/web/src/app.ts")
    api_tests = scoped_commands(result, "test", "packages/api/src/server.ts")
    assert [(c.command, c.cwd) for c in web_tests] == [("pnpm run test", "packages/web")]
    assert [c.cwd for c in api_tests] == ["packages/api"]
    root = scoped_commands(result, "test", "README.md")
    assert [c.cwd for c in root] == ["."]
    assert commands(result, "test", "README.md") == commands(result, "test")


async def test_cached_until_refresh(tmp_path: Path) -> None:
    repo = fx.python_uv(tmp_path / "py")
    provider = RepoCanonProvider()
    first = await provider.analyze(repo)
    assert await provider.analyze(repo) is first
    assert await provider.refresh(repo) is first
    assert await provider.refresh(repo, force=True) is not first


async def test_healthcheck() -> None:
    health = await RepoCanonProvider().healthcheck()
    assert health.available
    assert health.version == "0.2.1" or health.version
    disabled = await RepoCanonProvider(enabled=False).healthcheck()
    assert not disabled.available


async def test_falls_back_when_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provider_mod, "repocanon_available", lambda: False)
    repo = fx.python_uv(tmp_path / "py")
    result = await RepoCanonProvider().analyze(repo)
    assert isinstance(result, DegradedCanon)
    assert commands(result, "test") == ["uv run pytest"]
    assert (await RepoCanonProvider().healthcheck()).available is False


@pytest.mark.parametrize(
    ("make", "kind", "expected"),
    [
        (fx.python_uv, "test", ["uv run pytest"]),
        (fx.python_uv, "lint", ["uv run ruff check ."]),
        (fx.python_uv, "typecheck", ["uv run pyright"]),
        (fx.node_pnpm, "test", ["pnpm test"]),
        (fx.node_pnpm, "build", ["pnpm run build"]),
        (fx.rust, "test", ["cargo test"]),
        (fx.go, "test", ["go test ./..."]),
        (fx.foundry, "test", ["forge test"]),
        (fx.makefile_only, "test", ["make test"]),
    ],
)
def test_degraded_discovery(
    tmp_path: Path, make: Callable[[Path], Path], kind: str, expected: list[str]
) -> None:
    result = degraded_discovery(make(tmp_path / "r"))
    assert result.commands.get(kind) == expected
    assert result.degraded


def test_degraded_empty_repo(tmp_path: Path) -> None:
    result = degraded_discovery(tmp_path)
    assert result.commands == {}
    assert any("No known manifest" in n for n in result.notes)


def test_cli_repo_canon(home: Path, tmp_path: Path) -> None:  # never the real ~/.local/share
    repo = fx.node_pnpm(tmp_path / "web")
    result = CliRunner().invoke(app, ["repo", "canon", "--repo", str(repo), "--json"])
    assert result.exit_code == 0, result.output
    assert '"degraded": false' in result.stdout
