import asyncio
import json
import os
import shutil
import socket
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from dfm.config import DFMConfig, state_dirs
from dfm.execution import ExecutionEnvironment
from dfm.orchestration.implement import ImplementOptions, implement
from dfm.orchestration.runtime import Runtime
from dfm.policy import Approval, Policy
from dfm.policy.types import PolicyVerdict, ProposedAction
from dfm.processes import ProcessManager, ProcessRegistry
from dfm.services import ServiceError, ServiceManager, ServiceSpec, detect, port_open
from tests import repo_fixtures as fx
from tests.helpers import DONE, ScriptedCoder, path_with_tools

Git = Callable[..., str]


class Allow:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def ask(self, action: ProposedAction, verdict: PolicyVerdict) -> Approval:
        self.asked.append(action.summary)
        return Approval.ONCE


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def manager(
    repo: Path, tmp_path: Path, approver: Allow | None = None, path: str | None = None
) -> ServiceManager:
    environ = {"HOME": str(tmp_path / "home"), "PATH": path or os.environ["PATH"]}
    env = ExecutionEnvironment.create(
        repo, DFMConfig(), session_id="svc", environ=environ, dirs=state_dirs(environ)
    )
    pm = ProcessManager(ProcessRegistry(env.tmp / "p"), session_id="svc")
    policy = Policy(DFMConfig(), approver) if approver else Policy(DFMConfig())
    return ServiceManager(env, pm, policy, tmp_path / "data")


def test_detection(tmp_path: Path) -> None:
    repo = fx.write(
        tmp_path / "r",
        {
            "docker-compose.yml": (
                "services:\n"
                "  db:\n    image: postgres:16\n    ports: ['55432:5432']\n"
                "  cache:\n    image: redis:7\n"
                "  queue:\n    image: bitnami/kafka:3\n    ports:\n      - target: 9092\n        published: 19092\n"
            ),
            ".env.example": "DATABASE_URL=postgresql://u:p@localhost:55432/app\nREDIS_URL=redis://cache.internal:6379/0\n",
            ".env": "DATABASE_URL=postgresql://admin:secret@localhost:6000/prod\n",
        },
    )
    found = detect(repo, hints=["Start Postgres before integration tests", "use ruff"])
    kinds = [(s.kind, s.port, s.source) for s in found.services]
    assert ("postgres", 55432, "docker-compose.yml") in kinds
    assert ("redis", 6379, "docker-compose.yml") in kinds
    assert ("kafka", 19092, "docker-compose.yml") in kinds
    assert ("postgres", 5432, "reflex practice") in kinds
    assert not any(port == 6000 for _, port, _ in kinds)  # .env (real secrets) is never read
    assert any("cache.internal" in note and "not local" in note for note in found.skipped)
    db = next(s for s in found.services if s.kind == "postgres" and s.port == 55432)
    assert (db.compose_file, db.compose_service) == ("docker-compose.yml", "db")


async def test_existing_service_is_reused_not_owned(tmp_path: Path) -> None:
    port = free_port()
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", port)
    try:
        svc = manager(tmp_path, tmp_path)
        handle = await svc.ensure(ServiceSpec("nats", "nats", port))
        assert handle.owned is False and handle.method == "existing"
        await svc.release(handle)
        assert await port_open("127.0.0.1", port)  # DFM never stops what it did not start
        assert svc.owned() == []
    finally:
        server.close()


@pytest.mark.skipif(shutil.which("pg_isready") is None, reason="pg_isready not installed")
async def test_port_taken_by_something_else(tmp_path: Path) -> None:
    port = free_port()
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", port)
    try:
        with pytest.raises(ServiceError, match="taken by something that is not a healthy postgres"):
            await manager(tmp_path, tmp_path).ensure(ServiceSpec("postgres", "db", port))
    finally:
        server.close()


async def test_remote_hosts_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ServiceError, match="not local"):
        await manager(tmp_path, tmp_path).ensure(
            ServiceSpec("postgres", "db", 5432, host="db.example.com")
        )


