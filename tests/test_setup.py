import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dfm.cli.app import app
from dfm.models.mlx.discovery import GB
from dfm.models.mlx.model import mlx_install_hint
from dfm.models.resources import ModelResourceManager
from dfm.orchestration.setup import SetupReport, setup
from tests.test_local_models import VM_STAT, fake_model_dir, runner

pytestmark = pytest.mark.usefixtures("mlx_installed")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


def manager() -> ModelResourceManager:
    return ModelResourceManager(
        runner({("vm_stat",): VM_STAT, ("sysctl", "-n", "hw.memsize"): str(48 * GB)})
    )


async def test_dry_run_recommends_and_writes_nothing(home: Path, tmp_path: Path) -> None:
    hub = tmp_path / "hub"
    fake_model_dir(hub, "big-30b", gb=17)
    fake_model_dir(hub, "small-7b", gb=4)
    asked: list[str] = []
    report = await setup(
        confirm=lambda q: asked.append(q) or True, dry_run=True, resources=manager(), roots=[hub]
    )
    steps = {s.name: s for s in report.steps}
    assert report.primary is not None and report.primary.name == "big-30b"
    assert report.small is not None and report.small.name == "small-7b"
    assert steps["config"].status == "skip" and steps["state dirs"].status == "skip"
    assert steps["offline mode"].status == "ok"
    assert asked == []
    assert not (home / ".config").exists() and not (home / ".local").exists()


async def test_applies_after_confirmation(home: Path, tmp_path: Path) -> None:
    hub = tmp_path / "hub"
    weights = fake_model_dir(hub, "coder-20b", gb=11)
    report = await setup(confirm=lambda q: True, resources=manager(), roots=[hub])
    assert report.ok
    config = tomllib.loads((home / ".config" / "dfm" / "config.toml").read_text())
    assert config["models"]["primary"]["model"] == str(weights)
    assert (home / ".local" / "share" / "dfm" / "sessions").is_dir()
    again = await setup(
        confirm=lambda q: pytest.fail("should not ask twice"), resources=manager(), roots=[hub]
    )
    assert {s.name: s.status for s in again.steps}["config"] == "ok"


async def test_declined_and_no_models(home: Path, tmp_path: Path) -> None:
    hub = tmp_path / "hub"
    fake_model_dir(hub, "coder-20b", gb=11)
    declined = await setup(confirm=lambda q: False, resources=manager(), roots=[hub])
    assert {s.name: s.status for s in declined.steps}["config"] == "skip"
    assert not (home / ".config" / "dfm" / "config.toml").exists()
    empty = await setup(confirm=lambda q: True, resources=manager(), roots=[tmp_path / "nothing"])
    assert not empty.ok
    assert {s.name: s.status for s in empty.steps}["local coder"] == "fail"


async def test_without_mlx_lm_it_names_the_models_it_found(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # without mlx-lm no model type counts as supported, so "found: none" hid a usable download
    monkeypatch.setattr("dfm.orchestration.setup.mlx_available", lambda: False)
    monkeypatch.setattr("dfm.models.mlx.discovery.mlx_supports", lambda model_type: False)
    hub = tmp_path / "hub"
    fake_model_dir(hub, "org--coder-27b", gb=15)
    report = await setup(confirm=lambda q: True, dry_run=True, resources=manager(), roots=[hub])
    steps = {s.name: s for s in report.steps}
    assert steps["MLX"].status == "fail"
    assert steps["local coder"].status == "fail"
    assert "org/coder-27b" in steps["local coder"].detail
    assert "download" not in steps["local coder"].action


def test_mlx_install_hint_matches_why_it_is_missing() -> None:
    assert "upgrade dfm-agent" in mlx_install_hint((3, 14), "arm64")
    on_315 = mlx_install_hint((3, 15), "arm64")
    assert "Python 3.15" in on_315 and "--python 3.14" in on_315
    assert "Apple Silicon" in mlx_install_hint((3, 14), "x86_64")


def test_setup_prints_install_commands_with_their_brackets(monkeypatch: pytest.MonkeyPatch) -> None:
    # Rich read `[mlx]` in `dfm-agent[mlx]` as a style tag and printed `dfm-agent`
    report = SetupReport()
    report.add("MLX", "fail", "mlx-lm [x] not installed", "uv tool install 'dfm-agent[mlx]'")

    async def fake_setup(**_: object) -> SetupReport:
        return report

    monkeypatch.setattr("dfm.cli.setup_cmd.run_setup", fake_setup)
    result = CliRunner().invoke(app, ["setup", "--dry-run"])
    assert result.exit_code == 1
    printed = " ".join(result.stdout.split())  # Rich wraps at the test terminal's width
    assert "uv tool install 'dfm-agent[mlx]'" in printed
    assert "mlx-lm [x] not installed" in printed
