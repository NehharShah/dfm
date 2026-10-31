import ast
import json
import logging
from pathlib import Path

from pydantic import BaseModel
from typer.testing import CliRunner

from dfm.cli.app import app
from dfm.models.base import GenerateRequest, GenerateResult, ModelHealth
from dfm.models.structured import structured
from dfm.observability import configure_logging, load
from dfm.orchestration.implement import ImplementOptions, implement
from dfm.orchestration.runtime import Runtime
from tests.helpers import DONE, FIX, ScriptedCoder

TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


async def test_fixture_run_produces_metrics(home: Path, calc_repo: Path) -> None:
    rt = Runtime.create(calc_repo, request="fix the failing test", mode="implement")
    result = await implement(
        rt, "fix the failing test", ImplementOptions(coder=ScriptedCoder(FIX, DONE))
    )
    await rt.close(result.outcome.session_status)
    metrics = load(rt.dirs.data / "metrics", rt.session.id)  # what 068 benchmarks read
    assert (
        metrics is not None and metrics["status"] == "completed" and metrics["mode"] == "implement"
    )
    assert (
        metrics["stages"]["tests"]["count"] >= 2
        and metrics["stages"]["model"]["scripted-coder"]["count"] == 2
    )
    assert (
        metrics["tool_calls"]["patches"] == 1 and metrics["steps"] == 2 and metrics["repairs"] == 0
    )
    assert (
        metrics["context"]["items"] > 0 and metrics["memory_peak_mb"] > 0 and metrics["wall_ms"] > 0
    )


class Answer(BaseModel):
    ok: bool


class CountingModel:
    name = "counting"

    async def healthcheck(self) -> ModelHealth:
        return ModelHealth(self.name, True, "ok")

    async def generate(self, request: GenerateRequest) -> GenerateResult:
        return GenerateResult('{"ok": true}', self.name, 5, tokens_in=120, tokens_out=7)

    async def stream(self, request: GenerateRequest):  # type: ignore[no-untyped-def]
        yield ""


async def test_tokens_are_recorded(home: Path, calc_repo: Path) -> None:
    rt = Runtime.create(calc_repo, request="x", mode="ask")
    for _ in range(2):
        await structured(CountingModel(), GenerateRequest.prompt("hi"), Answer)
    await rt.close("completed")
    metrics = load(rt.dirs.data / "metrics", rt.session.id)
    assert metrics is not None and metrics["tokens"] == {"in": 240, "out": 14}


async def test_logs_are_json_redacted_and_rotated(
    home: Path, calc_repo: Path, tmp_path: Path
) -> None:
    rt = Runtime.create(calc_repo, request="x", mode="ask")
    rt.event("note", text=f"pushing with {TOKEN}")
    await rt.close("completed")
    log = rt.dirs.data / "logs" / "dfm.jsonl"
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    note = next(r for r in lines if r["msg"] == "note")
    assert note["session"] == rt.session.id and TOKEN not in log.read_text()
    assert "[REDACTED" in json.dumps(note["payload"])
    small = configure_logging(tmp_path / "logs", max_bytes=2000, backups=2)
    for i in range(200):
        logging.getLogger("dfm.test").info("line %d %s", i, "x" * 50)
    assert small.exists() and (tmp_path / "logs" / "dfm.jsonl.1").exists()
    assert not (tmp_path / "logs" / "dfm.jsonl.3").exists()  # bounded by backupCount


def test_no_remote_telemetry_code() -> None:
    root = Path(__file__).parents[1] / "dfm" / "observability"
    banned = {"urllib", "http", "socket", "requests", "httpx", "aiohttp", "ssl"}
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            assert not any(n.split(".")[0] in banned for n in names), (path.name, names)


async def test_status_and_profile(home: Path, calc_repo: Path) -> None:
    rt = Runtime.create(calc_repo, request="fix the failing test", mode="implement")
    result = await implement(
        rt, "fix the failing test", ImplementOptions(coder=ScriptedCoder(FIX, DONE))
    )
    await rt.close(result.outcome.session_status)
    status = CliRunner().invoke(app, ["status", "--repo", str(calc_repo), "--json"])
    assert status.exit_code == 0, status.output
    rows = json.loads(status.stdout)["sessions"]
    assert rows[0]["session"] == rt.session.id and rows[0]["wall_ms"]
    profile = CliRunner().invoke(app, ["profile", rt.session.id, "--json"])
    assert profile.exit_code == 0 and json.loads(profile.stdout)["tool_calls"]["patches"] == 1
    human = CliRunner().invoke(app, ["profile", rt.session.id])
    assert "model scripted-coder: 2 calls" in human.output