async def test_compose_start_ownership_and_release(tmp_path: Path) -> None:
    port = free_port()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        f"#!{sys.executable}\n" + (Path(__file__).parent / "fake_docker.py").read_text()
    )
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    (bin_dir / "fake_docker.json").write_text(
        json.dumps({"port": port, "log": str(tmp_path / "docker.log")})
    )
    repo = fx.write(
        tmp_path / "r",
        {"compose.yaml": f"services:\n  queue:\n    image: nats:2\n    ports: ['{port}:4222']\n"},
    )
    approver = Allow()
    svc = manager(repo, tmp_path, approver, path=f"{bin_dir}:/usr/bin:/bin")
    (spec,) = detect(repo).services
    handle = await svc.ensure(spec)
    assert handle.owned and handle.method == "compose"
    assert any("docker compose" in s and "up" in s for s in approver.asked)  # asked first
    assert [h.id for h in svc.owned()] == [handle.id]
    await svc.release(handle)
    await asyncio.sleep(0.3)
    assert not await port_open("127.0.0.1", port)
    assert svc.owned() == []
    calls = [json.loads(line) for line in (tmp_path / "docker.log").read_text().splitlines()]
    up = next(c for c in calls if "up" in c)
    assert up[:3] == ["compose", "-p", "dfm-svc"]  # DFM's own, per-session project
    override = Path(up[up.index("-f", 4) + 1])
    assert "dfm.session: svc" in override.read_text()  # every container labelled
    assert calls[-1][:3] == ["compose", "-p", "dfm-svc"] and calls[-1][-1] == "down"


async def test_start_refused_without_approval(tmp_path: Path) -> None:
    if shutil.which("redis-server") is None:
        pytest.skip("redis-server not installed")
    with pytest.raises(ServiceError, match="not started"):
        await manager(tmp_path, tmp_path).ensure(ServiceSpec("redis", "cache", free_port()))


PG_TEST = """\
import os, subprocess
from urllib.parse import urlparse


def test_database_answers():
    url = urlparse(os.environ.get("DATABASE_URL", "postgresql://postgres@127.0.0.1:{port}/postgres"))
    out = subprocess.run(
        ["psql", "-h", "127.0.0.1", "-p", str(url.port), "-U", "postgres", "-tAc", "select 40 + 2"],
        capture_output=True, text=True, timeout=20,
    )
    assert out.stdout.strip() == "42", out.stderr
"""


@pytest.mark.skipif(
    not all(shutil.which(b) for b in ("postgres", "initdb", "psql", "pg_isready")),
    reason="PostgreSQL binaries not installed",
)
async def test_integration_tests_needing_postgres_run_end_to_end(
    home: Path, git_repo: Path, git: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the skip guard found Postgres on the developer's PATH; the isolated runtime gets that dir
    monkeypatch.setenv("PATH", path_with_tools("postgres", "initdb", "psql", "pg_isready"))
    port = free_port()
    fx.write(
        git_repo,
        {
            "pyproject.toml": '[project]\nname = "app"\nversion = "0"\n[tool.pytest.ini_options]\n',
            ".env.example": f"DATABASE_URL=postgresql://postgres@localhost:{port}/postgres\n",
            "tests/test_db.py": PG_TEST.replace("{port}", str(port)),
        },
    )
    git(git_repo, "add", ".")
    git(git_repo, "commit", "-qm", "db test")
    approver = Allow()
    rt = Runtime.create(
        git_repo, request="run the integration tests", mode="implement", approver=approver
    )
    result = await implement(
        rt, "make the tests pass", ImplementOptions(coder=ScriptedCoder(DONE), max_steps=1)
    )
    await rt.close(result.outcome.session_status)
    assert result.outcome.status == "completed", result.outcome.report()
    assert any(e.claim == "tests pass" for e in result.outcome.verified)
    assert any(s.startswith("initdb") for s in approver.asked) and any(
        s.startswith("postgres") for s in approver.asked
    )
    assert not await port_open("127.0.0.1", port)  # DFM stopped the Postgres it started
