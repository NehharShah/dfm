import json
import os
import sys
from pathlib import Path

import pytest

from dfm.config.writer import set_user_value
from dfm.context import ContextEngine, Task, TokenBudget
from dfm.integrations.reflex import load_skill
from dfm.orchestration.plan import plan
from dfm.orchestration.runtime import Runtime

HANDBOOK = {
    "project": "calc",
    "practices": [
        {
            "id": "p1",
            "kind": "rule",
            "text": "Run pytest with -q before committing",
            "when": "when writing tests",
            "sessions": 4,
            "last": "2026-10-01T00:00:00Z",
            "sources": [{"tool": "cursor", "request": "PRIVATE PROMPT TEXT"}],
        },
        {
            "id": "p2",
            "kind": "command",
            "text": "uv sync before running anything",
            "when": "",
            "sessions": 2,
            "last": "2026-09-01T00:00:00Z",
            "sources": [],
        },
    ],
}
ANSWERS = {
    "question": "q",
    "answers": [
        {
            "score": 0.9,
            "id": "p3",
            "kind": "rule",
            "text": "Start Postgres before integration tests",
            "when": "when testing",
            "sessions": 3,
            "last": "2026-10-02T00:00:00Z",
            "sources": [],
        }
    ],
}

FAKE_REFLEX = """#!{python}
import json, sys
open({log!r}, "a").write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["handbook", "ask"]:
    print(json.dumps({answers}))
elif sys.argv[1:2] == ["handbook"]:
    print(json.dumps({handbook}))
elif sys.argv[1:2] == ["--version"]:
    print("0.5.2")
else:
    sys.exit(2)
"""

SKILL = """---
name: postgres-integration-tests
description: "Repeated workflow involving start services, run tests. Use for requests similar to: postgres, integration, tests, database."
metadata:
  generator: reflex
---

# Repeated workflow: postgres integration tests

1. Start Postgres with docker compose.
2. Run the integration suite.
"""


@pytest.fixture
def reflex_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "reflex.log"
    fake = bin_dir / "reflex"
    fake.write_text(
        FAKE_REFLEX.format(
            python=sys.executable, log=str(log), answers=repr(ANSWERS), handbook=repr(HANDBOOK)
        )
    )
    fake.chmod(0o755)
    skills = home / ".reflex" / "skills" / "postgres-integration-tests"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(SKILL)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{bin_dir}:{Path(sys.executable).parent}:/usr/bin:/bin")
    set_user_value("models.apple.enabled", False, {"HOME": str(home)})
    return log


async def test_practices_ranked_and_private_prompts_dropped(
    reflex_env: Path, calc_repo: Path
) -> None:
    rt = Runtime.create(calc_repo, request="x", mode="memory")
    practices = await rt.reflex.practices(rt.repo, "run the integration tests")
    await rt.close("completed")
    assert [p.text for p in practices] == [
        "Start Postgres before integration tests",
        "Run pytest with -q before committing",
        "uv sync before running anything",
    ]
    assert all("PRIVATE PROMPT" not in p.line() for p in practices)
    calls = [json.loads(line) for line in reflex_env.read_text().splitlines()]
    assert all(c[0] == "handbook" for c in calls)  # read-only commands only
    assert all(c[-1] == "--json" for c in calls)


async def test_each_question_goes_to_reflex_once_per_session(
    reflex_env: Path, calc_repo: Path
) -> None:
    rt = Runtime.create(calc_repo, request="x", mode="implement")
    collect = rt.reflex.collector(rt.repo)
    task = Task("fix the postgres integration tests")
    first = await collect(task)
    for _ in range(4):  # later agent steps
        assert await collect(task) == first
    await rt.close("completed")
    calls = [json.loads(line) for line in reflex_env.read_text().splitlines()]
    assert len(calls) == 2  # `handbook ask <task>` and `handbook`, once each


async def test_context_engine_gets_historical_items(reflex_env: Path, calc_repo: Path) -> None:
    rt = Runtime.create(calc_repo, request="x", mode="implement")
    engine = ContextEngine(rt.env, None, extra=[rt.reflex.collector(rt.repo)])
    bundle = await engine.build(Task("fix the postgres integration tests"), TokenBudget(6000))
    await rt.close("completed")
    reflex_items = [i for i in bundle.items if i.source == "reflex"]
    assert len(reflex_items) == 2, bundle.dropped  # practices + the matching skill
    assert all(i.historical for i in reflex_items)
    assert any(i.path == "skill:postgres-integration-tests" for i in reflex_items)
    assert "historical evidence" in bundle.render()


async def test_plan_lists_historical_practice(reflex_env: Path, calc_repo: Path) -> None:
    rt = Runtime.create(calc_repo, request="fix", mode="plan")
    result = await plan(rt, "fix the failing integration tests")
    await rt.close("completed")
    assert any("Start Postgres" in p for p in result.historical_practice)
    assert any(e["type"] == "reflex_loaded" for e in rt.store.load(rt.session.id).events)


async def test_reflex_missing_or_disabled(
    reflex_env: Path, calc_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}:/usr/bin:/bin")
    rt = Runtime.create(calc_repo, request="x", mode="memory")
    assert await rt.reflex.practices(rt.repo, "x") == []
    assert rt.reflex.last_error
    await rt.close("completed")
    set_user_value("integrations.reflex.enabled", False, {"HOME": os.environ["HOME"]})
    off = Runtime.create(calc_repo, request="x", mode="memory")
    assert not off.reflex.enabled and off.reflex.skills() == []
    await off.close("completed")


def test_skill_parsing_and_matching(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(SKILL)
    skill = load_skill(path)
    assert skill is not None
    assert skill.keywords == ("postgres", "integration", "tests", "database")
    assert skill.score("run the postgres integration tests") >= 3
    assert skill.score("write a commit message") == 0
    (tmp_path / "BROKEN.md").write_text("no front matter")
    assert load_skill(tmp_path / "BROKEN.md") is None
