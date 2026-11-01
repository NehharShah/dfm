import json
import sys
from pathlib import Path

import pytest

from dfm.config import DFMConfig, state_dirs
from dfm.execution import ExecutionEnvironment
from dfm.models import ContextLimitExceeded, GenerateRequest, ModelTimeout, ModelUnavailable
from dfm.models.mlx import LocalModelSpec, MLXModel, final_text, mlx_available
from dfm.models.mlx.text import named_call_action, tag_action, tool_call_as_action
from dfm.models.schema import fm_schema
from dfm.models.structured import structured
from dfm.models.tasks import CommitMessage
from dfm.orchestration.agent import AgentAction
from tests.helpers import LIVE_MODEL_SKIP, live_model

# The worker is faked; only the live test (skipped on the real check) needs mlx-lm
pytestmark = pytest.mark.usefixtures("mlx_installed")

HARMONY = "<|channel|>analysis<|message|>think hard<|end|><|start|>assistant<|channel|>final<|message|>def add(a, b):\n    return a + b<|return|>"


def test_final_text_variants() -> None:
    assert final_text(HARMONY) == "def add(a, b):\n    return a + b"
    assert final_text("<|channel|>analysis<|message|>only thinking") == ""
    assert (
        final_text("thought one\nthought two<channel|>```python\nx = 1\n```")
        == "```python\nx = 1\n```"
    )
    assert final_text("<think>hmm</think>\nanswer") == "answer"
    assert final_text("reasoning</think>answer") == "answer"
    assert final_text("plain answer") == "plain answer"
    constrained = '<|channel|>analysis<|message|>x<|end|><|start|>assistant<|channel|>final <|constrain|>JSON<|message|>{"a": 1}'
    assert final_text(constrained) == '{"a": 1}'


# Speaks the worker's wire protocol (ids, ok/kind/error, chunks, cancel on a reader thread).
FAKE_WORKER = r"""
import json, queue, sys, threading, time
loaded = None
cancelled = set()
requests = queue.Queue()
def reader():
    for line in sys.stdin:
        req = json.loads(line)
        if req.get("op") == "cancel":
            cancelled.add(req["target"]); continue
        requests.put(req)
    requests.put(None)
threading.Thread(target=reader, daemon=True).start()
while (req := requests.get()) is not None:
    op, rid = req["op"], req["id"]
    mode = open(__file__ + ".mode").read().strip()
    if op == "ping":
        print(json.dumps({"id": rid, "ok": True, "mlx": "x", "loaded": loaded}), flush=True)
    elif op == "load":
        loaded = req["path"]
        open(__file__ + ".log", "a").write("load\n")
        print(json.dumps({"id": rid, "ok": True, "load_s": 0.1, "peak_gb": 1.0}), flush=True)
    elif op == "generate":
        open(__file__ + ".log", "a").write(json.dumps(req["messages"]) + "\n")
        if mode == "crash":
            sys.exit(9)
        if mode == "noise":
            print("some library printed this", flush=True)
        if mode == "torn":
            sys.stdout.write('{"id": %d, "ok": true, "text": "half' % rid); sys.stdout.flush(); sys.exit(9)
        if mode == "slow":
            time.sleep(10)
        if mode == "stuck":  # ignores cancel: only a kill ends it
            time.sleep(30)
        if mode == "context":
            print(json.dumps({"id": rid, "ok": False, "kind": "context", "error": "too long"}), flush=True); continue
        if req.get("stream"):
            for piece in ["def ", "add"]:
                print(json.dumps({"id": rid, "chunk": piece}), flush=True)
        stop = "length"
        if mode == "long":  # 'generates' a token every 50 ms for 3 s, honouring cancel
            for _ in range(60):
                if rid in cancelled:
                    stop = "cancelled"; break
                time.sleep(0.05)
            open(__file__ + ".log", "a").write("stop=" + stop + "\n")
        text = '<|channel|>analysis<|message|>x<|end|><|channel|>final<|message|>{"subject": "Fix add"}<|return|>' if mode == "json" else "<think>t</think>hello"
        if mode == "echo":
            text = req["messages"][-1]["content"]
        print(json.dumps({"id": rid, "ok": True, "text": text, "prompt_tokens": 12, "generation_tokens": 3, "generation_tps": 50.0, "seconds": 0.05, "peak_gb": 1.0, "stop_reason": stop}), flush=True)
    elif op == "unload":
        print(json.dumps({"id": rid, "ok": True}), flush=True)
        if req.get("exit"):
            break
"""


