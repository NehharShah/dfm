import json
import tomllib
from collections.abc import Sequence
from pathlib import Path

import pytest

from dfm.config import ConfigError, DFMConfig, load_config, state_dirs
from dfm.config.writer import dumps, set_user_value
from dfm.execution import ExecutionEnvironment
from dfm.models import GenerateRequest, ModelUnavailable
from dfm.models.mlx import LocalModelSpec, MLXModel, discovery, mlx_available
from dfm.models.mlx.discovery import GB, choose, describe_dir, discover, usable
from dfm.models.resources import ModelResourceManager, ResourceSnapshot
from dfm.routing import Route, RouteDecision, RoutingContext

# Real mlx_supports and mlx_available, captured before any test patches them
REAL_MLX_SUPPORTS = discovery.mlx_supports
MLX_INSTALLED = mlx_available()

pytestmark = pytest.mark.usefixtures("mlx_installed")

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               500000.
Pages active:                            1000000.
Pages inactive:                           500000.
Pages speculative:                         48576.
Pages purgeable:                               0.
"""


def fake_model_dir(
    root: Path, name: str, *, model_type: str = "gpt_oss", gb: float = 1.0, bits: int = 4
) -> Path:
    path = root / f"models--{name}" / "snapshots" / "abc123"
    path.mkdir(parents=True)
    config = {
        "model_type": model_type,
        "architectures": ["GptOssForCausalLM"],
        "quantization": {"bits": bits, "group_size": 32, "mode": "mxfp4"},
        "num_hidden_layers": 24,
        "hidden_size": 2880,
        "num_attention_heads": 64,
        "num_key_value_heads": 8,
        "head_dim": 64,
        "max_position_embeddings": 131072,
        "num_local_experts": 32,
        "num_experts_per_tok": 4,
    }
    (path / "config.json").write_text(json.dumps(config))
    (path / "chat_template.jinja").write_text("{{ messages }}")
    with (path / "model.safetensors").open("wb") as fh:
        fh.truncate(int(gb * GB))  # sparse file: size without disk use
    return path


def test_discover_and_describe(tmp_path: Path) -> None:
    hub = tmp_path / "hub"
    fake_model_dir(hub, "org--coder-20b", gb=11)
    fake_model_dir(hub, "unsupported", model_type="nonexistent_arch")
    found = {c.name: c for c in discover([hub])}
    coder = found["org/coder-20b"]
    assert coder.supported and coder.mlx_format and coder.has_chat_template and usable(coder)
    assert coder.moe and (coder.experts, coder.active_experts) == (32, 4)
    assert round(coder.size_gb) == 11
    assert not usable(found["unsupported"])


def test_mlx_supports_reads_the_installed_mlx_lm() -> None:
    assert not REAL_MLX_SUPPORTS("") and not REAL_MLX_SUPPORTS("nonexistent_arch")
    if not MLX_INSTALLED:
        pytest.skip("mlx-lm not installed (Python 3.15, or not Apple Silicon)")
    assert REAL_MLX_SUPPORTS("gpt_oss")


def test_memory_math(tmp_path: Path) -> None:
    c = describe_dir(fake_model_dir(tmp_path, "m", gb=10))
    assert c is not None
    kv = 2 * 24 * 8 * 64 * 2 * 32768 / GB
    assert c.kv_gb(32768) == pytest.approx(kv)
    assert c.memory_gb(32768) == pytest.approx(10 + 2 * kv + 1.5)  # + prefix-cache snapshots


def test_choose_largest_fitting_and_small(tmp_path: Path) -> None:
    hub = tmp_path / "hub"
    for name, gb in (("a-3b", 2), ("b-20b", 11), ("c-70b", 40)):
        fake_model_dir(hub, name, gb=gb)
    candidates = discover([hub])
    primary, small = choose(candidates, memory_budget_gb=20, context=32768)
    assert primary is not None and primary.name == "b-20b"
    assert small is not None and small.name == "a-3b"
    assert choose(candidates, memory_budget_gb=1, context=32768) == (None, None)


def runner(outputs: dict[tuple[str, ...], str]):  # type: ignore[no-untyped-def]
    def run(argv: Sequence[str]) -> str:
        return outputs.get(tuple(argv), "")

    return run


def test_snapshot_parsing() -> None:
    manager = ModelResourceManager(
        runner(
            {
                ("vm_stat",): VM_STAT,
                ("sysctl", "-n", "hw.memsize"): str(48 * GB),
                (
                    "sysctl",
                    "-n",
                    "vm.swapusage",
                ): "total = 1024.00M  used = 512.00M  free = 512.00M  (encrypted)",
            }
        )
    )
    snap = manager.snapshot()
    assert snap.available_gb == pytest.approx((500000 + 500000 + 48576) * 16384 / GB)
    assert snap.swap_used_gb == pytest.approx(0.5)
    assert manager.headroom_gb(snap) == pytest.approx(4.0)  # 8% of 48 GB is below the floor


def test_admission_ladder(tmp_path: Path) -> None:
    big = describe_dir(fake_model_dir(tmp_path / "x", "big", gb=20))
    small = describe_dir(fake_model_dir(tmp_path / "y", "small", gb=3))
    assert big is not None and small is not None
    manager = ModelResourceManager(runner({}))

    def snap(avail_gb: float) -> ResourceSnapshot:
        return ResourceSnapshot(int(48 * GB), int(avail_gb * GB), 0, 0)

    assert manager.admit(big, 32768, snap=snap(40)).decision == "ok"
    reduced = manager.admit(big, 131072, snap=snap(31))
    assert reduced.decision == "reduce_context" and reduced.context < 131072
    assert manager.admit(big, 32768, small=small, snap=snap(15)).decision == "use_small"
    assert manager.admit(big, 32768, snap=snap(10)).decision == "deny"


def test_router_adjuster_downgrades(tmp_path: Path) -> None:
    big = describe_dir(fake_model_dir(tmp_path / "x", "big", gb=30))
    small = describe_dir(fake_model_dir(tmp_path / "y", "small", gb=3))
    manager = ModelResourceManager(
        runner({("vm_stat",): VM_STAT, ("sysctl", "-n", "hw.memsize"): str(48 * GB)})
    )
    adjust = manager.router_adjuster(big, small, 32768)
    decision = RouteDecision(Route.PRIMARY_CODER, ("x",), "deterministic", 0.8, "primary")
    adjusted = adjust(decision, "refactor", RoutingContext())
    assert adjusted.coder == "small" and adjusted.source == "resources"
    resident = manager.router_adjuster(big, small, 32768, primary_loaded=lambda: True)
    assert resident(decision, "refactor", RoutingContext()).coder == "primary"  # already fits
    assert adjusted.route is Route.SMALL_CODER


def test_config_writer(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path)}
    path = set_user_value("network.github", "allow", env)
    set_user_value("models.primary.model", "/models/x", env)
    data = tomllib.loads(path.read_text())
    assert data == {"network": {"github": "allow"}, "models": {"primary": {"model": "/models/x"}}}
    assert load_config(None, env=env).config.models.primary.model == "/models/x"
    assert path.with_suffix(".toml.bak").exists()
    with pytest.raises(ConfigError):
        set_user_value("models.apple.remote", True, env)
    assert dumps({"a": {"b": [1, "x"], "c": True}}) == '[a]\nb = [1, "x"]\nc = true\n'


async def test_runtime_builds_coders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dfm.orchestration.runtime import Runtime

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    weights = fake_model_dir(tmp_path / "hub", "coder", gb=1)
    env = {"HOME": str(home)}
    set_user_value("models.apple.enabled", False, env)
    set_user_value("models.primary.model", str(weights), env)
    set_user_value("models.fallback.model", str(weights), env)
    repo = tmp_path / "repo"
    repo.mkdir()
    rt = Runtime.create(repo, request="x", mode="ask")
    assert isinstance(rt.pool.primary, MLXModel)
    assert rt.pool.small is None  # same weights: one instance, never loaded twice
    assert rt.pool.coder("small").name == "coder-small"
    await rt.close("completed")


async def test_admission_blocks_load_before_worker_starts(tmp_path: Path) -> None:
    weights = fake_model_dir(tmp_path / "hub", "huge", gb=40)
    repo = tmp_path / "repo"
    repo.mkdir()
    environ = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    env = ExecutionEnvironment.create(
        repo, DFMConfig(), session_id="adm", environ=environ, dirs=state_dirs(environ)
    )
    tight = ModelResourceManager(
        runner({("vm_stat",): VM_STAT, ("sysctl", "-n", "hw.memsize"): str(48 * GB)})
    )
    model = MLXModel(
        LocalModelSpec("primary", weights, "huge"), env, resources=tight, sandbox=False
    )
    with pytest.raises(ModelUnavailable, match="not enough memory"):
        await model.generate(GenerateRequest.prompt("x"))
    assert model._proc is None  # pyright: ignore[reportPrivateUsage]


def test_hybrid_linear_attention_models_count_only_kv_layers(tmp_path: Path) -> None:
    """Qwen3.6-27B: 48 linear-attention + 16 full-attention layers; only 16 keep a KV cache."""
    path = tmp_path / "models--qwen" / "snapshots" / "abc"
    path.mkdir(parents=True)
    text = {
        "num_hidden_layers": 64,
        "num_key_value_heads": 4,
        "num_attention_heads": 24,
        "head_dim": 256,
        "hidden_size": 5120,
        "max_position_embeddings": 262144,
        "full_attention_interval": 4,
        "layer_types": [
            "linear_attention",
            "linear_attention",
            "linear_attention",
            "full_attention",
        ]
        * 16,
    }
    config = {
        "model_type": "qwen3_5",
        "architectures": ["Qwen3_5ForConditionalGeneration"],
        "quantization": {"bits": 4, "group_size": 64},
        "text_config": text,
    }
    (path / "config.json").write_text(json.dumps(config))
    with (path / "model.safetensors").open("wb") as fh:
        fh.truncate(int(16 * GB))
    model = describe_dir(path)
    assert model is not None and model.layers == 16
    assert 1.5 < model.kv_gb(32768) < 2.5  # all 64 layers would claim ~8.6 GB
    del text["layer_types"]
    (path / "config.json").write_text(json.dumps(config))
    assert describe_dir(path).layers == 16  # type: ignore[union-attr]  # from full_attention_interval
