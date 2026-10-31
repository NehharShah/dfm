import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dfm.config import DFMConfig, state_dirs
from dfm.execution import ExecutionEnvironment
from dfm.models import (
    ContextLimitExceeded,
    GenerateRequest,
    Message,
    Model,
    ModelIncompatible,
    ModelRefusal,
    ModelTimeout,
    ModelUnavailable,
    RemoteModelRefused,
    StructuredOutputError,
)
from dfm.models.apple import AppleFMModel
from dfm.processes import Escalation, ProcessManager, ProcessRegistry


@pytest.fixture
def env(tmp_path: Path) -> ExecutionEnvironment:
    repo = tmp_path / "repo"
    repo.mkdir()
    environ = {"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"}
    return ExecutionEnvironment.create(
        repo, DFMConfig(), session_id="fm", environ=environ, dirs=state_dirs(environ)
    )


@pytest.fixture
def pm(env: ExecutionEnvironment) -> ProcessManager:
    return ProcessManager(
        ProcessRegistry(env.tmp / "p"), escalation=Escalation(int_grace_s=0.2, term_grace_s=0.2)
    )


def fake_fm(
    tmp_path: Path, respond: str, *, code: int = 0, sleep: float = 0, tokens: int = 10
) -> str:
    """A stand-in `fm` that logs its argv and stdin, then answers `respond`."""
    script = tmp_path / "fm"
    log = tmp_path / "fm.log"
    script.write_text(
        f"""#!{sys.executable}
import json, sys, time
args = sys.argv[1:]
data = sys.stdin.read() if not sys.stdin.isatty() else ""
with open({str(log)!r}, "a") as fh:
    fh.write(json.dumps({{"args": args, "stdin": data}}) + "\\n")
if args[:1] == ["available"]:
    print("System model available"); sys.exit(0)
if args[:1] == ["count-tokens"]:
    print({tokens}); sys.exit(0)
time.sleep({sleep})
out = {respond!r}
stream = sys.stderr if out.startswith("Error:") else sys.stdout
print(out, file=stream)
sys.exit({code})
"""
    )
    script.chmod(0o755)
    return str(script)


def calls(tmp_path: Path) -> list[tuple[list[str], str]]:
    out: list[tuple[list[str], str]] = []
    for line in (tmp_path / "fm.log").read_text().splitlines():
        record = json.loads(line)
        out.append(([str(a) for a in record["args"]], str(record["stdin"])))
    return out


def test_is_a_model(pm: ProcessManager, env: ExecutionEnvironment) -> None:
    assert isinstance(AppleFMModel(pm, env), Model)


def test_refuses_any_other_model(pm: ProcessManager, env: ExecutionEnvironment) -> None:
    with pytest.raises(RemoteModelRefused):
        AppleFMModel(pm, env, model="gpt-4")


async def test_generate_uses_verified_flags(
    tmp_path: Path, pm: ProcessManager, env: ExecutionEnvironment
) -> None:
    model = AppleFMModel(pm, env, binary=fake_fm(tmp_path, "hello"))
    request = GenerateRequest(
        messages=(Message("system", "Be brief."), Message("user", "Say hello")),
        instructions="You are DFM's helper.",
        temperature=0,
    )
    result = await model.generate(request)
    assert result.text == "hello"
    assert result.model == "apple-fm"
    args, stdin = calls(tmp_path)[-1]
    assert args[:4] == ["respond", "-m", "system", "--no-stream"]
    assert "-g" in args
    assert args[args.index("-i") + 1] == "You are DFM's helper.\n\nBe brief."
    assert stdin == "Say hello"


async def test_transcript_rendering(
    tmp_path: Path, pm: ProcessManager, env: ExecutionEnvironment
) -> None:
    model = AppleFMModel(pm, env, binary=fake_fm(tmp_path, "ok"))
    await model.generate(
        GenerateRequest(
            messages=(Message("user", "a"), Message("assistant", "b"), Message("user", "c"))
        )
    )
    assert calls(tmp_path)[-1][1] == "User:\na\n\nAssistant:\nb\n\nUser:\nc"


async def test_transform_purpose_uses_content_guardrails(
    tmp_path: Path, pm: ProcessManager, env: ExecutionEnvironment
) -> None:
    model = AppleFMModel(pm, env, binary=fake_fm(tmp_path, "summary"))
    await model.generate(GenerateRequest.prompt("long log", purpose="transform"))
    args = calls(tmp_path)[-1][0]
    assert args[args.index("--guardrails") + 1] == "permissive-content-transformations"


