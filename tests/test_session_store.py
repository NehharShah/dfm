import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dfm.cli.app import app
from dfm.config import DFMConfig, state_dirs
from dfm.policy import ActionClass, Policy, ProposedAction
from dfm.sessions import (
    SessionStore,
    ensure_repo_state_dir,
    new_session_id,
    repo_hash,
    trace_policy,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "proj"
    (r / ".git" / "info").mkdir(parents=True)
    return r


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "data" / "sessions")


def test_ids_sort_by_time() -> None:
    a = new_session_id()
    b = new_session_id()
    assert len(a) == len(b) == 22
    assert a[:15] <= b[:15]


def test_roundtrip(store: SessionStore, repo: Path) -> None:
    session = store.create(repo, "fix the failing test", mode="implement")
    store.append(session, "user_request", text="fix the failing test")
    store.append(session, "command_finished", argv=["uv", "run", "pytest"], exit_code=1)
    store.complete(session, "completed", "fixed")
    record = store.load(session.id)
    assert record.header["source"] == "dfm"
    assert record.header["schema_version"] == 1
    assert record.header["repo_hash"] == repo_hash(repo)
    assert record.header["repo_name"] == "proj"
    assert [e["seq"] for e in record.events] == [1, 2, 3]
    assert record.status == "completed"
    assert not record.torn_tail


def test_unknown_event_type_rejected(store: SessionStore, repo: Path) -> None:
    session = store.create(repo, "x")
    with pytest.raises(ValueError, match="unknown"):
        store.append(session, "made_up")


def test_redaction_and_no_file_bodies(store: SessionStore, repo: Path) -> None:
    session = store.create(repo, "use OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx please")
    store.append(
        session,
        "command_started",
        argv=["cast", "send", "--private-key", "0xdeadbeef"],
        env={"GH_TOKEN": "ghp_" + "a" * 36},
    )
    store.append(session, "file_read", path="src/app.py", content="SECRET BODY\n" * 10)
    raw = session.path.read_text()
    assert "sk-abcdefghijklmnopqrstuvwx" not in raw
    assert "0xdeadbeef" not in raw
    assert "ghp_" not in raw
    assert "SECRET BODY" not in raw
    event = store.load(session.id).events[-1]
    assert event["content_bytes"] == 120
    assert len(event["content_sha256"]) == 64


def test_torn_tail_tolerated(store: SessionStore, repo: Path) -> None:
    session = store.create(repo, "x")
    store.append(session, "note", text="ok")
    with session.path.open("a") as fh:
        fh.write('{"type": "note", "seq": 2, "te')
    record = store.load(session.id)
    assert record.torn_tail
    assert len(record.events) == 1


def test_reopen_continues_sequence(store: SessionStore, repo: Path, tmp_path: Path) -> None:
    session = store.create(repo, "x")
    store.append(session, "note", text="a")
    reopened = store.open(session.id, repo)
    assert store.append(reopened, "note", text="b")["seq"] == 2
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="different repository"):
        store.open(session.id, other)


def test_list_and_index(store: SessionStore, repo: Path, tmp_path: Path) -> None:
    first = store.create(repo, "one")
    store.complete(first, "failed")
    second = store.create(repo, "two")
    other = tmp_path / "other"
    other.mkdir()
    store.create(other, "elsewhere")
    mine = store.list_sessions(repo)
    assert {s.request for s in mine} == {"one", "two"}
    assert len(store.list_sessions()) == 3
    latest = store.latest(repo, unfinished=True)
    assert latest is not None and latest.id == second.id
    index = json.loads((repo / ".dfm" / "sessions.json").read_text())
    assert index[first.id]["status"] == "failed"
    assert index[second.id]["status"] == "running"


def test_dfm_dir_excluded_from_git(repo: Path) -> None:
    ensure_repo_state_dir(repo)
    ensure_repo_state_dir(repo)
    exclude = (repo / ".git" / "info" / "exclude").read_text()
    assert exclude.count("/.dfm/") == 1
    assert not (repo / ".gitignore").exists()


async def test_policy_verdicts_traced(store: SessionStore, repo: Path) -> None:
    session = store.create(repo, "x")
    policy = Policy(DFMConfig())
    trace_policy(policy, store, session)
    await policy.authorize(ProposedAction("shell", ActionClass.UNKNOWN, "frob", "agent"))
    event = store.load(session.id).events[-1]
    assert event["type"] == "policy_decision"
    assert event["decision"] == "deny"
    assert event["action_class"] == "UNKNOWN"


def test_cli_sessions(tmp_path: Path, repo: Path) -> None:
    env = {"HOME": str(tmp_path / "home")}
    store = SessionStore(state_dirs(env).sessions)
    store.create(repo, "explain this repository")
    runner = CliRunner()
    result = runner.invoke(app, ["sessions", "--all"], env=env)
    assert result.exit_code == 0, result.output
    assert "explain" in result.stdout
    assert store.list_sessions()[0].id in result.stdout


def test_dfm_state_is_excluded_in_a_linked_worktree(git_repo: Path, tmp_path: Path) -> None:
    import subprocess

    from dfm.sessions.store import ensure_repo_state_dir

    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(git_repo), "worktree", "add", "-q", "--detach", str(linked)], check=True
    )
    ensure_repo_state_dir(linked)
    (linked / ".dfm" / "checkpoint.json").write_text("{}")
    status = subprocess.run(
        ["git", "-C", str(linked), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert ".dfm" not in status
