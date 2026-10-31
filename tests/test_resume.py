import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dfm.cli.app import app
from dfm.orchestration.ask import ask
from dfm.orchestration.resume import resume
from dfm.orchestration.runtime import Runtime
from dfm.processes import ProcessRegistry, process_start_time
from dfm.processes.manager import ProcessRecord, pid_alive
from dfm.recovery import Checkpointer, Stage
from dfm.recovery.reconcile import RepoChange, reconcile
from dfm.routing import is_resume_request
from dfm.tools.errors import ToolError
from tests.helpers import DONE, ScriptedCoder

WIP = "def add(a, b):\n    return a - b  # wip\n"
FINISH = {
    "action": "patch",
    "reason": "finish",
    "path": "src/calc/ops.py",
    "search": "return a - b  # wip",
    "replace": "return a + b",
}


def dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def crash_session(repo: Path) -> tuple[str, Checkpointer]:
    """A session that edited a file, checkpointed, and died without finishing."""
    rt = Runtime.create(repo, request="fix the failing test", mode="implement")
    checkpointer = Checkpointer(repo, rt.session.id)
    checkpointer.record_original("src/calc/ops.py")
    (repo / "src/calc/ops.py").write_text(WIP)
    checkpointer.save(Stage.EDIT, pending=["finish the fix", "run tests"])
    lines = rt.session.path.read_text().splitlines()
    header = json.loads(lines[0])
    header["dfm_pid"] = dead_pid()
    rt.session.path.write_text("\n".join([json.dumps(header), *lines[1:]]) + "\n")
    rt.env.close()  # the process "died": no task_completed was written
    return rt.session.id, checkpointer


def test_reconcile_states(calc_repo: Path, git: Callable[..., str]) -> None:
    (calc_repo / "README.md").write_text("user notes\n")
    cp = Checkpointer(calc_repo, "s1")
    cp.record_original("src/calc/ops.py")
    (calc_repo / "src/calc/ops.py").write_text(WIP)
    checkpoint = cp.save(Stage.EDIT) and cp.latest()
    assert checkpoint is not None
    assert reconcile(checkpoint, calc_repo).state is RepoChange.UNCHANGED

    (calc_repo / "notes.txt").write_text("new\n")
    external = reconcile(checkpoint, calc_repo)
    assert external.state is RepoChange.DFM_EDITS_INTACT and external.intact == ["src/calc/ops.py"]
    assert external.external_changes == ["notes.txt"]

    (calc_repo / "src/calc/ops.py").write_text("def add(a, b):\n    return 42\n")
    user = reconcile(checkpoint, calc_repo)
    assert user.state is RepoChange.USER_CHANGED_DFM_FILES and user.changed_by_user == [
        "src/calc/ops.py"
    ]
    assert "yours now" in user.report()

    git(calc_repo, "add", "README.md", "notes.txt")
    git(calc_repo, "commit", "-qm", "user commit")
    assert reconcile(checkpoint, calc_repo).state is RepoChange.CONFLICTING
    (calc_repo / "src/calc/ops.py").write_text(WIP)
    moved = reconcile(checkpoint, calc_repo)
    assert moved.state is RepoChange.HEAD_MOVED and "HEAD moved" in moved.report()
    (calc_repo / "src/calc/ops.py").unlink()
    assert reconcile(checkpoint, calc_repo).deleted == ["src/calc/ops.py"]


async def test_resume_continues_the_interrupted_session(home: Path, calc_repo: Path) -> None:
    old_id, _ = crash_session(calc_repo)
    rt = Runtime.create(calc_repo, request="resume", mode="resume")
    result = await resume(rt, coder=ScriptedCoder(FINISH, DONE))
    assert result.outcome is not None
    await rt.close(result.outcome.session_status)
    assert result.previous.id == old_id
    assert "Remaining: finish the fix, run tests" in result.summary
    assert result.reconciliation is not None and result.reconciliation.intact == ["src/calc/ops.py"]
    assert result.outcome.status == "completed", result.outcome.report()
    assert (calc_repo / "src/calc/ops.py").read_text() == "def add(a, b):\n    return a + b\n"
    old = rt.store.load(old_id)
    assert old.status == "interrupted"
    with pytest.raises(ToolError, match="no interrupted session"):
        await resume(Runtime.create(calc_repo, request="resume", mode="resume"))


async def test_user_changes_win_on_resume(home: Path, calc_repo: Path) -> None:
    crash_session(calc_repo)
    (calc_repo / "src/calc/ops.py").write_text(
        "def add(a, b):\n    return a - b  # my own attempt\n"
    )
    rt = Runtime.create(calc_repo, request="resume", mode="resume")
    overwrite = {
        "action": "patch",
        "reason": "fix",
        "path": "src/calc/ops.py",
        "search": "return a - b  # my own attempt",
        "replace": "return a + b",
    }
    result = await resume(rt, coder=ScriptedCoder(overwrite, DONE), max_steps=2)
    assert result.outcome is not None
    await rt.close(result.outcome.session_status)
    assert result.reconciliation is not None and result.reconciliation.changed_by_user == [
        "src/calc/ops.py"
    ]
    assert "my own attempt" in (calc_repo / "src/calc/ops.py").read_text()
    assert result.outcome.status == "failed"