@pytest.fixture
def env(tmp_path: Path) -> ExecutionEnvironment:
    repo = tmp_path / "repo"
    repo.mkdir()
    environ = {"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"}
    return ExecutionEnvironment.create(
        repo, DFMConfig(), session_id="mlx", environ=environ, dirs=state_dirs(environ)
    )


def fake_model(
    tmp_path: Path, env: ExecutionEnvironment, mode: str = "ok", idle_unload_s: float = 600.0
) -> tuple[MLXModel, Path]:
    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER)
    Path(str(script) + ".mode").write_text(mode)
    launcher = tmp_path / "fakepython"
    launcher.write_text(f"#!/bin/sh\nexec {sys.executable} {script}\n")
    launcher.chmod(0o755)
    weights = tmp_path / "weights"
    weights.mkdir(exist_ok=True)
    spec = LocalModelSpec("small", weights, "fake-1b", context=4096)
    model = MLXModel(spec, env, python=str(launcher), sandbox=False, idle_unload_s=idle_unload_s)
    return model, Path(str(script) + ".log")


async def test_lazy_load_and_reasoning_stripped(tmp_path: Path, env: ExecutionEnvironment) -> None:
    model, log = fake_model(tmp_path, env)
    assert model.name == "dfm-small"
    health = await model.healthcheck()
    assert health.ready and "sleeping" in health.detail
    assert not log.exists()  # healthcheck did not load weights
    result = await model.generate(GenerateRequest.prompt("hi", instructions="be brief"))
    assert result.text == "hello"
    assert (result.tokens_in, result.tokens_out) == (12, 3)
    messages = json.loads(log.read_text().splitlines()[1])
    assert messages[0] == {"role": "system", "content": "be brief"}
    await model.generate(GenerateRequest.prompt("again"))
    assert log.read_text().count("load") == 1  # loaded once
    await model.unload()
    assert not model.loaded


async def test_structured_through_mlx(tmp_path: Path, env: ExecutionEnvironment) -> None:
    model, log = fake_model(tmp_path, env, "json")
    out = await structured(model, GenerateRequest.prompt("commit msg"), CommitMessage)
    assert out.subject == "Fix add"
    system = json.loads(log.read_text().splitlines()[1])[0]["content"]
    assert "JSON Schema" in system
    await model.unload()


async def test_failures_are_typed(tmp_path: Path, env: ExecutionEnvironment) -> None:
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
    context, _ = fake_model(tmp_path / "a", env, "context")
    with pytest.raises(ContextLimitExceeded):
        await context.generate(GenerateRequest.prompt("x"))
    await context.unload()
    crash, _ = fake_model(tmp_path / "b", env, "crash")
    with pytest.raises(ModelUnavailable, match="out of memory"):
        await crash.generate(GenerateRequest.prompt("x"))
    slow, _ = fake_model(tmp_path / "c", env, "slow")
    with pytest.raises(ModelTimeout):
        await slow.generate(GenerateRequest.prompt("x", timeout_s=0.5))
    assert not slow.loaded
    missing = MLXModel(LocalModelSpec("primary", tmp_path / "nope"), env, sandbox=False)
    with pytest.raises(ModelUnavailable, match="not found"):
        await missing.generate(GenerateRequest.prompt("x"))
    assert not (await missing.healthcheck()).ready


