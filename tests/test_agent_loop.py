import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from dfm.orchestration.agent import AgentLoop, TaskOutcome
from dfm.orchestration.runtime import Runtime
from tests.helpers import DONE, FIX, ScriptedCoder


@pytest.fixture
def repo(calc_repo: Path) -> Path:
    return calc_repo


async def run_agent(repo: Path, coder: ScriptedCoder, **kw: object) -> tuple[TaskOutcome, Runtime]:
    rt = Runtime.create(repo, request="fix the failing test", mode="implement")
    loop = AgentLoop(rt, coder=coder, **kw)  # type: ignore[arg-type]
    outcome = await loop.run("fix the failing test")
    await rt.close(outcome.session_status, outcome.summary)
    return outcome, rt


def event_types(rt: Runtime) -> list[str]:
    return [e["type"] for e in rt.store.load(rt.session.id).events]


async def test_fixes_failing_test_with_evidence(
    home: Path, repo: Path, git: Callable[..., str]
) -> None:
    coder = ScriptedCoder(
        {"action": "read", "reason": "look", "path": "src/calc/ops.py"}, FIX, DONE
    )
    outcome, rt = await run_agent(repo, coder)
    assert outcome.status == "completed", outcome.report()
    assert outcome.changed == ["src/calc/ops.py"]
    assert (repo / "src/calc/ops.py").read_text() == "def add(a, b):\n    return a + b\n"
    claims = {e.claim: e for e in outcome.verified}
    assert "tests pass" in claims and claims["tests pass"].exit_code == 0
    # the first prompt carried the parsed failure and the repository facts
    assert "test_add" in coder.prompts[0] and "assert -1 == 5" in coder.prompts[0]
    types = event_types(rt)
    for expected in (
        "user_request",
        "test_started",
        "test_finished",
        "model_started",
        "patch_finished",
        "verification",
        "task_completed",
    ):
        assert expected in types, expected
    report = outcome.report()
    assert (
        report.startswith("Status: completed") and "Verified" in report and "tests pass" in report
    )
    assert "Architecture" in report


async def test_done_without_fix_is_not_reported_as_verified(home: Path, repo: Path) -> None:
    outcome, _ = await run_agent(repo, ScriptedCoder(DONE))
    assert outcome.status == "failed"
    assert "verification failed" in outcome.summary
    assert not any(e.claim == "tests pass" for e in outcome.verified)
    assert any(item.startswith("tests:") for item in outcome.not_verified)
    assert outcome.changed == []


async def test_refused_command_is_an_observation(home: Path, repo: Path) -> None:
    coder = ScriptedCoder(
        {"action": "run", "reason": "clean up", "command": "rm -rf src"}, FIX, DONE
    )
    outcome, _ = await run_agent(repo, coder)
    assert (repo / "src").exists()
    assert "refused by policy" in coder.prompts[1]
    assert outcome.status == "completed"


async def test_per_step_text_is_the_last_message(home: Path, repo: Path) -> None:
    # the MLX worker caches everything before the last message: keep the changing part there
    coder = ScriptedCoder(
        {"action": "read", "reason": "look", "path": "src/calc/ops.py"}, FIX, DONE
    )
    await run_agent(repo, coder)
    first, second = coder.requests[0].messages, coder.requests[1].messages
    assert "This is step 1 of " in first[-1].content
    assert first[-1].content.endswith("Choose the next action.")
    assert "assert -1 == 5" in first[-1].content  # the failing test: current state, not context
    assert "assert -1 == 5" not in first[0].content
    assert "This is step 2 of" in second[-1].content
    assert first[0].content == second[0].content  # task and context: cached across steps
    assert second[1].content.startswith(first[1].content)  # history only grows (until it slides)
    assert "Step 1: read" in second[1].content