async def test_live_session_is_not_taken_over(home: Path, calc_repo: Path) -> None:
    running = Runtime.create(calc_repo, request="fix the failing test", mode="implement")
    rt = Runtime.create(calc_repo, request="resume", mode="resume")
    with pytest.raises(ToolError, match="still running"):
        await resume(rt, running.session.id)
    await rt.close("aborted")
    await running.close("completed")


async def test_orphans_are_stopped(home: Path, calc_repo: Path) -> None:
    old_id, _ = crash_session(calc_repo)
    rt = Runtime.create(calc_repo, request="resume", mode="resume")
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    registry: ProcessRegistry = rt.pm.registry
    registry.add(
        ProcessRecord(
            "orph1",
            "dev server",
            proc.pid,
            proc.pid,
            process_start_time(proc.pid) or "",
            ["sleep"],
            "/",
            old_id,
            dead_pid(),
            0.0,
        )
    )
    try:
        result = await resume(rt, coder=ScriptedCoder(FINISH, DONE))
        assert result.reconciliation is not None and [
            o.id for o in result.reconciliation.orphans
        ] == ["orph1"]
        proc.wait(timeout=10)
        assert not pid_alive(proc.pid) or proc.returncode is not None
        assert result.outcome is not None
        await rt.close(result.outcome.session_status)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert os.environ["HOME"] == str(home)


CRASHING_DFM = """
import asyncio, sys
from pathlib import Path
from dfm.orchestration.runtime import Runtime
from dfm.recovery import Checkpointer, Stage
from dfm.tools.git import WorktreeManager

async def main() -> None:
    repo = Path(sys.argv[1])
    rt = Runtime.create(repo, request="fix the failing test", mode="implement")
    cp = Checkpointer(repo, rt.session.id)
    cp.record_original("src/calc/ops.py")
    (repo / "src/calc/ops.py").write_text(sys.argv[2])
    cp.save(Stage.EDIT, pending=["finish the fix"])
    proc = await rt.pm.start_background(["sleep", "60"], rt.env, name="watcher")
    wt = await WorktreeManager(repo, rt.dirs.data, rt.policy, session_id=rt.session.id).create()
    (wt.path / "scratch.txt").write_text("half-done idea\\n")
    print(rt.session.id, proc.pid, wt.path, flush=True)
    await asyncio.sleep(120)

asyncio.run(main())
"""


async def test_kill_9_mid_repair_then_resume(home: Path, calc_repo: Path) -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", CRASHING_DFM, str(calc_repo), WIP],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "HOME": str(home)},
    )
    assert child.stdout is not None
    old_id, sleep_pid, wt_path = child.stdout.readline().split()
    child.kill()  # SIGKILL: no cleanup, no task_completed, no worktree removal
    child.wait()
    assert pid_alive(int(sleep_pid)) and Path(wt_path).exists()

    rt = Runtime.create(calc_repo, request="resume", mode="resume")
    try:
        result = await resume(rt, coder=ScriptedCoder(FINISH, DONE))
        assert result.outcome is not None
        await rt.close(result.outcome.session_status)
    finally:
        if pid_alive(int(sleep_pid)):
            os.kill(int(sleep_pid), 9)
    assert result.previous.id == old_id
    assert result.reconciliation is not None
    assert result.reconciliation.intact == ["src/calc/ops.py"]
    assert [o.pid for o in result.reconciliation.orphans] == [int(sleep_pid)]
    assert not pid_alive(int(sleep_pid))
    assert not Path(wt_path).exists()
    (patch,) = result.recovered_patches
    assert "half-done idea" in patch.read_text()
    assert result.outcome.status == "completed", result.outcome.report()


def test_resume_requests_are_recognised() -> None:
    for text in ("resume", "resume what we were doing yesterday", "What were we doing?"):
        assert is_resume_request(text), text
    assert not is_resume_request("add a resume parser to the CV module")


async def test_ask_what_were_we_doing_is_read_only(home: Path, calc_repo: Path) -> None:
    old_id, _ = crash_session(calc_repo)
    rt = Runtime.create(calc_repo, request="what were we doing", mode="ask")
    answer = await ask(rt, "what were we doing yesterday?")
    await rt.close("completed")
    assert f"dfm resume {old_id}" in answer.text and "Remaining: finish the fix" in answer.text
    assert rt.store.load(old_id).status == "running"  # asking does not take the session over


def test_cli_implement_resume_request_goes_to_resume(home: Path, calc_repo: Path) -> None:
    crash_session(calc_repo)
    result = CliRunner().invoke(
        app, ["implement", "resume what we were doing yesterday", "--repo", str(calc_repo)]
    )
    assert "Resuming" in result.output and "Remaining: finish the fix" in result.output
    result = CliRunner().invoke(app, ["resume", "--repo", str(calc_repo)])
    assert result.exit_code == 2 and "no interrupted session" in result.output