async def test_stream(tmp_path: Path, env: ExecutionEnvironment) -> None:
    model, _ = fake_model(tmp_path, env)
    chunks = [c async for c in model.stream(GenerateRequest.prompt("x"))]
    assert chunks == ["def ", "add"]
    await model.unload()


async def test_idle_unload(tmp_path: Path, env: ExecutionEnvironment) -> None:
    import asyncio

    model, _ = fake_model(tmp_path, env, idle_unload_s=0.2)
    await model.generate(GenerateRequest.prompt("x"))
    assert model.loaded
    await asyncio.sleep(0.6)
    assert not model.loaded


async def test_cancel_stops_the_worker_and_keeps_it_loaded(
    tmp_path: Path, env: ExecutionEnvironment
) -> None:
    """Ctrl-C (task cancellation) reaches the worker, which stops after its current token and
    stays loaded; the next request is served right away rather than queueing behind an orphan."""
    import asyncio
    import time

    model, log = fake_model(tmp_path, env, "long")
    task = asyncio.create_task(model.generate(GenerateRequest.prompt("x")))
    await asyncio.sleep(0.4)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert model.loaded  # not killed: it answered the cancel
    assert "stop=cancelled" in log.read_text()
    Path(str(tmp_path / "fake_worker.py") + ".mode").write_text("ok")
    started = time.monotonic()
    result = await model.generate(GenerateRequest.prompt("y"))
    assert result.text == "hello" and time.monotonic() - started < 1.0
    assert log.read_text().count("load") == 1
    await model.unload()


async def test_timeout_interrupts_then_kills_only_if_the_worker_is_stuck(
    tmp_path: Path, env: ExecutionEnvironment
) -> None:
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    polite, _ = fake_model(tmp_path / "a", env, "long")
    with pytest.raises(ModelTimeout):
        await polite.generate(GenerateRequest.prompt("x", timeout_s=0.3))
    assert polite.loaded  # it honoured the cancel within the grace period
    await polite.unload()
    stuck, _ = fake_model(tmp_path / "b", env, "stuck")
    with pytest.raises(ModelTimeout):
        await stuck.generate(GenerateRequest.prompt("x", timeout_s=0.3))
    assert not stuck.loaded  # no answer to the cancel: killed


async def test_stream_consumer_stopping_early_cancels_generation(
    tmp_path: Path, env: ExecutionEnvironment
) -> None:
    from collections.abc import AsyncGenerator
    from typing import cast

    model, log = fake_model(tmp_path, env, "long")
    gen = cast(AsyncGenerator[str, None], model.stream(GenerateRequest.prompt("x")))
    await anext(gen)  # the first chunk arrives before the slow 'tokens'
    await gen.aclose()
    import asyncio

    await asyncio.sleep(0.2)  # the fake logs its stop reason after answering the cancel
    assert model.loaded
    assert "stop=cancelled" in log.read_text()
    await model.unload()


async def test_non_protocol_lines_and_torn_replies_are_typed_errors(
    tmp_path: Path, env: ExecutionEnvironment
) -> None:
    """A library print on stdout is skipped; a reply cut short by a crash is 'worker exited',
    never a raw JSONDecodeError that bypasses the ModelError handlers."""
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    noisy, _ = fake_model(tmp_path / "a", env, "noise")
    assert (await noisy.generate(GenerateRequest.prompt("x"))).text == "hello"
    await noisy.unload()
    torn, _ = fake_model(tmp_path / "b", env, "torn")
    with pytest.raises(ModelUnavailable, match="worker exited"):
        await torn.generate(GenerateRequest.prompt("x"))


