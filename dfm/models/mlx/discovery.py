"""Find and size local MLX models; nothing is hardcoded.

Candidates come from the Hugging Face cache, DFM's own models directory and
LM Studio's directory. Each is described from its config.json (architecture,
quantization, attention layout, MoE experts, context) and checked against the
installed mlx-lm. Memory is estimated as weights (safetensors size) + KV cache
at a target context + runtime overhead.
"""

from __future__ import annotations

import importlib.util
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

GB = 1024**3
OVERHEAD_GB = 1.5
CODING_HINTS = (
    "coder",
    "code",
    "gpt-oss",
    "qwen",
    "deepseek",
    "devstral",
    "codestral",
    "gemma",
    "granite",
    "glm",
)


@dataclass(frozen=True)
class ModelCandidate:
    path: Path
    name: str
    model_type: str
    architectures: tuple[str, ...]
    disk_bytes: int
    quant_bits: int | None
    quant_mode: str | None
    mlx_format: bool  # quantized/converted for MLX (config has "quantization")
    layers: int
    kv_heads: int
    head_dim: int
    max_context: int
    experts: int | None
    active_experts: int | None
    sliding_window: int | None
    multimodal: bool
    has_chat_template: bool
    supported: bool  # installed mlx-lm has a module for this model_type

    @property
    def size_gb(self) -> float:
        return self.disk_bytes / GB

    @property
    def moe(self) -> bool:
        return bool(self.experts)

    def kv_gb(self, context: int, bytes_per_value: int = 2) -> float:
        """Upper bound: every layer keeps full K and V (sliding windows make it smaller)."""
        tokens = context
        return 2 * self.layers * self.kv_heads * self.head_dim * bytes_per_value * tokens / GB

    def memory_gb(self, context: int) -> float:
        # the working KV cache plus the worker's prefix snapshots (together at most one more
        # context's worth, see dfm/models/mlx/worker.py)
        return self.size_gb + 2 * self.kv_gb(context) + OVERHEAD_GB

    def describe(self) -> str:
        quant = (
            f"{self.quant_bits}-bit {self.quant_mode or ''}".strip()
            if self.quant_bits
            else (self.quant_mode or "unquantized")
        )
        moe = f", MoE {self.active_experts}/{self.experts}" if self.experts else ""
        return f"{self.name}: {self.model_type}, {quant}, {self.size_gb:.1f} GB{moe}, ctx {self.max_context}"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return cast(dict[str, Any], json.loads(path.read_text()))
    except (OSError, ValueError):
        return {}


def mlx_supports(model_type: str) -> bool:
    if not model_type:
        return False
    try:
        return importlib.util.find_spec(f"mlx_lm.models.{model_type}") is not None
    except (ImportError, ValueError):
        return False


def _kv_layers(text: dict[str, Any]) -> int:
    """Layers that keep a KV cache. Hybrid models (Qwen3.5/3.6, Qwen3-Next) interleave
    linear-attention layers, whose state does not grow with the context."""
    total = int(text.get("num_hidden_layers") or 0)
    layer_types = text.get("layer_types")
    if isinstance(layer_types, list) and layer_types:
        return sum(
            1
            for t in cast(list[Any], layer_types)
            if str(t) in {"full_attention", "attention", "sliding_attention"}
        )
    interval = text.get("full_attention_interval")
    if isinstance(interval, int) and interval > 1:
        return total // interval
    return total


