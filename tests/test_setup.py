import tomllib
from pathlib import Path

import pytest

from dfm.models.mlx.discovery import GB
from dfm.models.resources import ModelResourceManager
from dfm.orchestration.setup import setup
from tests.test_local_models import VM_STAT, fake_model_dir, runner


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
