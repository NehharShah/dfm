import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dfm.config import DFMConfig, state_dirs
from dfm.execution import ExecutionEnvironment, ResourceLimits
from dfm.processes import (
    Escalation,
    NotOwned,
    OutputBuffer,
    ProcessManager,
    ProcessRecord,
    ProcessRegistry,
    Stream,
    port_ready,
    process_start_time,
)
from dfm.processes.manager import pid_alive

PY = sys.executable
FAST = Escalation(int_grace_s=0.3, term_grace_s=0.3)


@pytest.fixture
def env(tmp_path: Path) -> ExecutionEnvironment:
    repo = tmp_path / "repo"
    repo.mkdir()
    environ = {"HOME": str(tmp_path / "home"), "PATH": os.environ["PATH"], "SECRET_TOKEN": "s3cr3t"}
    return ExecutionEnvironment.create(
        repo,
        DFMConfig(),
        session_id="t",
        environ=environ,
        dirs=state_dirs(environ),
        limits=ResourceLimits(wall_time_s=30, output_bytes=4096),
    )


@pytest.fixture
def pm(env: ExecutionEnvironment) -> ProcessManager:
    return ProcessManager(ProcessRegistry(env.tmp / "processes"), session_id="t", escalation=FAST)


async def test_run_captures_output(pm: ProcessManager, env: ExecutionEnvironment) -> None:
    result = await pm.run(
        [PY, "-c", "import sys; print('out'); print('err', file=sys.stderr)"], env
    )
    assert result.ok
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"


async def test_streaming_callback(pm: ProcessManager, env: ExecutionEnvironment) -> None:
    seen: list[tuple[Stream, str]] = []
    code = "import sys\nfor i in range(3):\n    print(i, flush=True)\nprint('e', file=sys.stderr)"
    await pm.run([PY, "-c", code], env, on_output=lambda s, line: seen.append((s, line)))
    assert [line for s, line in seen if s is Stream.STDOUT] == ["0\n", "1\n", "2\n"]
    assert (Stream.STDERR, "e\n") in seen


async def test_cwd_and_scrubbed_env(pm: ProcessManager, env: ExecutionEnvironment) -> None:
    code = "import os; print(os.getcwd()); print(os.environ.get('SECRET_TOKEN'))"
    result = await pm.run([PY, "-c", code], env)
    cwd, token = result.stdout.splitlines()
    assert os.path.realpath(cwd) == str(env.cwd)
    assert token == "None"
    passed = await pm.run([PY, "-c", code], env, secret_env=["SECRET_TOKEN"])
    assert passed.stdout.splitlines()[1] == "s3cr3t"


async def test_exit_code(pm: ProcessManager, env: ExecutionEnvironment) -> None:
    result = await pm.run([PY, "-c", "raise SystemExit(3)"], env)
    assert result.exit_code == 3
    assert not result.ok


async def test_stdin(pm: ProcessManager, env: ExecutionEnvironment) -> None:
    result = await pm.run(
        [PY, "-c", "import sys; print(sys.stdin.read().upper())"], env, stdin="hi"
    )
    assert result.stdout.strip() == "HI"


async def test_timeout_kills_process_group(pm: ProcessManager, env: ExecutionEnvironment) -> None:
    started = time.monotonic()
    result = await pm.run(["sh", "-c", "sleep 30 & sleep 30"], env, timeout=0.5)
    assert result.timed_out
    assert time.monotonic() - started < 5
    await asyncio.sleep(0.1)
    out = subprocess.run(["pgrep", "-f", "sleep 30 & sleep 30"], capture_output=True, text=True)
    assert out.stdout.strip() == ""


async def test_timeout_escalates_past_ignored_sigint(
    pm: ProcessManager, env: ExecutionEnvironment
) -> None:
    code = (
        "import signal, time\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)"
    )
    started = time.monotonic()
    result = await pm.run([PY, "-c", code], env, timeout=0.5)
    assert result.timed_out
    assert result.exit_code is not None and result.exit_code < 0
    assert time.monotonic() - started < 5


async def test_cancellation_kills(pm: ProcessManager, env: ExecutionEnvironment) -> None:
    marker = env.tmp / "pid"
    code = f"import os, time; open({str(marker)!r}, 'w').write(str(os.getpid())); time.sleep(30)"
    task = asyncio.create_task(pm.run([PY, "-c", code], env))
    for _ in range(100):
        if marker.exists() and marker.read_text():
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not pid_alive(int(marker.read_text()))


async def test_stray_children_cleaned_after_exit(
    pm: ProcessManager, env: ExecutionEnvironment
) -> None:
    marker = env.tmp / "child"
    code = f"sleep 30 & echo $! > {marker}"
    result = await pm.run(["sh", "-c", code], env)
    assert result.ok
    child = int(marker.read_text())
    await asyncio.sleep(0.1)
    assert not pid_alive(child)


async def test_output_truncation(pm: ProcessManager, env: ExecutionEnvironment) -> None:
    code = "for i in range(5000):\n    print('line', i)"
    result = await pm.run([PY, "-c", code], env)
    assert result.truncated
    assert "bytes omitted" in result.stdout
    assert result.stdout.startswith("line 0\n")
    assert result.stdout.rstrip().endswith("line 4999")


