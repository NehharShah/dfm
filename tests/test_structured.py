import json
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from dfm.config import DFMConfig, state_dirs
from dfm.execution import ExecutionEnvironment
from dfm.models import GenerateRequest, GenerateResult, ModelHealth, StructuredOutputError
from dfm.models.apple import AppleFMModel
from dfm.models.schema import fm_schema
from dfm.models.structured import first_json_object, structured
from dfm.models.tasks import CommitMessage, CompressedLog, RouteSuggestion, TaskClassification
from dfm.processes import ProcessManager, ProcessRegistry
from dfm.routing import Route

GOLDEN = Path(__file__).parent / "fixtures" / "fm_schema"


class A(BaseModel):
    s: str = Field(description="a string")
    i: int
    d: float
    b: bool
    tags: list[str]
    note: str | None = None


class Inner(BaseModel):
    n: int


class Outer(BaseModel):
    title: str
    inner: Inner


def test_matches_fm_golden_scalars() -> None:
    assert fm_schema(A) == json.loads((GOLDEN / "scalars_array_optional.json").read_text())


def test_matches_fm_golden_nested() -> None:
    assert fm_schema(Outer) == json.loads((GOLDEN / "nested_object.json").read_text())


def test_enum_and_literal() -> None:
    schema = fm_schema(RouteSuggestion)
    assert schema["properties"]["route"] == {
        "type": "string",
        "enum": [r.value for r in Route],
        "description": "Which worker should handle the request",
    }


@pytest.mark.parametrize(
    "model", [TaskClassification, RouteSuggestion, CompressedLog, CommitMessage]
)
def test_task_schemas_build(model: type[BaseModel]) -> None:
    schema = fm_schema(model)
    assert schema["additionalProperties"] is False
    assert schema["x-order"] == list(schema["properties"])


def test_unsupported_type() -> None:
    class Bad(BaseModel):
        x: dict[str, int]

    with pytest.raises(TypeError):
        fm_schema(Bad)


class ScriptedModel:
    name = "scripted"

    def __init__(self, *outputs: str) -> None:
        self.outputs = list(outputs)
        self.requests: list[GenerateRequest] = []

    async def healthcheck(self) -> ModelHealth:
        return ModelHealth(self.name, True, "ok")

    async def generate(self, request: GenerateRequest) -> GenerateResult:
        self.requests.append(request)
        return GenerateResult(text=self.outputs.pop(0), model=self.name, latency_ms=1)

    async def stream(self, request: GenerateRequest) -> AsyncIterator[str]:
        yield ""


async def test_structured_success_sets_schema() -> None:
    model = ScriptedModel('{"route": "TOOLS_ONLY", "reason": "git", "confidence": 0.9}')
    out = await structured(model, GenerateRequest.prompt("git status"), RouteSuggestion)
    assert out.route is Route.TOOLS_ONLY
    assert model.requests[0].schema == fm_schema(RouteSuggestion)


async def test_structured_tolerates_fenced_json() -> None:
    model = ScriptedModel('```json\n{"subject": "Fix add", "body": null}\n```')
    out = await structured(model, GenerateRequest.prompt("x"), CommitMessage)
    assert out.subject == "Fix add"


async def test_structured_takes_the_first_of_two_objects() -> None:
    # seen live from gpt-oss-20b: an action, then a second one ("Extra data" before)
    model = ScriptedModel(
        'Sure: {"subject": "Fix add", "body": null} {"subject": "again", "body": null}'
    )
    out = await structured(model, GenerateRequest.prompt("x"), CommitMessage)
    assert out.subject == "Fix add" and len(model.requests) == 1
    assert first_json_object('a {not json} then {"a": {"b": 1}}') == {"a": {"b": 1}}
    assert first_json_object("[1, 2] and no object") is None


async def test_structured_retries_greedy_then_succeeds() -> None:
    model = ScriptedModel(
        '{"route": "SOMEWHERE_ELSE", "reason": "x", "confidence": 1}',
        '{"route": "APPLE", "reason": "x", "confidence": 1}',
    )
    out = await structured(model, GenerateRequest.prompt("x", temperature=0.7), RouteSuggestion)
    assert out.route is Route.APPLE
    retry = model.requests[1]
    assert retry.temperature == 0.7  # the caller's sampling: forcing greedy causes loops
    # the retry says what was wrong instead of repeating the same prompt
    assert [m.role for m in retry.messages] == ["user", "assistant", "user"]
    assert "SOMEWHERE_ELSE" in retry.messages[1].content
    assert "could not be used" in retry.messages[2].content and "route" in retry.messages[2].content


async def test_a_looped_reply_is_reported_as_a_loop() -> None:
    class Looping(ScriptedModel):
        async def generate(self, request: GenerateRequest) -> GenerateResult:
            self.requests.append(request)
            if len(self.requests) == 1:
                return GenerateResult("Ok.\n\nOk.\n\n", "m", 1, tokens_out=60, stop_reason="loop")
            return GenerateResult('{"subject": "Fix add", "body": null}', "m", 1)

    model = Looping()
    out = await structured(model, GenerateRequest.prompt("x"), CommitMessage)
    assert out.subject == "Fix add"
    assert "kept repeating itself" in model.requests[1].messages[-1].content


async def test_structured_gives_up_after_retry() -> None:
    model = ScriptedModel("no json here", "still none")
    with pytest.raises(StructuredOutputError, match="RouteSuggestion"):
        await structured(model, GenerateRequest.prompt("x"), RouteSuggestion)


def _fm_ready() -> bool:
    if shutil.which("fm") is None:
        return False
    out = subprocess.run(["fm", "available"], capture_output=True, text=True, check=False)
    return "System model available" in out.stdout


live = pytest.mark.skipif(not _fm_ready(), reason="Apple FM not available")


@pytest.mark.apple_fm
@live
def test_live_fm_schema_generator_agrees() -> None:
    out = subprocess.run(
        ["fm", "schema", "object", "--name", "Inner", "--integer", "n"],
        capture_output=True,
        text=True,
        check=True,
        env={"NO_COLOR": "1", "PATH": "/usr/bin:/bin", "HOME": str(Path.home())},
    )
    assert json.loads(out.stdout) == fm_schema(Inner)


@pytest.mark.apple_fm
@live
async def test_live_classification(tmp_path: Path) -> None:
    environ: dict[str, Any] = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin"}
    repo = tmp_path / "r"
    repo.mkdir()
    env = ExecutionEnvironment.create(
        repo,
        DFMConfig(),
        session_id="live",
        environ=environ,
        dirs=state_dirs({"HOME": str(tmp_path)}),
    )
    model = AppleFMModel(ProcessManager(ProcessRegistry(env.tmp / "p")), env)
    route = await structured(
        model,
        GenerateRequest.prompt(
            "Developer request: git status", instructions="Pick the route.", purpose="classify"
        ),
        RouteSuggestion,
    )
    assert route.route is Route.TOOLS_ONLY
    kind = await structured(
        model,
        GenerateRequest.prompt(
            "Developer request: fix the failing tests in tests/test_api.py", purpose="classify"
        ),
        TaskClassification,
    )
    assert kind.needs_code_change
