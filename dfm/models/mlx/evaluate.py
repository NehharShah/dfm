"""A small, deterministic coding check for a local model.

Three tasks scored by exact checks, not by another model: fix a function, emit a
valid patch action, locate a compiler error. Also records load time, generation
speed and peak memory. It is a smoke test for "can this model drive the DFM loop",
not a benchmark (068 is the benchmark).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from pydantic import BaseModel

from dfm.models.base import GenerateRequest, ModelError
from dfm.models.mlx.model import MLXModel
from dfm.models.structured import structured
from dfm.orchestration.agent import INSTRUCTIONS, AgentAction


class ErrorLocation(BaseModel):
    file: str
    line: int
    fix: str


@dataclass
class EvalResult:
    model: str
    passed: list[str] = field(default_factory=lambda: [])
    failed: list[str] = field(default_factory=lambda: [])
    load_s: float | None = None
    tokens_per_s: float | None = None
    peak_gb: float | None = None
    seconds: float = 0.0
    errors: list[str] = field(default_factory=lambda: [])

    @property
    def score(self) -> float:
        total = len(self.passed) + len(self.failed)
        return len(self.passed) / total if total else 0.0


PATCH_PROMPT = """Task: fix the failing test

Repository context:
<repository-data source="src/calc/ops.py:1-2" trust="untrusted">
    1 def add(a, b):
    2     return a - b
</repository-data>

What happened so far:
Baseline tests: pytest: 0 passed, 1 failed
- tests/test_ops.py::test_add (tests/test_ops.py:5): assert -1 == 5

Choose the next action."""

RUST_ERROR = """error[E0308]: mismatched types
 --> src/lib.rs:7:5
  |
6 | pub fn len(s: &str) -> u32 {
  |                        --- expected `u32` because of return type
7 |     s.len()
  |     ^^^^^^^ expected `u32`, found `usize`"""


async def evaluate(model: MLXModel) -> EvalResult:
    result = EvalResult(model.spec.name or model.name)
    started = time.monotonic()
    try:
        await model.load()
        result.load_s = model.stats.get("load_s")
        fix = await model.generate(
            GenerateRequest.prompt(
                "Fix this Python function so add(2, 3) == 5. Reply with only the code.\n\ndef add(a, b):\n    return a - b",
                timeout_s=300,
            )
        )
        (result.passed if "a + b" in fix.text else result.failed).append("fix_function")
        try:
            action = await structured(
                model,
                GenerateRequest.prompt(
                    PATCH_PROMPT, instructions=INSTRUCTIONS, purpose="code", timeout_s=300
                ),
                AgentAction,
            )
            # A correct patch, or a sensible first step towards it, counts.
            patched = (
                action.action == "patch"
                and (action.path or "").endswith("ops.py")
                and "a + b" in (action.replace or "")
            )
            looked = (
                action.action in {"read", "search"}
                and "ops.py" in f"{action.path} {action.pattern}"
            ) or "def add" in (action.pattern or "")
            ok = patched or looked
            (result.passed if ok else result.failed).append("patch_action")
        except ModelError as exc:
            result.failed.append("patch_action")
            result.errors.append(f"patch_action: {exc}")
        try:
            loc = await structured(
                model,
                GenerateRequest.prompt(
                    f"Where is this compiler error and how do you fix it?\n\n{RUST_ERROR}",
                    timeout_s=300,
                ),
                ErrorLocation,
            )
            ok = loc.file.endswith("src/lib.rs") and loc.line == 7
            (result.passed if ok else result.failed).append("compiler_error")
        except ModelError as exc:
            result.failed.append("compiler_error")
            result.errors.append(f"compiler_error: {exc}")
        result.tokens_per_s = model.stats.get("tps")
        result.peak_gb = model.stats.get("peak_gb")
    except ModelError as exc:
        result.errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        await model.unload()
    result.seconds = round(time.monotonic() - started, 1)
    return result
