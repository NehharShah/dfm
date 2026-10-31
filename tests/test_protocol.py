import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dfm.cli.app import app
from dfm.config.writer import set_user_value
from dfm.orchestration.agent import AgentLoop
from dfm.orchestration.runtime import Runtime
from dfm.sessions import SessionStore
from dfm.sessions.protocol import EVENT_FIELDS, json_schema, validate_file, validate_records
from tests.helpers import DONE, FIX, ScriptedCoder

SCHEMA_FILE = (
    Path(__file__).resolve().parents[1] / "docs" / "protocol" / "dfm-session-v1.schema.json"
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("HOME", str(h))
    set_user_value("models.apple.enabled", False, {"HOME": str(h)})
    return h


def test_schema_file_in_sync() -> None:
    assert json.loads(SCHEMA_FILE.read_text()) == json_schema()


async def test_real_agent_trace_is_valid(home: Path, calc_repo: Path) -> None:
    rt = Runtime.create(calc_repo, request="fix", mode="implement")
    outcome = await AgentLoop(rt, coder=ScriptedCoder(FIX, DONE)).run("fix the failing test")
    await rt.close(outcome.session_status)
    assert validate_file(rt.session.path) == []
    types = {json.loads(line).get("type") for line in rt.session.path.read_text().splitlines()[1:]}
    exported = {
        "user_request",
        "repocanon_loaded",
        "repokg_loaded",
        "context_selected",
        "file_read",
        "command_started",
        "command_finished",
        "model_started",
        "model_finished",
        "patch_started",
        "patch_finished",
        "test_started",
        "test_finished",
        "verification",
        "task_completed",
        "policy_decision",
    }
    assert exported - types <= {"file_read"}  # the scripted coder never reads
    assert set(types) <= set(EVENT_FIELDS)


_EMITTED = re.compile(
    r'(?:_emit|\.event|on_event|trace|emit)\(\s*"([a-z_]+)"'
    r'|\bappend\(\s*[\w.]*session,\s*"([a-z_]+)"'
)


def test_every_emitted_event_type_is_in_the_protocol() -> None:
    # an unknown type raises in SessionStore.append, mid-task (a `file_stub` event once
    # crashed every run that read a large file)
    root = Path(__file__).parent.parent / "dfm"
    emitted = {
        next(g for g in m.groups() if g): str(path.relative_to(root.parent))
        for path in root.rglob("*.py")
        for m in _EMITTED.finditer(path.read_text())
    }
    assert len(emitted) > 15  # the pattern still finds the call sites
    assert {name: where for name, where in emitted.items() if name not in EVENT_FIELDS} == {}


def test_store_rejects_events_missing_fields(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    session = store.create(tmp_path, "x")
    with pytest.raises(ValueError, match="missing"):
        store.append(session, "command_finished", exit_code=0)


def test_validator_findings() -> None:
    header = {
        "schema_version": 1,
        "session_id": "s",
        "source": "dfm",
        "dfm_version": "0",
        "repo_hash": "h",
        "repo_name": "r",
        "started_at": "t",
        "mode": "m",
        "request": "q",
    }
    good = [
        header,
        {"type": "note", "seq": 1, "at": "t"},
        {"type": "future_event", "seq": 2, "at": "t", "x": 1},
    ]
    assert validate_records(good) == []
    bad = [
        {**header, "schema_version": 2},
        {"type": "note", "seq": 1, "at": "t"},
        {"type": "note", "seq": 1, "at": "t"},
        {"type": "file_read", "seq": 3, "at": "t", "path": "a.py", "content": "secret body"},
        {"type": "task_completed", "seq": 4, "at": "t", "status": "great"},
    ]
    messages = " | ".join(str(e) for e in validate_records(bad))
    assert (
        "schema_version" in messages
        and "seq" in messages
        and "file bodies" in messages
        and "unknown status" in messages
    )


async def test_export_validate_cli_and_private_mode(home: Path, calc_repo: Path) -> None:
    rt = Runtime.create(calc_repo, request="x", mode="ask")
    await rt.close("completed")
    runner = CliRunner()
    exported = runner.invoke(app, ["sessions", "export", rt.session.id])
    assert (
        exported.exit_code == 0 and json.loads(exported.stdout.splitlines()[0])["source"] == "dfm"
    )
    assert runner.invoke(app, ["sessions", "validate", rt.session.id]).exit_code == 0
    set_user_value("integrations.reflex.session_export", False, {"HOME": str(home)})
    private = Runtime.create(calc_repo, request="y", mode="ask")
    await private.close("completed")
    assert private.session.path.parent.name == "private"
    listed = runner.invoke(app, ["sessions", "--all"])
    assert private.session.id in listed.stdout and rt.session.id in listed.stdout
