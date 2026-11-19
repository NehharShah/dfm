"""Regression tests for bounded read-only answers in the agent loop."""

from pathlib import Path
from types import SimpleNamespace

import pytest

import dfm.orchestration.agent as agent


@pytest.fixture
def loop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> agent.AgentLoop:
    async def gather_stub(*_args: object) -> SimpleNamespace:
        return SimpleNamespace(canon=None)

    monkeypatch.setattr(agent, "gather", gather_stub)
    monkeypatch.setattr(agent, "select_adapter", lambda *_args: None)
    worker = object.__new__(agent.AgentLoop)
    object.__setattr__(
        worker,
        "rt",
        SimpleNamespace(repo=tmp_path, canon=None, graph=None, event=lambda *_args, **_kwargs: None),
    )
    worker.adapter = None
    worker.commands = None
    worker.coder = None
    worker.max_steps = 5
    worker.token_budget = 10000
    worker.history = ["Step from previous question: unrelated"]
    worker.compacted = ["- previous question"]
    worker.struggling = False
    monkeypatch.setattr(worker, "out_of_time", lambda: False)
    monkeypatch.setattr(
        worker,
        "_observe",
        lambda step, action, result: worker.history.append(
            f"Step {step}: {action.action} ({action.reason})\n{result}"
        ),
    )
    return worker


@pytest.mark.asyncio
async def test_repeated_read_forces_final_answer(loop: agent.AgentLoop) -> None:
    calls: list[set[str]] = []

    async def next_action(
        _question: str, step: int, *, instructions: str, tools: tuple
    ) -> agent.AgentAction:
        names = {item["function"]["name"] for item in tools}
        calls.append(names)
        if names == {"done", "give_up"}:
            return agent.AgentAction(
                action="give_up", reason="blocked", summary="Requested logs are inaccessible"
            )
        return agent.AgentAction(action="read", reason="inspect", path="worker.py", start=1, end=100)

    async def execute(_action: agent.AgentAction) -> str:
        return "worker.py lines 1-100 of 100: same contents"

    loop._next_action = next_action  # type: ignore[method-assign]
    loop._execute = execute  # type: ignore[method-assign]
    result = await loop.answer("Why did my input change?")
    assert result.status == "unanswered"
    assert "inaccessible" in result.text
    assert result.steps == 4
    assert calls[-1] == {"done", "give_up"}
    assert not any("previous question" in item for item in loop.history)


@pytest.mark.asyncio
async def test_last_step_is_reserved_for_final_answer(loop: agent.AgentLoop) -> None:
    loop.max_steps = 2
    calls: list[set[str]] = []

    async def next_action(
        _question: str, step: int, *, instructions: str, tools: tuple
    ) -> agent.AgentAction:
        names = {item["function"]["name"] for item in tools}
        calls.append(names)
        if names == {"done", "give_up"}:
            return agent.AgentAction(
                action="done", reason="complete", summary="Found symbol in file.py:1"
            )
        return agent.AgentAction(action="search", reason="locate", pattern="symbol")

    async def execute(_action: agent.AgentAction) -> str:
        return "file.py:1: symbol"

    loop._next_action = next_action  # type: ignore[method-assign]
    loop._execute = execute  # type: ignore[method-assign]
    result = await loop.answer("Where is symbol?")
    assert result.status == "answered"
    assert result.steps == 2
    assert calls[-1] == {"done", "give_up"}


@pytest.mark.asyncio
async def test_invalid_final_action_is_not_executed(loop: agent.AgentLoop) -> None:
    loop.max_steps = 1
    executed: list[bool] = []

    async def next_action(*_args: object, **_kwargs: object) -> agent.AgentAction:
        return agent.AgentAction(action="run", reason="inspect status", command="git status")

    async def execute(_action: agent.AgentAction) -> str:
        executed.append(True)
        return "unexpected"

    loop._next_action = next_action  # type: ignore[method-assign]
    loop._execute = execute  # type: ignore[method-assign]
    result = await loop.answer("What changed?")
    assert result.status == "unanswered"
    assert "did not produce a final answer" in result.text
    assert executed == []


@pytest.mark.asyncio
async def test_distinct_read_ranges_are_not_treated_as_repeats(
    loop: agent.AgentLoop,
) -> None:
    loop.max_steps = 5
    calls: list[set[str]] = []

    async def next_action(
        _question: str, step: int, *, instructions: str, tools: tuple
    ) -> agent.AgentAction:
        calls.append({item["function"]["name"] for item in tools})
        if step == 5:
            return agent.AgentAction(
                action="done", reason="complete", summary="Reviewed distinct ranges"
            )
        return agent.AgentAction(
            action="read", reason="inspect range", path="worker.py", start=step * 10, end=step * 10 + 9
        )

    async def execute(action: agent.AgentAction) -> str:
        return f"contents of lines {action.start}-{action.end}"

    loop._next_action = next_action  # type: ignore[method-assign]
    loop._execute = execute  # type: ignore[method-assign]
    result = await loop.answer("Summarize this long file")
    assert result.status == "answered"
    assert result.steps == 5
    assert calls[3] != {"done", "give_up"}
    assert calls[4] == {"done", "give_up"}


@pytest.mark.asyncio
async def test_clears_previous_request_history(loop: agent.AgentLoop) -> None:
    loop.max_steps = 1
    history_on_action: list[tuple[list[str], list[str]]] = []

    async def next_action(*_args: object, **_kwargs: object) -> agent.AgentAction:
        history_on_action.append((list(loop.history), list(loop.compacted)))
        return agent.AgentAction(action="give_up", reason="blocked", summary="No relevant evidence")

    loop._next_action = next_action  # type: ignore[method-assign]
    await loop.answer("New question")
    assert history_on_action == [([], [])]
