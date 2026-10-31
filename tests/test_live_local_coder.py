"""First usable milestone: a real local model fixes a real failing test.

Opt-in: set DFM_TEST_MODEL to a local MLX model (see tests/helpers.py). No hosted model is
involved.
"""

from pathlib import Path

import pytest

from dfm.config.writer import set_user_value
from dfm.models.mlx import mlx_available
from dfm.orchestration.implement import ImplementOptions, implement
from dfm.orchestration.runtime import Runtime
from tests.helpers import LIVE_MODEL_SKIP, live_model

WEIGHTS = live_model()


@pytest.mark.mlx
@pytest.mark.skipif(not mlx_available() or WEIGHTS is None, reason=LIVE_MODEL_SKIP)
async def test_local_coder_fixes_failing_test(
    tmp_path: Path, calc_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert WEIGHTS is not None
    real_home = Path.home()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    env = {"HOME": str(home)}
    set_user_value("models.apple.enabled", False, env)
    set_user_value("models.primary.model", str(WEIGHTS), env)
    set_user_value("models.fallback.model", str(WEIGHTS), env)
    assert WEIGHTS.is_relative_to(real_home)
    rt = Runtime.create(calc_repo, request="fix the failing test", mode="implement")
    result = await implement(rt, "fix the failing test", ImplementOptions(max_steps=8))
    await rt.close(result.outcome.session_status)
    if "not enough memory for a local coder" in result.outcome.summary:
        pytest.skip(f"resource manager refused the model here: {result.outcome.summary}")
    assert result.outcome.status == "completed", result.outcome.report()
    assert "return a + b" in (calc_repo / "src/calc/ops.py").read_text()
    assert any(e.claim == "tests pass" and e.exit_code == 0 for e in result.outcome.verified)
    models = {
        e.get("model") for e in rt.store.load(rt.session.id).events if e["type"] == "model_finished"
    }
    assert models <= {"coder-small", "coder-primary"}  # local coders only