async def test_structured_output(
    tmp_path: Path, pm: ProcessManager, env: ExecutionEnvironment
) -> None:
    model = AppleFMModel(pm, env, binary=fake_fm(tmp_path, '{"route": "TOOLS_ONLY"}'))
    schema = {"type": "object", "properties": {"route": {"type": "string"}}}
    result = await model.generate(GenerateRequest.prompt("git status", schema=schema))
    assert result.parsed == {"route": "TOOLS_ONLY"}
    args = calls(tmp_path)[-1][0]
    assert "--schema" in args
    assert not list(env.tmp.glob("fm-schema-*.json"))  # temp schema removed


async def test_non_json_for_schema(
    tmp_path: Path, pm: ProcessManager, env: ExecutionEnvironment
) -> None:
    model = AppleFMModel(pm, env, binary=fake_fm(tmp_path, "not json"))
    with pytest.raises(StructuredOutputError):
        await model.generate(GenerateRequest.prompt("x", schema={"type": "object"}))


@pytest.mark.parametrize(
    ("output", "error"),
    [
        (
            "Error: The session's transcript exceeded the model's context size.",
            ContextLimitExceeded,
        ),
        ("Error: The model's safety guardrails were triggered.", ModelRefusal),
        ("Error: Apple Intelligence is not enabled.", ModelUnavailable),
        ("Error: You must agree to the license first.", ModelUnavailable),
        ("Error: The value 'x' is invalid for '--guardrails <level>'.", ModelIncompatible),
    ],
)
async def test_failures_are_typed(
    tmp_path: Path,
    pm: ProcessManager,
    env: ExecutionEnvironment,
    output: str,
    error: type[Exception],
) -> None:
    model = AppleFMModel(pm, env, binary=fake_fm(tmp_path, output, code=1))
    with pytest.raises(error):
        await model.generate(GenerateRequest.prompt("x"))


async def test_timeout(tmp_path: Path, pm: ProcessManager, env: ExecutionEnvironment) -> None:
    model = AppleFMModel(pm, env, binary=fake_fm(tmp_path, "late", sleep=10))
    with pytest.raises(ModelTimeout):
        await model.generate(GenerateRequest.prompt("x", timeout_s=0.5))


async def test_missing_binary(
    tmp_path: Path, pm: ProcessManager, env: ExecutionEnvironment
) -> None:
    model = AppleFMModel(pm, env, binary=str(tmp_path / "no-such-fm"))
    with pytest.raises(ModelUnavailable):
        await model.generate(GenerateRequest.prompt("x"))
    health = await model.healthcheck()
    assert not health.ready


async def test_context_precheck(
    tmp_path: Path, pm: ProcessManager, env: ExecutionEnvironment
) -> None:
    model = AppleFMModel(pm, env, binary=fake_fm(tmp_path, "never", tokens=9000))
    with pytest.raises(ContextLimitExceeded):
        await model.generate(GenerateRequest.prompt("word " * 3000))
    assert not any(args[:1] == ["respond"] for args, _ in calls(tmp_path))


async def test_healthcheck_ok(
    tmp_path: Path, pm: ProcessManager, env: ExecutionEnvironment
) -> None:
    health = await AppleFMModel(pm, env, binary=fake_fm(tmp_path, "OK")).healthcheck()
    assert health.ready
    assert health.latency_ms is not None


async def test_stream(tmp_path: Path, pm: ProcessManager, env: ExecutionEnvironment) -> None:
    model = AppleFMModel(pm, env, binary=fake_fm(tmp_path, "one\ntwo"))
    chunks = [c async for c in model.stream(GenerateRequest.prompt("x"))]
    assert "".join(chunks) == "one\ntwo\n"
    args = calls(tmp_path)[-1][0]
    assert "--stream" in args


def _fm_ready() -> bool:
    if shutil.which("fm") is None:
        return False
    out = subprocess.run(["fm", "available"], capture_output=True, text=True, check=False)
    return "System model available" in out.stdout


live = pytest.mark.skipif(not _fm_ready(), reason="Apple FM not available on this machine")


@pytest.mark.apple_fm
@live
async def test_live_generate_and_schema(pm: ProcessManager, env: ExecutionEnvironment) -> None:
    model = AppleFMModel(pm, env, binary=shutil.which("fm") or "fm")
    health = await model.healthcheck()
    assert health.ready, health
    schema = {
        "title": "Route",
        "additionalProperties": False,
        "x-order": ["route"],
        "type": "object",
        "required": ["route"],
        "properties": {"route": {"type": "string", "description": "one of TOOLS_ONLY, APPLE"}},
    }
    result = await model.generate(
        GenerateRequest.prompt("git status", schema=schema, purpose="classify")
    )
    assert result.parsed is not None and "route" in result.parsed
    assert await model.count_tokens("Hello world") > 0