@pytest.mark.mlx
@pytest.mark.skipif(not mlx_available() or live_model() is None, reason=LIVE_MODEL_SKIP)
async def test_live_local_model(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    environ = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin"}
    env = ExecutionEnvironment.create(
        repo,
        DFMConfig(),
        session_id="live-mlx",
        environ=environ,
        dirs=state_dirs({"HOME": str(tmp_path)}),
    )
    path = live_model()
    assert path is not None
    model = MLXModel(LocalModelSpec("small", path, path.name, context=16384), env)
    try:
        result = await model.generate(
            GenerateRequest.prompt(
                "Fix so add(2,3)==5. Reply with only code.\n\ndef add(a, b):\n    return a - b",
                timeout_s=300,
            )
        )
        assert "a + b" in result.text and "<|channel|>" not in result.text
        commit = await structured(
            model,
            GenerateRequest.prompt(
                "Write a commit message for: fixed add() returning a difference", timeout_s=300
            ),
            CommitMessage,
        )
        assert commit.subject
    finally:
        await model.unload()


def test_harmony_tool_call_translation() -> None:
    raw = (
        "<|channel|>analysis<|message|>The add function subtracts. Let's patch ops.py.<|end|>"
        "<|start|>assistant<|channel|>commentary to=repo_browser.patch <|constrain|>json<|message|>"
        '{"path":"src/calc/ops.py","search":"return a - b","replace":"return a + b"}'
    )
    allowed = ["read", "search", "patch", "create", "run", "test", "done", "give_up"]
    action = json.loads(tool_call_as_action(raw, allowed) or "{}")
    assert action == {
        "path": "src/calc/ops.py",
        "search": "return a - b",
        "replace": "return a + b",
        "reason": "The add function subtracts. Let's patch ops.py.",
        "action": "patch",
    }
    search = '<|channel|>commentary to=functions.grep<|message|>{"query": "def add"}'
    assert json.loads(tool_call_as_action(search, allowed) or "{}")["pattern"] == "def add"
    assert tool_call_as_action("<|channel|>commentary to=x.teleport<|message|>{}", allowed) is None
    assert tool_call_as_action("<|channel|>final<|message|>{}", allowed) is None
    addressed = (
        "<|channel|>analysis<|message|>Inspect it.<|end|><|start|>assistant<|channel|>analysis to=assistant code"
        '<|message|>{"action":"read","reason":"look","path":"src/calc/ops.py"}'
    )
    assert json.loads(tool_call_as_action(addressed, allowed) or "{}")["action"] == "read"


ALLOWED = ["read", "search", "patch", "create", "run", "test", "done", "give_up"]


def test_harmony_listing_tools_become_a_read_only_run() -> None:
    # seen live from gpt-oss-20b on the python-bug benchmark task
    raw = (
        "<|channel|>analysis<|message|>We need to inspect repository. Let's list files.<|end|>"
        '<|start|>assistant<|channel|>analysis to=repo_browser.print_tree code<|message|>{"path": "", "depth": 2}'
    )
    assert json.loads(tool_call_as_action(raw, ALLOWED) or "{}") == {
        "command": "git ls-files",
        "reason": "We need to inspect repository. Let's list files.",
        "action": "run",
    }
    sub = '<|channel|>commentary to=repo_browser.print_tree<|message|>{"path": "src/my dir/"}'
    assert json.loads(tool_call_as_action(sub, ALLOWED) or "{}")["command"] == (
        "git ls-files -- 'src/my dir'"
    )
    opened = (
        "<|channel|>commentary to=repo_browser.open_file<|message|>"
        '{"path": "a.py", "line_start": 1, "line_end": 9}'
    )
    action = json.loads(tool_call_as_action(opened, ALLOWED) or "{}")
    assert (action["action"], action["start"], action["end"]) == ("read", 1, 9)
    assert tool_call_as_action(sub, ["read"]) is None  # run not allowed: no translation


def test_harmony_variants_seen_on_the_go_refactor_task() -> None:
    hyphen = (
        '<|channel|>commentary to=repo-search code<|message|>{"path":"calc/calc.go","query":"func"}'
    )
    assert json.loads(tool_call_as_action(hyphen, ALLOWED) or "{}")["action"] == "search"
    colon = '<|channel|>analysis to=assistant:read code<|message|>{"path":"calc/calc_test.go"}'
    assert json.loads(tool_call_as_action(colon, ALLOWED) or "{}") == {
        "path": "calc/calc_test.go",
        "reason": "read",
        "action": "read",
    }
    # a final answer without `<|message|>`, and a stray brace after the object
    no_message = (
        "<|channel|>analysis<|message|>patch it<|end|><|start|>assistant<|channel|>final "
        '<|constrain|>{"action":"patch","reason":"r","path":"calc/calc.go","search":"a","replace":"b"} }'
    )
    assert final_text(no_message).startswith('{"action":"patch"')
    assert final_text(HARMONY) == "def add(a, b):\n    return a + b"  # the usual form is unchanged


def test_markup_actions_are_translated() -> None:
    # seen live from Qwen3.6-27B on the ci-failure benchmark task
    done = '<done summary="Fixed slugify; tests pass (1 passed, &quot;0&quot; failed).">'
    assert json.loads(tag_action(done, ALLOWED) or "{}") == {
        "summary": 'Fixed slugify; tests pass (1 passed, "0" failed).',
        "reason": "done",
        "action": "done",
    }
    xml = (
        "<tool_call>\n<function=read>\n<parameter=path>\nsrc/a.py\n</parameter>\n"
        "<parameter=start>\n3\n</parameter>\n</function>\n</tool_call>"
    )
    assert json.loads(tag_action(xml, ALLOWED) or "{}") == {
        "path": "src/a.py",
        "start": 3,
        "reason": "read",
        "action": "read",
    }
    assert tag_action('<teleport to="mars">', ALLOWED) is None
    assert tag_action("plain prose, no markup", ALLOWED) is None
    call = {"name": "run_command", "arguments": {"cmd": "ls"}}
    assert named_call_action(call, ALLOWED) == {"command": "ls", "reason": "run", "action": "run"}
    assert named_call_action({"action": "read", "name": "x", "arguments": {}}, ALLOWED) is None
    assert named_call_action({"name": "teleport", "arguments": {}}, ALLOWED) is None


async def test_generate_translates_markup_actions(
    tmp_path: Path, env: ExecutionEnvironment
) -> None:
    model, _ = fake_model(tmp_path, env, "echo")
    schema = fm_schema(AgentAction)
    try:
        result = await model.generate(GenerateRequest.prompt('<done summary="ok">', schema=schema))
        assert result.parsed == {"summary": "ok", "reason": "done", "action": "done"}
        named = '{"name": "read", "arguments": {"path": "a.py"}}'
        result = await model.generate(GenerateRequest.prompt(named, schema=schema))
        assert result.parsed == {"path": "a.py", "reason": "read", "action": "read"}
        plain = await model.generate(GenerateRequest.prompt('<done summary="ok">'))
        assert plain.text == '<done summary="ok">' and plain.parsed is None  # no schema: as is
    finally:
        await model.unload()


def test_agent_action_accepts_argv_commands() -> None:
    # seen live from gpt-oss-20b: "command": ["bash", "-lc", "ls -R"]
    wrapped = AgentAction(action="run", reason="look", command=["bash", "-lc", "ls -R"])  # type: ignore[arg-type]
    assert wrapped.command == "ls -R"
    argv = AgentAction(action="run", reason="look", command=["grep", "-rn", "a b", "src"])  # type: ignore[arg-type]
    assert argv.command == "grep -rn 'a b' src"
    assert AgentAction(action="run", reason="x", command="ls").command == "ls"


def test_harmony_tool_names_with_spaces_or_run_together() -> None:
    # seen live from gpt-oss-20b on go-refactor, with model-recommended sampling
    spaced = (
        "<|channel|>analysis<|message|>Let's view it.<|end|><|start|>assistant<|channel|>analysis"
        ' to=assistant read code<|message|>{"path":"calc/calc_test.go","start":1,"end":200}'
    )
    assert json.loads(tool_call_as_action(spaced, ALLOWED) or "{}")["action"] == "read"
    namespace = '<|channel|>commentary to=repo search code<|message|>{"pattern":"Add(a, b)"}\n'
    assert json.loads(tool_call_as_action(namespace, ALLOWED) or "{}") == {
        "pattern": "Add(a, b)",
        "reason": "search",
        "action": "search",
    }
    joined = '<|channel|>analysis to=reposearch code<|message|>{"path":"", "pattern":"Add"}'
    assert json.loads(tool_call_as_action(joined, ALLOWED) or "{}")["action"] == "search"
    unknown = '<|channel|>commentary to=assistant teleport code<|message|>{"to":"mars"}'
    assert tool_call_as_action(unknown, ALLOWED) is None


def test_harmony_call_to_final_is_done() -> None:
    # seen live from gpt-oss-20b after a successful fix; it ended the run as a failure before
    raw = (
        "<|channel|>analysis<|message|>The patch is done. So next action: done.<|end|>"
        "<|start|>assistant<|channel|>commentary to=final <|constrain|>json<|message|>{}"
    )
    assert json.loads(tool_call_as_action(raw, ALLOWED) or "{}") == {
        "reason": "The patch is done. So next action: done.",
        "action": "done",
    }


async def test_qwen_xml_call_with_braces_in_content(
    tmp_path: Path, env: ExecutionEnvironment
) -> None:
    model, _ = fake_model(tmp_path, env, "echo")
    xml = (
        "<tool_call>\n<function=create>\n<parameter=path>\nconf.py\n</parameter>\n"
        '<parameter=content>\nCONFIG = {"debug": true}\n</parameter>\n</function>\n</tool_call>'
    )
    try:
        result = await model.generate(GenerateRequest.prompt(xml, schema=fm_schema(AgentAction)))
        # the file's own braces must not be mistaken for the action
        assert result.parsed == {
            "path": "conf.py",
            "content": 'CONFIG = {"debug": true}',
            "reason": "create",
            "action": "create",
        }
    finally:
        await model.unload()


async def test_rests_have_their_own_allowance_and_streams_cool_down(
    tmp_path: Path, env: ExecutionEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    model, _ = fake_model(tmp_path, env)
    timeout, params = model._generate_params(GenerateRequest.prompt("x", timeout_s=300))  # pyright: ignore[reportPrivateUsage]
    assert (timeout, params["rest_budget_s"]) == (600, 300)  # pacing can't trip the timeout
    off = MLXModel(LocalModelSpec("small", tmp_path, thermal_limit=None), env, sandbox=False)
    assert off._generate_params(GenerateRequest.prompt("x", timeout_s=300))[0] == 300  # pyright: ignore[reportPrivateUsage]
    cooled: list[bool] = []

    async def fake_cool_down() -> float:
        cooled.append(True)
        return 0.0

    monkeypatch.setattr(model, "_cool_down", fake_cool_down)
    try:
        assert "".join([c async for c in model.stream(GenerateRequest.prompt("hi"))])
        assert cooled == [True]
    finally:
        await model.unload()


def test_native_tools_replace_the_json_schema_instructions(
    tmp_path: Path, env: ExecutionEnvironment
) -> None:
    model, _ = fake_model(tmp_path, env)
    tools = ({"type": "function", "function": {"name": "read"}},)
    request = GenerateRequest.prompt(
        "x", schema=fm_schema(AgentAction), tools=tools, instructions="rules"
    )
    assert "JSON Schema" in model._messages(request)[0]["content"]  # pyright: ignore[reportPrivateUsage]
    model.native_tools = True  # learned when a tool-capable template loads
    system = model._messages(request)[0]["content"]  # pyright: ignore[reportPrivateUsage]
    assert "JSON Schema" not in system and "exactly one call" in system
    assert model._generate_params(request)[1]["tools"] == list(tools)  # pyright: ignore[reportPrivateUsage]