def test_output_buffer_small() -> None:
    buf = OutputBuffer(100)
    buf.add("a\n")
    assert buf.text() == "a\n"
    assert not buf.truncated


async def test_pty(pm: ProcessManager, env: ExecutionEnvironment) -> None:
    result = await pm.run([PY, "-c", "import sys; print(sys.stdout.isatty())"], env, pty=True)
    assert result.ok
    assert result.stdout.strip() == "True"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_background_server_ready_and_stop(
    pm: ProcessManager, env: ExecutionEnvironment
) -> None:
    port = free_port()
    handle = await pm.start_background(
        [PY, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        env,
        name="http",
        ready=port_ready(port),
        ready_timeout=15,
    )
    assert port in handle.ports
    record = pm.registry.get(handle.id)
    assert record is not None and record.pid == handle.pid
    assert pm.registry.is_live(record)
    await pm.stop(handle)
    assert not pid_alive(handle.pid)
    assert pm.registry.get(handle.id) is None


async def test_stop_refuses_unowned(pm: ProcessManager) -> None:
    with pytest.raises(NotOwned):
        await pm.stop("deadbeef")


def _record(pid: int, started: str, owner: int, rid: str = "abc") -> ProcessRecord:
    return ProcessRecord(rid, "x", pid, pid, started, ["sleep"], "/", "s", owner, time.time())


def test_pid_reuse_guard(tmp_path: Path) -> None:
    registry = ProcessRegistry(tmp_path)
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        assert not registry.is_live(_record(proc.pid, "Thu Jan  1 00:00:00 1970", os.getpid()))
        started = process_start_time(proc.pid) or ""
        assert registry.is_live(_record(proc.pid, started, os.getpid()))
    finally:
        proc.kill()
        proc.wait()


async def test_orphans_found_and_stopped(tmp_path: Path) -> None:
    registry = ProcessRegistry(tmp_path)
    dead_owner = subprocess.Popen(["true"])
    dead_owner.wait()
    orphan = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        started = process_start_time(orphan.pid) or ""
        registry.add(_record(orphan.pid, started, dead_owner.pid, "orph"))
        registry.add(_record(orphan.pid, started, os.getpid(), "mine"))
        assert [r.id for r in registry.orphans()] == ["orph"]
        await ProcessManager(registry, escalation=FAST).stop("orph")
        orphan.wait(timeout=5)
        assert registry.get("orph") is None
        assert registry.prune() == ["mine"]
    finally:
        if orphan.poll() is None:
            orphan.kill()


def test_cli_processes(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from dfm.cli.app import app

    env = {"HOME": str(tmp_path)}
    runner = CliRunner()
    empty = runner.invoke(app, ["processes"], env=env)
    assert empty.exit_code == 0 and "no DFM-owned processes" in empty.stdout
    missing = runner.invoke(app, ["processes", "stop", "nope"], env=env)
    assert missing.exit_code == 1
    registry = ProcessRegistry(state_dirs(env).processes)
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        registry.add(_record(proc.pid, process_start_time(proc.pid) or "", os.getpid(), "cli1"))
        listed = runner.invoke(app, ["processes"], env=env)
        assert "cli1" in listed.stdout and "running" in listed.stdout
        stopped = runner.invoke(app, ["processes", "stop", "cli1"], env=env)
        assert stopped.exit_code == 0, stopped.output
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()


async def test_a_line_longer_than_the_stream_limit_does_not_hang(
    pm: ProcessManager, env: ExecutionEnvironment
) -> None:
    seen: list[str] = []
    code = "import sys; sys.stdout.write('x' * 300000); sys.stdout.write('\\nafter\\n')"
    started = time.monotonic()
    result = await pm.run([PY, "-c", code], env, timeout=20, on_output=lambda s, t: seen.append(t))
    assert result.ok and not result.timed_out and time.monotonic() - started < 10
    joined = "".join(seen)
    assert joined.count("x") == 300000 and joined.endswith("after\n")  # nothing dropped


async def test_a_daemon_holding_stdout_does_not_hang_the_run(
    pm: ProcessManager, env: ExecutionEnvironment
) -> None:
    code = (
        "import subprocess, sys\n"
        "p = subprocess.Popen(['sleep', '30'], start_new_session=True)\n"  # leaves the group
        "print(p.pid, flush=True)\n"
    )
    started = time.monotonic()
    result = await pm.run([PY, "-c", code], env, timeout=25)
    try:
        assert result.exit_code == 0 and time.monotonic() - started < 15
        assert "still holds it open" in result.stderr
    finally:
        os.kill(int(result.stdout.split()[0]), 9)


@pytest.mark.skipif(
    __import__("dfm.security.sandbox", fromlist=["x"]).already_sandboxed(),
    reason="/bin/ps is setuid: a sandbox cannot run it (the reason for the kernel lookup)",
)
def test_start_time_from_the_kernel_matches_ps() -> None:
    from dfm.processes.manager import _libproc_start_time  # pyright: ignore[reportPrivateUsage]

    proc = subprocess.Popen(["sleep", "5"])
    try:
        shown = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-p", str(proc.pid)], capture_output=True, text=True
        ).stdout.strip()
        if sys.platform == "darwin":
            assert _libproc_start_time(proc.pid) == shown  # same string: old records still match
        assert process_start_time(proc.pid) == shown
    finally:
        proc.kill()
        proc.wait()
