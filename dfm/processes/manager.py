"""Run processes with streaming output, timeouts, cancellation and ownership.

Every process starts in its own session (new process group), so a timeout or
stop signals the whole group: `pnpm dev` and the node it spawned go together.
Signals escalate SIGINT -> SIGTERM -> SIGKILL with grace periods.

Background processes are recorded in an ownership registry on disk
(`<data>/processes/<id>.json`) with their start time, so DFM only ever signals
processes it started, survives pid reuse, and finds its own orphans after a crash.
The manager does not check policy: callers classify and authorize first.
"""

import asyncio
import contextlib
import ctypes
import json
import os
import pty
import re
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from dfm.execution import ExecutionEnvironment
from dfm.secrets import redact_argv


class Stream(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"


OutputCallback = Callable[[Stream, str], None]


class NotOwned(PermissionError):
    """DFM refuses to signal a process it did not start."""


class OutputBuffer:
    """Keeps the first quarter and the last three quarters of `limit` bytes."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._head: list[str] = []
        self._head_bytes = 0
        self._tail: list[str] = []
        self._tail_bytes = 0
        self.dropped = 0

    def add(self, chunk: str) -> None:
        size = len(chunk.encode(errors="replace"))
        if self._head_bytes + size <= self._limit // 4:
            self._head.append(chunk)
            self._head_bytes += size
            return
        self._tail.append(chunk)
        self._tail_bytes += size
        while self._tail_bytes > self._limit - self._limit // 4 and len(self._tail) > 1:
            removed = self._tail.pop(0)
            removed_size = len(removed.encode(errors="replace"))
            self._tail_bytes -= removed_size
            self.dropped += removed_size

    @property
    def truncated(self) -> bool:
        return self.dropped > 0

    def text(self) -> str:
        middle = f"\n[... {self.dropped} bytes omitted ...]\n" if self.dropped else ""
        return "".join(self._head) + middle + "".join(self._tail)


@dataclass(frozen=True)
class Escalation:
    int_grace_s: float = 2.0
    term_grace_s: float = 3.0


@dataclass(frozen=True)
class ProcessResult:
    argv: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


# ---------------------------------------------------------------- ownership registry


class _BsdInfo(ctypes.Structure):
    """`struct proc_bsdinfo` (sys/proc_info.h): only the start time is read."""

    _fields_ = [
        ("ids", ctypes.c_uint32 * 12),
        ("comm", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        ("more", ctypes.c_uint32 * 5),
        ("nice", ctypes.c_int32),
        ("start_sec", ctypes.c_uint64),
        ("start_usec", ctypes.c_uint64),
    ]


_PROC_PIDTBSDINFO = 3


def _libproc_start_time(pid: int) -> str | None:
    """The kernel's start time, formatted as `ps -o lstart=` prints it.

    `/bin/ps` is setuid, and sandbox-exec refuses to run setuid binaries: inside DFM's sandbox
    it fails, so ask the kernel directly (works for the user's own processes).
    """
    if sys.platform != "darwin":
        return None
    try:
        lib = ctypes.CDLL("/usr/lib/libproc.dylib")
    except OSError:
        return None
    info = _BsdInfo()
    size = lib.proc_pidinfo(
        pid, _PROC_PIDTBSDINFO, ctypes.c_uint64(0), ctypes.byref(info), ctypes.sizeof(info)
    )
    if size != ctypes.sizeof(info) or not info.start_sec:
        return None
    return time.strftime("%a %b %e %H:%M:%S %Y", time.localtime(info.start_sec))


def process_start_time(pid: int) -> str | None:
    """Start time as `ps -o lstart=` reports it; changes if the pid is reused."""
    found = _libproc_start_time(pid)
    if found is not None:
        return found
    try:
        out = subprocess.run(  # noqa: S603
            ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = out.stdout.strip()
    return value or None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass(frozen=True)
class ProcessRecord:
    id: str
    name: str
    pid: int
    pgid: int
    started: str  # ps lstart, guards against pid reuse
    argv: list[str]  # redacted
    cwd: str
    session_id: str
    owner_pid: int  # the DFM process that started it
    created_at: float
    ports: list[int] = field(default_factory=lambda: [])


class ProcessRegistry:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, record_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", record_id):
            raise ValueError(f"invalid process id {record_id!r}")
        return self.directory / f"{record_id}.json"

    def add(self, record: ProcessRecord) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self._path(record.id).with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(record)))
        tmp.replace(self._path(record.id))

    def remove(self, record_id: str) -> None:
        self._path(record_id).unlink(missing_ok=True)

    def get(self, record_id: str) -> ProcessRecord | None:
        path = self._path(record_id)
        if not path.exists():
            return None
        return self._load(path)

    def records(self) -> list[ProcessRecord]:
        if not self.directory.exists():
            return []
        records = [self._load(p) for p in sorted(self.directory.glob("*.json"))]
        return [r for r in records if r is not None]

    @staticmethod
    def _load(path: Path) -> ProcessRecord | None:
        try:
            data: dict[str, Any] = json.loads(path.read_text())
            return ProcessRecord(**data)
        except (OSError, ValueError, TypeError):
            return None

    def is_live(self, record: ProcessRecord) -> bool:
        """Same pid, same start time: still the process DFM started."""
        return pid_alive(record.pid) and process_start_time(record.pid) == record.started

    def orphans(self) -> list[ProcessRecord]:
        """Live processes whose owning DFM process is gone."""
        return [r for r in self.records() if not pid_alive(r.owner_pid) and self.is_live(r)]

    def prune(self) -> list[str]:
        """Forget records of processes that have exited."""
        gone = [r.id for r in self.records() if not self.is_live(r) and not group_alive(r.pgid)]
        for record_id in gone:
            self.remove(record_id)
        return gone


# ---------------------------------------------------------------- readiness / ports


def listening_ports(pgid: int) -> list[int]:
    """TCP ports in LISTEN state held by any process in the group (via lsof)."""
    try:
        out = subprocess.run(  # noqa: S603
            ["/usr/sbin/lsof", "-nP", "-a", "-g", str(pgid), "-iTCP", "-sTCP:LISTEN", "-Fn"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    ports = {int(m.group(1)) for m in re.finditer(r"^n.*:(\d+)$", out.stdout, re.M)}
    return sorted(ports)


ReadinessCheck = Callable[["ManagedProcess"], Awaitable[bool]]


def port_ready(port: int | None = None) -> ReadinessCheck:
    """Ready when the group listens on `port` (or on any port if None)."""

    async def check(proc: "ManagedProcess") -> bool:
        ports = await asyncio.to_thread(listening_ports, proc.pgid)
        return bool(ports) if port is None else port in ports

    return check


def output_ready(pattern: str) -> ReadinessCheck:
    regex = re.compile(pattern)

    async def check(proc: "ManagedProcess") -> bool:
        return regex.search(proc.output.text()) is not None

    return check


# ---------------------------------------------------------------- manager


@dataclass
class ManagedProcess:
    id: str
    name: str
    argv: tuple[str, ...]
    pid: int
    pgid: int
    output: OutputBuffer
    process: asyncio.subprocess.Process
    readers: list[asyncio.Task[None]]
    ports: list[int] = field(default_factory=lambda: [])

    @property
    def running(self) -> bool:
        return self.process.returncode is None


class ProcessManager:
    def __init__(
        self,
        registry: ProcessRegistry,
        *,
        session_id: str = "",
        escalation: Escalation | None = None,
    ) -> None:
        self.registry = registry
        self.session_id = session_id
        self.escalation = escalation or Escalation()
        self._background: dict[str, ManagedProcess] = {}

    async def run(
        self,
        cmd: Sequence[str],
        env: ExecutionEnvironment,
        *,
        timeout: float | None = None,
        pty: bool = False,
        stdin: str | None = None,
        on_output: OutputCallback | None = None,
        secret_env: Sequence[str] = (),
        cwd: Path | None = None,
    ) -> ProcessResult:
        argv = tuple(cmd)
        timeout = timeout if timeout is not None else env.limits.wall_time_s
        out = OutputBuffer(env.limits.output_bytes)
        err = OutputBuffer(env.limits.output_bytes)
        started = time.monotonic()
        workdir = cwd or env.cwd
        if pty:
            proc, readers = await self._spawn_pty(argv, env, workdir, secret_env, out, on_output)
        else:
            out_r, out_w = os.pipe()
            err_r, err_w = os.pipe()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=workdir,
                    env=env.child_env(secret_env),
                    stdin=asyncio.subprocess.PIPE
                    if stdin is not None
                    else asyncio.subprocess.DEVNULL,
                    stdout=out_w,
                    stderr=err_w,
                    start_new_session=True,
                )
            except BaseException:
                os.close(out_r)
                os.close(err_r)
                raise
            finally:
                os.close(out_w)
                os.close(err_w)
            readers = [
                await _read_fd(out_r, Stream.STDOUT, out, on_output),
                await _read_fd(err_r, Stream.STDERR, err, on_output),
            ]
            if stdin is not None and proc.stdin is not None:
                proc.stdin.write(stdin.encode())
                await proc.stdin.drain()
                proc.stdin.close()

        timed_out = cancelled = False
        try:
            await asyncio.wait_for(proc.wait(), timeout)
            if group_alive(proc.pid):
                # Foreground commands may not leave children behind; servers use start_background.
                await self._terminate_group(proc)
        except TimeoutError:
            timed_out = True
            await self._terminate_group(proc)
        except asyncio.CancelledError:
            cancelled = True
            await asyncio.shield(self._terminate_group(proc))
            raise
        finally:
            if not (cancelled or timed_out):
                # a descendant that left the process group can hold the pipe open forever
                _, still_open = await asyncio.wait(readers, timeout=READER_DRAIN_S)
                if still_open:
                    err.add(
                        f"\n[DFM: stopped reading output {READER_DRAIN_S:.0f} s after the "
                        "command exited: a background process still holds it open]\n"
                    )
            for task in readers:
                if not task.done():
                    task.cancel()
            with contextlib.suppress(asyncio.CancelledError, OSError):
                await asyncio.gather(*readers, return_exceptions=True)

        return ProcessResult(
            argv=argv,
            exit_code=proc.returncode,
            stdout=out.text(),
            stderr=err.text(),
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=timed_out,
            cancelled=cancelled,
            truncated=out.truncated or err.truncated,
        )

    async def _spawn_pty(
        self,
        argv: tuple[str, ...],
        env: ExecutionEnvironment,
        workdir: Path,
        secret_env: Sequence[str],
        out: OutputBuffer,
        on_output: OutputCallback | None,
    ) -> tuple[asyncio.subprocess.Process, list[asyncio.Task[None]]]:
        master, slave = pty.openpty()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=workdir,
                env=env.child_env(secret_env),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
            )
        finally:
            os.close(slave)
        return proc, [await _read_fd(master, Stream.STDOUT, out, on_output)]

    async def _terminate_group(self, proc: asyncio.subprocess.Process) -> None:
        pgid = proc.pid
        await terminate_group(
            pgid, self.escalation, lambda: proc.returncode is not None and not group_alive(pgid)
        )
        with contextlib.suppress(ProcessLookupError):
            await asyncio.wait_for(proc.wait(), 5)

    async def start_background(
        self,
        cmd: Sequence[str],
        env: ExecutionEnvironment,
        *,
        name: str,
        ready: ReadinessCheck | None = None,
        ready_timeout: float = 30.0,
        secret_env: Sequence[str] = (),
    ) -> ManagedProcess:
        argv = tuple(cmd)
        buffer = OutputBuffer(env.limits.output_bytes)
        out_r, out_w = os.pipe()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=env.cwd,
                env=env.child_env(secret_env),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=out_w,
                stderr=out_w,
                start_new_session=True,
            )
        except BaseException:
            os.close(out_r)
            raise
        finally:
            os.close(out_w)
        handle = ManagedProcess(
            id=uuid.uuid4().hex[:12],
            name=name,
            argv=argv,
            pid=proc.pid,
            pgid=proc.pid,
            output=buffer,
            process=proc,
            readers=[await _read_fd(out_r, Stream.STDOUT, buffer, None)],
        )
        started = await asyncio.to_thread(process_start_time, proc.pid) or ""
        self.registry.add(
            ProcessRecord(
                id=handle.id,
                name=name,
                pid=proc.pid,
                pgid=proc.pid,
                started=started,
                argv=redact_argv(list(argv)),
                cwd=str(env.cwd),
                session_id=self.session_id,
                owner_pid=os.getpid(),
                created_at=time.time(),
            )
        )
        self._background[handle.id] = handle
        if ready is not None:
            deadline = time.monotonic() + ready_timeout
            while not await ready(handle):
                if not handle.running:
                    raise RuntimeError(f"{name} exited before becoming ready:\n{buffer.text()}")
                if time.monotonic() > deadline:
                    await self.stop(handle)
                    raise TimeoutError(f"{name} not ready after {ready_timeout}s")
                await asyncio.sleep(0.1)
        handle.ports = await asyncio.to_thread(listening_ports, handle.pgid)
        return handle

    async def stop(self, handle: ManagedProcess | str) -> None:
        """Stop a DFM-owned background process (and its group). Anything else: NotOwned."""
        record_id = handle if isinstance(handle, str) else handle.id
        record = self.registry.get(record_id)
        managed = self._background.pop(record_id, None)
        if record is None and managed is None:
            raise NotOwned(f"no DFM-owned process {record_id}")
        if managed is not None:
            await self._terminate_group(managed.process)
            for task in managed.readers:
                task.cancel()
        elif record is not None and await asyncio.to_thread(self.registry.is_live, record):
            await terminate_group(
                record.pgid, self.escalation, lambda: not group_alive(record.pgid)
            )
        self.registry.remove(record_id)

    async def stop_all(self) -> None:
        for record_id in list(self._background):
            await self.stop(record_id)


async def terminate_group(pgid: int, escalation: Escalation, done: Callable[[], bool]) -> None:
    for sig, grace in (
        (signal.SIGINT, escalation.int_grace_s),
        (signal.SIGTERM, escalation.term_grace_s),
        (signal.SIGKILL, 0.0),
    ):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        except PermissionError:
            return  # macOS: only zombies left (or not ours) - nothing more to signal
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if done():
                return
            await asyncio.sleep(0.05)


async def _read_fd(
    fd: int, stream: Stream, buffer: OutputBuffer, on_output: OutputCallback | None
) -> asyncio.Task[None]:
    """Pump the read end of a pipe (or pty) DFM owns. Pipes the subprocess transport owned
    would keep `Process.wait()` waiting on Python 3.12 until every holder closed them, so a
    daemon the command started would hang the run until its timeout."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(fd, "rb", 0)
    )

    async def pump() -> None:
        try:
            await _pump(reader, stream, buffer, on_output)
        finally:
            transport.close()

    return asyncio.create_task(pump())


_PUMP_CHUNK = 64 * 1024
READER_DRAIN_S = 5.0


async def _pump(
    reader: asyncio.StreamReader,
    stream: Stream,
    buffer: OutputBuffer,
    on_output: OutputCallback | None,
) -> None:
    """Forward output line by line. Reads in chunks: a line longer than the stream limit
    (minified JS, one JSON blob) must not stop the reader, or the pipe fills and the command
    hangs until its timeout."""

    def emit(data: bytes) -> None:
        text = data.decode(errors="replace")
        buffer.add(text)
        if on_output is not None:
            on_output(stream, text)

    pending = b""
    while True:
        try:
            chunk = await reader.read(_PUMP_CHUNK)
        except OSError:  # EIO from a closed pty
            break
        if not chunk:
            break
        pending += chunk
        lines = pending.splitlines(keepends=True)
        pending = lines.pop() if lines and not lines[-1].endswith((b"\n", b"\r")) else b""
        for line in lines:
            emit(line)
        if len(pending) >= _PUMP_CHUNK:  # an over-long line: pass it on as it is
            emit(pending)
            pending = b""
    if pending:
        emit(pending)
