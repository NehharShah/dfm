from pathlib import Path

import pytest

import dfm.models.mlx.model as mlx_model
from dfm.config import DFMConfig, state_dirs
from dfm.execution import ExecutionEnvironment
from dfm.models.mlx import LocalModelSpec, MLXModel
from dfm.models.thermal import LEVELS, ThermalMonitor, limit_level, pause_for


def test_limits_and_pacing() -> None:
    assert limit_level("moderate") == 1 and limit_level("heavy") == 2
    assert limit_level("off") is None and limit_level(None) is None
    assert pause_for(0, 1, 2.0) == 0.0  # cool: full speed
    assert pause_for(1, 1, 2.0) == 2.0  # at the limit: rest as long as it worked (half duty)
    assert pause_for(2, 1, 2.0) == 4.0  # hotter: a third
    assert pause_for(3, None, 2.0) == 0.0  # pacing off


def test_monitor_reads_a_level() -> None:
    assert 0 <= ThermalMonitor().level() < len(LEVELS)


def test_default_is_to_slow_down_at_moderate_pressure() -> None:
    assert DFMConfig().models.thermal_limit == "moderate"


async def test_waits_for_the_device_to_cool_before_a_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environ = {"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"}
    env = ExecutionEnvironment.create(
        tmp_path, DFMConfig(), session_id="t", environ=environ, dirs=state_dirs(environ)
    )
    monkeypatch.setattr(mlx_model, "THERMAL_POLL_S", 0.01)
    readings = iter([2, 2, 1])  # heavy, heavy, then back to the moderate limit
    model = MLXModel(LocalModelSpec("small", tmp_path, thermal_limit=1), env, sandbox=False)
    monkeypatch.setattr(model.thermal, "level", lambda: next(readings))
    assert await model._cool_down() == pytest.approx(0.02)  # pyright: ignore[reportPrivateUsage]
    off = MLXModel(LocalModelSpec("small", tmp_path, thermal_limit=None), env, sandbox=False)
    monkeypatch.setattr(off.thermal, "level", lambda: 4)
    assert await off._cool_down() == 0.0  # pyright: ignore[reportPrivateUsage]
