"""Full offline integration: local resources only.

`dfm --offline` fixes a failing test end to end (inspect, reason, edit, test, repair,
verify) with Apple FM routing and the local MLX coder, while a monitor records every
socket DFM and its child processes open. The run must make zero connections to anything
but loopback. Run alone with `pytest -m offline_e2e`; for the strictest check, turn
Wi-Fi off first.
"""

import shutil
from pathlib import Path

import pytest

from dfm.config import ConfigOverrides
from dfm.config.writer import set_user_value
from dfm.models.mlx import mlx_available
from dfm.orchestration.implement import ImplementOptions, implement
from dfm.orchestration.runtime import Runtime
from tests.helpers import DONE, FIX, LIVE_MODEL_SKIP, TEST_PATH, ScriptedCoder
from tests.netmon import NetworkMonitor
from tests.test_live_local_coder import WEIGHTS

pytestmark = pytest.mark.offline_e2e
needs_fm = pytest.mark.skipif(shutil.which("fm") is None, reason="needs the `fm` CLI")


@pytest.mark.mlx
@pytest.mark.apple_fm
@needs_fm
@pytest.mark.skipif(not mlx_available() or WEIGHTS is None, reason=LIVE_MODEL_SKIP)
async def test_offline_fix_with_zero_remote_connections(
    tmp_path: Path, calc_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert WEIGHTS is not None
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", TEST_PATH)  # the venv's pytest, whatever started this one
    env = {"HOME": str(home)}
    set_user_value("models.primary.model", str(WEIGHTS), env)
    set_user_value("models.fallback.model", str(WEIGHTS), env)
    with NetworkMonitor() as net:
        rt = Runtime.create(
            calc_repo,
            request="fix the failing test",
            mode="implement",
            overrides=ConfigOverrides(offline=True),
        )
        result = await implement(rt, "fix the failing test", ImplementOptions(max_steps=8))
        await rt.close(result.outcome.session_status)
    if "not enough memory for a local coder" in result.outcome.summary:
        pytest.skip(f"resource manager refused the model here: {result.outcome.summary}")
    assert set(rt.loaded.config.network.model_dump().values()) == {"deny"}
    assert net.remote() == [], net.remote()
    assert net.remote_lookups() == [], net.remote_lookups()
    events = rt.store.load(rt.session.id).events
    assert any(e["type"] == "route_decision" for e in events)
    assert result.outcome.status == "completed", result.outcome.report()
    assert any(e.claim == "tests pass" for e in result.outcome.verified)


@pytest.mark.apple_fm
@needs_fm
async def test_offline_pipeline_with_apple_fm_and_a_scripted_coder(
    tmp_path: Path, calc_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Everything but the MLX worker, monitored: real Apple FM routing, RepoCanon, RepoKG,
    Reflex, sandboxed tests, the session trace. Runs even when memory is too tight for MLX."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", TEST_PATH)  # the venv's pytest, whatever started this one
    with NetworkMonitor() as net:
        rt = Runtime.create(
            calc_repo,
            request="fix the failing test",
            mode="implement",
            overrides=ConfigOverrides(offline=True),
        )
        result = await implement(
            rt,
            "fix the failing test",
            ImplementOptions(coder=ScriptedCoder(FIX, DONE), max_steps=4),
        )
        await rt.close(result.outcome.session_status)
    assert result.outcome.status == "completed", result.outcome.report()
    assert rt.pool.apple is not None  # Apple FM was available and wired
    assert net.remote() == [], net.remote()
    assert net.remote_lookups() == [], net.remote_lookups()


def test_the_monitor_sees_remote_connections() -> None:
    """The negative control: a real outbound attempt is recorded (no traffic leaves: TEST-NET)."""
    import socket

    with NetworkMonitor() as net:
        sock = socket.socket()
        sock.settimeout(0.2)
        try:
            sock.connect(("192.0.2.1", 9))  # RFC 5737 documentation address: unroutable
        except OSError:
            pass
        finally:
            sock.close()
    assert ("python", "192.0.2.1:9") in net.remote()
