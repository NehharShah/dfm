"""The interactive session (`dfm` in a terminal), driven with scripted coders: no model loads."""

import asyncio
import io
import json
import os
import signal
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from dfm.interactive.display import Display
from dfm.interactive.repl import run
from dfm.interactive.session import InteractiveSession
from dfm.models import GenerateRequest, GenerateResult, ModelHealth
from dfm.orchestration.runtime import Runtime
from dfm.policy import Approval, InteractiveApprover
from dfm.policy.types import ActionClass, Confirmation, Decision, PolicyVerdict, ProposedAction
from dfm.recovery import Checkpointer
from dfm.routing import Router, RoutingContext
from tests.helpers import BUGGY, DONE, FIX, ScriptedCoder

FIXED = "def add(a, b):\n    return a + b\n"
SWAP = {
    "action": "patch",
    "reason": "same sum, other order",
    "path": "src/calc/ops.py",
    "search": "return a + b",
    "replace": "return b + a",
}


@pytest.fixture
def repo(calc_repo: Path) -> Path:
    return calc_repo


def make_session(
    repo: Path, coder: ScriptedCoder | None = None
) -> tuple[InteractiveSession, io.StringIO]:
    out = io.StringIO()
    rt = Runtime.create(repo, request="interactive session", mode="interactive")
    display = Display(Console(file=out, width=140), rt.repo)
    rt.listeners.append(display.event)
    return InteractiveSession(rt, display, coder=coder), out


async def test_request_runs_the_agent_loop_and_shows_each_step(home: Path, repo: Path) -> None:
    coder = ScriptedCoder(
        {"action": "read", "reason": "look", "path": "src/calc/ops.py"}, FIX, DONE
    )
    session, out = make_session(repo, coder)
    assert await session.handle("fix the failing test")
    await session.rt.close("completed")
    shown = out.getvalue()
    assert (repo / "src/calc/ops.py").read_text() == FIXED
    for expected in (
        "Baseline tests",
        "Read src/calc/ops.py",
        "Edit src/calc/ops.py",
        "-    return a - b",
        "+    return a + b",
        "1 passed, 0 failed",
        "✓ completed",
        "tests pass: `pytest",
    ):
        assert expected in shown, expected
    assert session.turns[0].status == "completed"
    assert session.turns[0].changed == ["src/calc/ops.py"]


async def test_later_turns_keep_editing_dfms_own_changes(home: Path, repo: Path) -> None:
    session, _ = make_session(repo, ScriptedCoder(FIX, DONE))
    await session.handle("fix the failing test")
    session.coder = ScriptedCoder(SWAP, DONE)
    await session.handle("change add to return b + a")
    assert (repo / "src/calc/ops.py").read_text() == "def add(a, b):\n    return b + a\n"
    # earlier requests are context for the coder
    assert "Earlier requests in this session" in session.coder.prompts[0]
    assert "fix the failing test -> completed" in session.coder.prompts[0]

    # once you edit the file yourself, it is your work again: DFM may not overwrite it
    (repo / "src/calc/ops.py").write_text(FIXED + "# mine\n")
    back = {**SWAP, "search": "return a + b", "replace": "return a+b"}
    session.coder = ScriptedCoder(back, DONE)
    await session.handle("tidy add")
    await session.rt.close("completed")
    assert (repo / "src/calc/ops.py").read_text() == FIXED + "# mine\n"
    assert "uncommitted changes" in session.coder.prompts[1]


async def test_undo_reverts_the_latest_request_only(home: Path, repo: Path) -> None:
    session, out = make_session(repo, ScriptedCoder(FIX, DONE))
    await session.handle("fix the failing test")
    session.coder = ScriptedCoder(SWAP, DONE)
    await session.handle("change add to return b + a")
    await session.handle("/undo")
    assert (repo / "src/calc/ops.py").read_text() == FIXED
    await session.handle("/undo")
    assert (repo / "src/calc/ops.py").read_text() == BUGGY
    await session.handle("/undo")
    await session.rt.close("completed")
    shown = out.getvalue()
    assert "Undid request 2" in shown and "Undid request 1" in shown
    assert "restored src/calc/ops.py" in shown
    assert "Nothing to undo" in shown