async def test_invalid_search_regex_is_an_observation(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # gpt-oss-20b sent an unbalanced regex once; it crashed the whole benchmark run
    monkeypatch.setattr("dfm.tools.filesystem.tools._real_rg", lambda: None)
    coder = ScriptedCoder({"action": "search", "reason": "find", "pattern": "add(a"}, FIX, DONE)
    outcome, _ = await run_agent(repo, coder)
    assert "invalid regex 'add(a'" in coder.prompts[1]
    assert outcome.status == "completed"


async def test_patch_conflict_feeds_back_current_text(home: Path, repo: Path) -> None:
    wrong = {**FIX, "search": "return a minus b"}
    coder = ScriptedCoder(wrong, FIX, DONE)
    outcome, _ = await run_agent(repo, coder)
    assert "patch not applied" in coder.prompts[1]
    assert "return a - b" in coder.prompts[1]  # closest current text shown
    assert outcome.status == "completed"


async def test_no_progress_stops(home: Path, repo: Path) -> None:
    breaks = [
        {
            "action": "patch",
            "reason": "try",
            "path": "src/calc/ops.py",
            "search": "return a - b",
            "replace": "return a - b  # 1",
        },
        {
            "action": "patch",
            "reason": "try",
            "path": "src/calc/ops.py",
            "search": "return a - b  # 1",
            "replace": "return a - b  # 2",
        },
        {
            "action": "patch",
            "reason": "try",
            "path": "src/calc/ops.py",
            "search": "return a - b  # 2",
            "replace": "return a - b  # 3",
        },
        DONE,
    ]
    coder = ScriptedCoder(*breaks)
    outcome, _ = await run_agent(repo, coder)
    assert outcome.status == "failed"
    assert "same failure persisted" in outcome.summary
    assert any("different approach" in p for p in coder.prompts)


async def test_user_work_preserved(home: Path, repo: Path) -> None:
    (repo / "README.md").write_text("my unfinished notes\n")
    overwrite = {
        "action": "patch",
        "reason": "tidy",
        "path": "README.md",
        "search": "my unfinished notes",
        "replace": "x",
    }
    coder = ScriptedCoder(overwrite, FIX, DONE)
    outcome, _ = await run_agent(repo, coder)
    assert (repo / "README.md").read_text() == "my unfinished notes\n"
    assert "uncommitted changes" in coder.prompts[1]
    assert outcome.status == "completed" and outcome.risks == []


async def test_malformed_model_output_fails_cleanly(home: Path, repo: Path) -> None:
    outcome, rt = await run_agent(repo, ScriptedCoder("I think we should fix it", "still prose"))
    assert outcome.status == "failed"
    assert "coding model failed" in outcome.summary
    assert "failure" in event_types(rt)


async def test_without_a_coder(home: Path, repo: Path) -> None:
    rt = Runtime.create(repo, request="x", mode="implement")
    outcome = await AgentLoop(rt).run("fix the failing test")
    await rt.close("failed")
    assert outcome.status == "failed" and "dfm setup" in outcome.summary


async def test_merge_in_progress_blocks(home: Path, repo: Path, git: Callable[..., str]) -> None:
    git(repo, "checkout", "-q", "-b", "other")
    (repo / "src/calc/ops.py").write_text("def add(a, b):\n    return 0\n")
    git(repo, "commit", "-qam", "other")
    git(repo, "checkout", "-q", "main")
    (repo / "src/calc/ops.py").write_text("def add(a, b):\n    return 1\n")
    git(repo, "commit", "-qam", "main")
    subprocess.run(
        ["git", "-C", str(repo), "merge", "-q", "other"], capture_output=True, check=False
    )
    outcome, _ = await run_agent(repo, ScriptedCoder(FIX))
    assert outcome.status == "blocked" and "merge in progress" in outcome.summary


async def test_a_failed_check_is_not_reported_as_completed(home: Path, repo: Path) -> None:
    broken = {
        "action": "create",
        "reason": "helper",
        "path": "src/calc/broken.py",
        "content": "def (:\n",
    }
    coder = ScriptedCoder(FIX, broken, DONE)
    outcome, _ = await run_agent(repo, coder)
    assert outcome.status == "failed"
    assert "type check / compile failed" in outcome.summary
    assert any(e.claim == "tests pass" for e in outcome.verified)  # the tests did pass


async def test_requests_carry_tools_reasoning_and_a_thinking_budget(home: Path, repo: Path) -> None:
    conflict = {
        "action": "patch",
        "reason": "x",
        "path": "src/calc/ops.py",
        "search": "nope",
        "replace": "y",
    }
    coder = ScriptedCoder(conflict, FIX, DONE)
    await run_agent(repo, coder)
    first, after_conflict = coder.requests[0], coder.requests[1]
    assert first.tools and {t["function"]["name"] for t in first.tools} >= {"read", "patch", "done"}
    assert (first.reasoning, first.thinking_budget) == ("medium", 1536)
    assert after_conflict.reasoning == "high"  # the patch did not apply: think harder


async def test_the_token_budget_stops_a_task(home: Path, repo: Path) -> None:
    from dfm.models.base import GenerateRequest, GenerateResult

    class Verbose(ScriptedCoder):
        async def generate(self, request: GenerateRequest) -> GenerateResult:
            result = await super().generate(request)
            return GenerateResult(result.text, self.name, 1, tokens_out=30_000)

    look = {"action": "read", "reason": "look", "path": "src/calc/ops.py"}
    outcome, _ = await run_agent(repo, Verbose(look, look, look, FIX, DONE))
    assert outcome.status == "failed" and "token budget used up" in outcome.summary
    assert outcome.steps <= 3  # 2 steps x 30k output tokens pass the 50k default


def test_compaction_keeps_one_line_per_old_step() -> None:
    from dfm.orchestration.agent import HISTORY_CHARS, HISTORY_KEEP_CHARS, AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.history = [f"Step {i}: read (look)\n" + "x" * 2000 for i in range(1, 12)]
    loop.compacted = []
    loop._compact_history()  # pyright: ignore[reportPrivateUsage]
    assert sum(len(h) for h in loop.history) <= HISTORY_KEEP_CHARS < HISTORY_CHARS
    assert loop.compacted[0] == "- Step 1: read (look)"  # the gist stays, the body goes
    assert len(loop.compacted) + len(loop.history) == 11
    before = list(loop.history)
    loop._compact_history()  # pyright: ignore[reportPrivateUsage]
    assert loop.history == before  # under the limit: nothing moves, the prefix stays cached
