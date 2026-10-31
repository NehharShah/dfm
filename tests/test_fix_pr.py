"""048: per-comment review repair. Fixture PR 8 has 3 open threads (2 right, 1 wrong),
1 resolved and 1 outdated thread."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dfm.cli.app import app
from dfm.config.writer import set_user_value
from dfm.orchestration.github import fix_pr
from dfm.orchestration.implement import ImplementOptions
from dfm.orchestration.runtime import Runtime
from tests.helpers import DONE, ScriptedCoder, calls, configure_fake

Git = Callable[..., str]

HEAD_OPS = "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * a\n"
TRIAGE = [
    {
        "verdict": "valid",
        "evidence": "return a - b",
        "reasoning": "add subtracts",
        "reply": "Good catch.",
    },
    {
        "verdict": "valid",
        "evidence": "return a * a",
        "reasoning": "mul ignores b",
        "reply": "Right.",
    },
    {
        "verdict": "invalid",
        "evidence": "assert add(2, 3) == 5",
        "reasoning": "2 + 3 is 5; the test expectation is correct",
        "reply": "2 + 3 is 5, so the test's expectation is right; the bug was in `add` itself.",
    },
]
FIXES = [
    {
        "action": "patch",
        "reason": "add",
        "path": "src/calc/ops.py",
        "search": "    return a - b\n",
        "replace": "    return a + b\n",
    },
    {
        "action": "patch",
        "reason": "mul",
        "path": "src/calc/ops.py",
        "search": "    return a * a\n",
        "replace": "    return a * b\n",
    },
]


@pytest.fixture
def pr_repo(calc_repo: Path, git: Git, home: Path, fake_gh: Path, tmp_path: Path) -> Path:
    git(calc_repo, "remote", "add", "origin", "https://github.com/acme/widget.git")
    git(calc_repo, "checkout", "-q", "-b", "add-mul")
    (calc_repo / "src/calc/ops.py").write_text(HEAD_OPS)
    tests = (calc_repo / "tests/test_ops.py").read_text().replace("import add", "import add, mul")
    (calc_repo / "tests/test_ops.py").write_text(
        tests + "\n\ndef test_mul():\n    assert mul(2, 3) == 6\n"
    )
    git(calc_repo, "commit", "-qam", "add mul")
    sha = git(calc_repo, "rev-parse", "HEAD").strip()
    git(calc_repo, "checkout", "-q", "main")
    configure_fake(tmp_path / "bin", pr_sha=sha)
    set_user_value("network.github", "allow", {"HOME": str(home)})
    return calc_repo


async def test_two_fixes_one_pushback(pr_repo: Path, git: Git, fake_gh: Path) -> None:
    coder = ScriptedCoder(*TRIAGE, *FIXES, DONE)
    rt = Runtime.create(pr_repo, request="fix PR 8", mode="implement")
    fixed = await fix_pr(rt, 8, ImplementOptions(coder=coder))
    assert fixed.result is not None
    await rt.close(fixed.result.outcome.session_status)
    assert [t.verdict for t in fixed.triaged] == ["valid", "valid", "invalid", "skipped", "skipped"]
    assert fixed.result.outcome.status == "completed", fixed.result.outcome.report()
    patch = fixed.result.patch_path
    assert patch is not None
    assert "+    return a + b" in patch.read_text() and "+    return a * b" in patch.read_text()
    pushback = [r for r in fixed.replies if r.thread.path == "tests/test_ops.py"]
    assert len(pushback) == 1 and "2 + 3 is 5" in pushback[0].body
    assert len(fixed.replies) == 3  # 1 pushback + 2 "fixed" notes, all drafts
    loop_prompt = coder.prompts[len(TRIAGE)]
    assert "should be 6" not in loop_prompt  # the wrong comment is not sent to the fixer
    assert "`mul` multiplies `a` by itself" in loop_prompt
    assert git(pr_repo, "status", "--porcelain") == ""  # worktree only
    assert fixed.next_steps()[-1] == "git push"  # printed for the developer, never run
    assert not [c for c in calls(fake_gh) if any("replies" in a for a in c)]  # nothing posted


async def test_evidence_must_match_the_code(pr_repo: Path, fake_gh: Path) -> None:
    invented = {"verdict": "valid", "evidence": "os.system(cmd)", "reasoning": "x", "reply": "y"}
    coder = ScriptedCoder(invented, invented, invented, DONE)
    rt = Runtime.create(pr_repo, request="fix PR 8", mode="implement")
    fixed = await fix_pr(rt, 8, ImplementOptions(coder=coder))
    await rt.close("completed")
    assert [t.verdict for t in fixed.triaged[:3]] == ["unclear"] * 3
    assert fixed.result is None and "nothing to fix" in fixed.notes[-1]


def test_cli_drafts_and_post_needs_approval(
    pr_repo: Path, fake_gh: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = CliRunner().invoke(
        app, ["fix-pr", "8", "--repo", str(pr_repo), "--json", "--post-replies"]
    )
    payload = json.loads(result.stdout)
    # no local model here: every open thread is unclear, nothing is fixed or replied to
    assert [t["verdict"] for t in payload["threads"]] == [
        "unclear",
        "unclear",
        "unclear",
        "skipped",
        "skipped",
    ]
    assert payload["replies"] == [] and payload["posted"] == [] and payload["patch"] is None


async def test_posting_replies_asks_and_is_refused_without_a_tty(
    pr_repo: Path, fake_gh: Path
) -> None:
    from dfm.orchestration.github import post_replies

    coder = ScriptedCoder(*TRIAGE, *FIXES, DONE)
    rt = Runtime.create(pr_repo, request="fix PR 8", mode="implement")
    fixed = await fix_pr(rt, 8, ImplementOptions(coder=coder))
    posted = await post_replies(rt, fixed)
    await rt.close("completed")
    assert len(posted) == 3 and all("not posted (policy" in line for line in posted)
    assert not [c for c in calls(fake_gh) if any("replies" in a for a in c)]