async def test_undo_keeps_files_changed_since_and_removes_created_ones(
    home: Path, repo: Path
) -> None:
    create = {
        "action": "create",
        "reason": "helper",
        "path": "src/calc/extra.py",
        "content": "X = 1\n",
    }
    session, out = make_session(repo, ScriptedCoder(FIX, create, DONE))
    await session.handle("fix the failing test")
    (repo / "src/calc/ops.py").write_text(FIXED + "# mine\n")
    await session.handle("/undo")
    await session.rt.close("completed")
    assert (repo / "src/calc/ops.py").read_text() == FIXED + "# mine\n"
    assert not (repo / "src/calc/extra.py").exists()
    shown = out.getvalue()
    assert "kept src/calc/ops.py: changed since DFM edited it" in shown
    assert "removed src/calc/extra.py" in shown


def test_checkpointer_undo_ignores_paths_it_never_recorded(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "a.txt").write_text("before\n")
    cp = Checkpointer(repo, "s-t1")
    cp.record_original("a.txt")
    (repo / "a.txt").write_text("after\n")
    left = {p: c.after for p, c in cp.modified_files().items()}
    result = cp.undo({**left, "../outside.txt": None, "b.txt": None})
    assert (repo / "a.txt").read_text() == "before\n"
    assert result.restored == ["a.txt"]
    assert len(result.skipped) == 2


async def test_diff_and_commands(home: Path, repo: Path) -> None:
    session, out = make_session(repo, ScriptedCoder(FIX, DONE))
    await session.handle("fix the failing test")
    out.truncate(0)
    out.seek(0)
    await session.handle("/diff")
    assert "-    return a - b" in out.getvalue() and "+    return a + b" in out.getvalue()
    assert await session.handle("/help")
    assert await session.handle("/frobnicate")
    assert await session.handle("/plan")
    assert await session.handle("/status")
    assert not await session.handle("/exit")
    await session.rt.close("completed")
    shown = out.getvalue()
    assert "/undo" in shown  # help
    assert "unknown command /frobnicate" in shown
    assert "/plan needs text" in shown
    assert "files changed: src/calc/ops.py" in shown


async def test_questions_without_a_model_answer_from_facts(home: Path, repo: Path) -> None:
    session, out = make_session(repo)  # no coder configured in the test home
    await session.handle("explain this repository")
    await session.handle("fix the failing test")
    await session.rt.close("completed")
    shown = out.getvalue()
    assert "deterministic facts" in shown
    assert "No local coding model is configured" in shown
    assert (repo / "src/calc/ops.py").read_text() == BUGGY
    assert [t.kind for t in session.turns] == ["question"]


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("fix the failing test", "task"),
        ("add a docstring to add()", "task"),
        ("review my changes", "review"),
        ("explain this repository", "question"),
        ("what were we doing yesterday", "question"),
        ("hello there", "question"),
    ],
)
async def test_classify(text: str, kind: str) -> None:
    decision = await Router().route(text, RoutingContext(apple_available=False))
    assert InteractiveSession.classify(text, decision) == kind


class SlowCoder:
    name = "slow-coder"

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def healthcheck(self) -> ModelHealth:
        return ModelHealth(self.name, True, "ok")

    async def generate(self, request: GenerateRequest) -> GenerateResult:
        self.started.set()
        await asyncio.sleep(3600)
        raise AssertionError("not cancelled")

    async def stream(self, request: GenerateRequest) -> AsyncIterator[str]:
        yield ""


