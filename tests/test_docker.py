import json
import stat
import sys
from pathlib import Path

import pytest

from dfm.config import DFMConfig, state_dirs
from dfm.execution import ExecutionEnvironment
from dfm.policy import Approval, Decision, Policy
from dfm.policy.engine import NonInteractiveApprover
from dfm.policy.types import PolicyVerdict, ProposedAction
from dfm.processes import ProcessManager, ProcessRegistry
from dfm.tools.docker import ComposeHandle, Docker, detect_runtime, project_name
from dfm.tools.errors import ToolError
from dfm.tools.shell.classify import classify

TABLE = [
    ("docker ps", "SAFE_READ"),
    ("docker build -t app .", "SAFE_BUILD"),
    ("docker compose up -d db", "LOCAL_STATE_CHANGE"),
    ("docker rm -f abc123", "LOCAL_DESTRUCTIVE"),
    ("docker rmi app:latest", "LOCAL_DESTRUCTIVE"),
    ("docker system prune -af", "DESTRUCTIVE"),
    ("docker volume prune", "DESTRUCTIVE"),
    ("docker container prune -f --filter label=dfm.session=s1", "SAFE_LOCAL"),
    ("docker container prune -f --filter label=other=1", "DESTRUCTIVE"),
    ("docker compose -p dfm-s1 down", "SAFE_LOCAL"),
    ("docker compose -p shop down", "LOCAL_DESTRUCTIVE"),
    ("docker run -v /:/host alpine", "PRIVILEGED"),
    ("docker run --privileged alpine", "PRIVILEGED"),
    ("docker run -v /var/run/docker.sock:/var/run/docker.sock alpine", "PRIVILEGED"),
    ("docker pull postgres:16", "REMOTE_READ"),
    ("docker image pull postgres:16", "REMOTE_READ"),
]


@pytest.mark.parametrize(("command", "expected"), TABLE)
def test_classification_table(command: str, expected: str) -> None:
    assert classify(command).parts[0].action.value == expected
    if "pull" in command:
        part = classify(command).parts[0]
        assert part.network is not None and part.network.value == "container_registries"


class Recorder:
    def __init__(self, answer: Approval = Approval.ONCE) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def ask(self, action: ProposedAction, verdict: PolicyVerdict) -> Approval:
        self.asked.append(action.summary)
        return self.answer


async def decide(command: str, approver: object) -> Decision:
    policy = Policy(DFMConfig(), approver)  # type: ignore[arg-type]
    (action,) = classify(command).to_actions("dfm")
    return (await policy.authorize(action)).decision


async def test_system_prune_denied_labelled_cleanup_allowed() -> None:
    assert await decide("docker system prune -af", NonInteractiveApprover()) is Decision.DENY
    quiet = Recorder()
    cleanup = "docker container prune -f --filter label=dfm.session=s1"
    assert await decide(cleanup, quiet) is Decision.ALLOW and quiet.asked == []


def make_docker(tmp_path: Path, approver: Recorder) -> tuple[Docker, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(
        f"#!{sys.executable}\n" + (Path(__file__).parent / "fake_docker.py").read_text()
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "docker.log"
    (bin_dir / "fake_docker.json").write_text(json.dumps({"port": 0, "log": str(log)}))
    environ = {"HOME": str(tmp_path / "home"), "PATH": f"{bin_dir}:/usr/bin:/bin"}
    env = ExecutionEnvironment.create(
        tmp_path, DFMConfig(), session_id="S-1", environ=environ, dirs=state_dirs(environ)
    )
    pm = ProcessManager(ProcessRegistry(env.tmp / "p"), session_id="S-1")
    return Docker(env, pm, Policy(DFMConfig(), approver), "S-1"), log


async def test_compose_is_namespaced_labelled_and_cleaned(tmp_path: Path) -> None:
    approver = Recorder()
    docker, log = make_docker(tmp_path, approver)
    assert docker.project == project_name("S-1") == "dfm-s-1"
    handle = await docker.compose_up("compose.yaml", ["db"])
    assert len(approver.asked) == 1 and "up" in approver.asked[0]  # starting asks
    assert "dfm.session: S-1" in handle.override.read_text()
    await docker.compose_down(handle)
    await docker.cleanup()
    assert len(approver.asked) == 1  # tearing down DFM's own resources does not ask
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls[0][:3] == ["compose", "-p", "dfm-s-1"] and calls[1][-1] == "down"
    assert [c[:2] for c in calls[2:]] == [
        ["container", "prune"],
        ["volume", "prune"],
        ["network", "prune"],
    ]
    assert all(c[-1] == "label=dfm.session=S-1" for c in calls[2:])


async def test_label_enforcement(tmp_path: Path) -> None:
    docker, _ = make_docker(tmp_path, Recorder(Approval.DENY))
    with pytest.raises(ToolError, match="not a DFM project"):
        await docker.compose_down(ComposeHandle("shop", "compose.yaml", tmp_path / "o.yml", ["db"]))
    argv = docker.run_argv("alpine", "--privileged", name="probe")
    assert argv[:6] == ["docker", "run", "--rm", "--init", "--label", "dfm.session=S-1"]
    assert classify(argv).parts[0].action.value == "PRIVILEGED"  # flags still win
    with pytest.raises(ToolError, match="policy refused"):
        await docker.compose_up("compose.yaml", ["db"])  # denied: nothing started


def test_runtime_detection(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert detect_runtime(home, {}) == ("none", None) or detect_runtime(home, {})[0] == "default"
    sock = home / ".orbstack" / "run" / "docker.sock"
    sock.parent.mkdir(parents=True)
    sock.touch()
    assert detect_runtime(home, {}) == ("orbstack", str(sock))
    colima = "unix:///Users/x/.colima/default/docker.sock"
    assert detect_runtime(home, {"DOCKER_HOST": colima}) == (
        "colima",
        "/Users/x/.colima/default/docker.sock",
    )