def describe_dir(path: Path, name: str | None = None) -> ModelCandidate | None:
    config = _read_json(path / "config.json")
    if not config:
        return None
    weights = list(path.glob("*.safetensors"))
    if not weights:
        return None
    text = cast(dict[str, Any], config.get("text_config") or config)
    raw_quant: Any = config.get("quantization") or config.get("quantization_config") or {}
    quant: dict[str, Any] = cast(dict[str, Any], raw_quant) if isinstance(raw_quant, dict) else {}
    bits = quant.get("bits")
    mode = cast(str | None, quant.get("mode") or quant.get("quant_method"))
    model_type = str(config.get("model_type") or text.get("model_type") or "")
    heads = int(text.get("num_attention_heads") or 1)
    hidden = int(text.get("hidden_size") or 0)
    head_dim = int(text.get("head_dim") or (hidden // heads if heads else 0))
    experts = (
        text.get("num_experts") or text.get("num_local_experts") or text.get("n_routed_experts")
    )
    active = text.get("num_experts_per_tok") or text.get("moe_k")
    return ModelCandidate(
        path=path,
        name=name or path.name,
        model_type=model_type,
        architectures=tuple(cast(list[str], config.get("architectures") or [])),
        disk_bytes=sum(w.stat().st_size for w in weights),
        quant_bits=int(bits) if bits else (4 if mode == "mxfp4" else None),
        quant_mode=mode,
        mlx_format="quantization" in config or bool(bits),
        layers=_kv_layers(text),
        kv_heads=int(text.get("num_key_value_heads") or heads),
        head_dim=head_dim,
        max_context=int(text.get("max_position_embeddings") or 0),
        experts=int(experts) if experts else None,
        active_experts=int(active) if active else None,
        sliding_window=cast(int | None, text.get("sliding_window")),
        multimodal="vision_config" in config
        or any(
            "ConditionalGeneration" in a for a in cast(list[str], config.get("architectures") or [])
        ),
        has_chat_template=(path / "chat_template.jinja").exists()
        or "chat_template" in _read_json(path / "tokenizer_config.json"),
        supported=mlx_supports(model_type),
    )


def default_roots(home: Path | None = None, data_dir: Path | None = None) -> list[Path]:
    home = home or Path.home()
    roots = [
        home / ".cache" / "huggingface" / "hub",
        home / ".lmstudio" / "models",
        home / ".cache" / "lm-studio" / "models",
    ]
    if data_dir is not None:
        roots.insert(0, data_dir / "models")
    return roots


def hub_name(path: Path) -> str | None:
    """`org/model` for a Hugging Face cache snapshot (`models--org--model/snapshots/<rev>`)."""
    parts = path.parts
    if len(parts) >= 3 and parts[-2] == "snapshots" and parts[-3].startswith("models--"):
        return re.sub(r"^models--", "", parts[-3]).replace("--", "/")
    return None


def discover(roots: list[Path]) -> list[ModelCandidate]:
    found: dict[Path, ModelCandidate] = {}
    for root in roots:
        if not root.is_dir():
            continue
        if root.name == "hub":  # Hugging Face cache layout: models--org--name/snapshots/<rev>/
            for repo_dir in sorted(root.glob("models--*")):
                name = re.sub(r"^models--", "", repo_dir.name).replace("--", "/")
                for snapshot in sorted((repo_dir / "snapshots").glob("*")):
                    c = describe_dir(
                        snapshot.resolve() if snapshot.is_symlink() else snapshot, name
                    )
                    if c is not None:
                        found[snapshot] = c
                        break
            continue
        for config in sorted(root.rglob("config.json")):
            if len(config.relative_to(root).parts) > 4:
                continue
            c = describe_dir(config.parent, config.parent.relative_to(root).as_posix())
            if c is not None:
                found[config.parent] = c
    return sorted(found.values(), key=lambda c: c.disk_bytes)


def usable(c: ModelCandidate) -> bool:
    return c.supported and c.mlx_format and c.has_chat_template and c.layers > 0


def coding_affinity(c: ModelCandidate) -> int:
    lowered = c.name.lower()
    return sum(1 for hint in CODING_HINTS if hint in lowered)


def choose(
    candidates: list[ModelCandidate], memory_budget_gb: float, context: int
) -> tuple[ModelCandidate | None, ModelCandidate | None]:
    """(primary, small): primary = largest usable model that fits the budget at `context`;
    small = the smallest usable model that fits in a third of the budget (may equal primary
    when only one fits)."""
    fitting = [c for c in candidates if usable(c) and c.memory_gb(context) <= memory_budget_gb]
    if not fitting:
        return None, None
    primary = max(fitting, key=lambda c: (c.disk_bytes, coding_affinity(c)))
    small_pool = [c for c in fitting if c.memory_gb(min(context, 16384)) <= memory_budget_gb / 3]
    small = (
        min(small_pool, key=lambda c: (c.disk_bytes, -coding_affinity(c)))
        if small_pool
        else min(fitting, key=lambda c: c.disk_bytes)
    )
    return primary, small