async def test_cancelling_a_request_keeps_the_session(home: Path, repo: Path) -> None:
    coder = SlowCoder()
    session, _ = make_session(repo, coder)  # type: ignore[arg-type]
    task = asyncio.create_task(session.handle("fix the failing test"))
    await asyncio.wait_for(coder.started.wait(), 60)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.turns[0].status == "interrupted"
    session.coder = ScriptedCoder(FIX, DONE)  # type: ignore[assignment]
    await session.handle("fix the failing test")
    await session.rt.close("completed")
    assert (repo / "src/calc/ops.py").read_text() == FIXED


async def _run_repl(
    repo: Path, lines: str, coder: object, *, interrupt: SlowCoder | None = None
) -> tuple[int, str]:
    out = io.StringIO()
    with create_pipe_input() as pipe:
        repl = asyncio.create_task(
            run(
                repo,
                console=Console(file=out, width=140),
                pt_input=pipe,
                pt_output=DummyOutput(),
                coder=coder,  # type: ignore[arg-type]
            )
        )
        pipe.send_text(lines)
        if interrupt is not None:
            await asyncio.wait_for(interrupt.started.wait(), 60)
            os.kill(os.getpid(), signal.SIGINT)  # Ctrl-C while the request runs
            await asyncio.sleep(0.2)
            pipe.send_text("/status\n/exit\n")
        code = await asyncio.wait_for(repl, 120)
    return code, out.getvalue()


def _last_session(home: Path) -> dict[str, object]:
    files = sorted(
        (home / ".local/share/dfm/sessions").rglob("*.jsonl"), key=lambda p: p.stat().st_mtime
    )
    lines = files[-1].read_text().splitlines()
    return {"header": json.loads(lines[0]), "events": [json.loads(x) for x in lines[1:]]}


async def test_prompt_loop_end_to_end(home: Path, repo: Path) -> None:
    code, shown = await _run_repl(
        repo, "fix the failing test\n/undo\n/exit\n", ScriptedCoder(FIX, DONE)
    )
    assert code == 0
    assert "✓ completed" in shown and "Undid request 1" in shown
    assert (repo / "src/calc/ops.py").read_text() == BUGGY
    trace = _last_session(home)
    assert trace["header"]["mode"] == "interactive"  # type: ignore[index]
    events = trace["events"]
    assert events[-1]["type"] == "task_completed" and events[-1]["status"] == "completed"  # type: ignore[index]


async def test_ctrl_c_stops_the_request_not_the_session(home: Path, repo: Path) -> None:
    coder = SlowCoder()
    code, shown = await _run_repl(repo, "fix the failing test\n", coder, interrupt=coder)
    assert code == 0
    assert "Stopped." in shown
    assert "requests: 1" in shown  # /status ran after the interrupt


async def test_hard_confirmation_through_the_session_prompt() -> None:
    answers = iter(["y", "DESTRUCTIVE"])

    async def ainput(prompt: str) -> str:
        assert prompt == "Type 'DESTRUCTIVE' to allow once: "
        return next(answers)

    approver = InteractiveApprover(
        console=Console(file=io.StringIO()), is_tty=lambda: True, ainput=ainput
    )
    action = ProposedAction("shell", ActionClass.DESTRUCTIVE, "git reset --hard", "model:coder")
    verdict = PolicyVerdict(
        Decision.ASK, ActionClass.DESTRUCTIVE, "DESTRUCTIVE default", Confirmation.HARD
    )
    assert await approver.ask(action, verdict) is Approval.DENY  # "y" is not the phrase
    assert await approver.ask(action, verdict) is Approval.ONCE


async def test_a_failing_listener_never_stops_the_work(home: Path, repo: Path) -> None:
    rt = Runtime.create(repo, request="x", mode="interactive")

    def broken(event_type: str, payload: dict[str, object]) -> None:
        raise RuntimeError("display bug")

    rt.listeners.append(broken)
    rt.event("note", text="still recorded")
    await rt.close("completed")
    assert any(e.get("text") == "still recorded" for e in rt.store.load(rt.session.id).events)
