import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dfm.cli.app import app
from dfm.config.writer import set_user_value
from dfm.integrations.github import GitHubError, repo_slug
from dfm.orchestration.github import fix_pr, github_for, implement_issue, review_pr
from dfm.orchestration.implement import ImplementOptions
from dfm.orchestration.runtime import Runtime
from tests.helpers import DONE, FIX, ScriptedCoder, calls, configure_fake

FIXTURES = Path(__file__).parent / "fixtures" / "github"
Git = Callable[..., str]


def allow_github(home: Path, rule: str = "allow") -> None:
    set_user_value("network.github", rule, {"HOME": str(home)})


@pytest.fixture
def repo(calc_repo: Path, git: Git) -> Path:
    git(calc_repo, "remote", "add", "origin", "git@github.com:acme/widget.git")
    return calc_repo


def test_repo_slug(repo: Path, git: Git) -> None:
    assert repo_slug(repo) == "acme/widget"
    git(repo, "remote", "set-url", "origin", "https://github.com/acme/widget.git")
    assert repo_slug(repo) == "acme/widget"
    git(repo, "remote", "set-url", "origin", "https://gitlab.com/acme/widget.git")
    assert repo_slug(repo) is None


async def test_live_then_cache_with_staleness(home: Path, repo: Path, fake_gh: Path) -> None:
    allow_github(home)
    rt = Runtime.create(repo, request="issue", mode="github")
    live = await github_for(rt).issue(42)
    assert live.source == "live" and live.value.title == "Crash when the config file is empty"
    assert live.value.labels and live.value.author == "user1"
    await rt.close("completed")

    allow_github(home, "deny")
    rt = Runtime.create(repo, request="issue", mode="github")
    gh = github_for(rt)
    cached = await gh.issue(42)
    assert cached.source == "cache" and "policy" in cached.note
    assert cached.staleness().startswith("from cache, fetched 0 min ago")
    with pytest.raises(GitHubError, match="not cached"):
        await gh.issue(43)
    await rt.close("completed")
    assert [c[:2] for c in calls(fake_gh)] == [["issue", "view"]]  # the denied reads never ran


async def test_checks_comments_and_failing_exit_code(home: Path, repo: Path, fake_gh: Path) -> None:
    allow_github(home)
    rt = Runtime.create(repo, request="pr", mode="github")
    gh = github_for(rt)
    checks = (await gh.checks(7)).value
    assert [c.name for c in checks if c.failed] == ["test (ubuntu)"]
    comments = (await gh.comments(7)).value
    assert [(c.path, c.line) for c in comments] == [
        ("src/widget/config.py", 12),
        ("src/widget/config.py", 20),
    ]
    assert comments[0].author == "user5"
    await rt.close("completed")


async def test_writes_always_ask(home: Path, repo: Path, fake_gh: Path) -> None:
    allow_github(home)  # reads allowed; a comment is REMOTE_WRITE and asks (no TTY → no)
    rt = Runtime.create(repo, request="comment", mode="github")
    with pytest.raises(GitHubError, match="policy"):
        await github_for(rt).comment(7, "done")
    await rt.close("completed")
    assert not [c for c in calls(fake_gh) if "comment" in c]


async def test_implement_issue_offline_from_cache(home: Path, repo: Path, fake_gh: Path) -> None:
    allow_github(home)
    rt = Runtime.create(repo, request="sync", mode="github")
    await github_for(rt).issue(42)  # sync while online
    await rt.close("completed")

    allow_github(home, "deny")  # network off for GitHub
    rt = Runtime.create(repo, request="implement issue 42", mode="implement")
    coder = ScriptedCoder(FIX, DONE)
    issue, result = await implement_issue(rt, 42, ImplementOptions(coder=coder), refresh=False)
    await rt.close(result.outcome.session_status)
    assert issue.source == "cache"
    assert result.outcome.status == "completed", result.outcome.report()
    prompt = coder.prompts[0]
    assert '<repository-data source="github-issue-42" trust="untrusted">' in prompt
    assert "Crash when the config file is empty" in prompt
    assert len(calls(fake_gh)) == 1  # only the earlier sync


def make_pr_branch(repo: Path, git: Git, tmp_path: Path) -> str:
    git(repo, "checkout", "-q", "-b", "fix-empty-config")
    (repo / "src/calc/ops.py").write_text("def add(a, b):\n    return eval(f'{a} + {b}')\n")
    git(repo, "commit", "-qam", "use eval")
    sha = git(repo, "rev-parse", "HEAD").strip()
    diff = git(repo, "diff", "main..fix-empty-config")
    git(repo, "checkout", "-q", "main")
    (tmp_path / "pr.diff").write_text(diff)
    configure_fake(tmp_path / "bin", pr_sha=sha, pr_diff=str(tmp_path / "pr.diff"))
    return sha


async def test_review_pr_in_a_worktree(
    home: Path, repo: Path, fake_gh: Path, git: Git, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    allow_github(home)
    make_pr_branch(repo, git, tmp_path)
    before = (repo / "src/calc/ops.py").read_text()
    rt = Runtime.create(repo, request="review PR 7", mode="review")
    result = await review_pr(rt, 7)
    await rt.close("completed")
    assert any(f.title == "eval of dynamic input" for f in result.report.findings)
    assert result.source == "live"
    assert (repo / "src/calc/ops.py").read_text() == before  # the user's tree is untouched
    assert "fix-empty-config" not in git(repo, "worktree", "list")  # DFM's worktree is gone


async def test_review_pr_uses_the_head_that_is_present(
    home: Path, repo: Path, fake_gh: Path, git: Git, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import dfm.orchestration.github as gh_mod

    allow_github(home)
    make_pr_branch(repo, git, tmp_path)
    present = git(repo, "rev-parse", "HEAD").strip()

    async def fetched_other_head(rt, gh, number, sha):  # type: ignore[no-untyped-def]
        return present  # e.g. FETCH_HEAD after a force-push: not the API's sha

    created: list[str] = []
    real_create = gh_mod.WorktreeManager.create

    async def record(self, base):  # type: ignore[no-untyped-def]
        created.append(base)
        return await real_create(self, base)

    monkeypatch.setattr(gh_mod, "ensure_pr_head", fetched_other_head)
    monkeypatch.setattr(gh_mod.WorktreeManager, "create", record)
    rt = Runtime.create(repo, request="review PR 7", mode="review")
    result = await review_pr(rt, 7)
    await rt.close("completed")
    assert created == [present]
    assert any("not available locally" in n for n in result.notes)


async def test_fix_pr_returns_a_patch(
    home: Path, repo: Path, fake_gh: Path, git: Git, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    allow_github(home)
    make_pr_branch(repo, git, tmp_path)
    fix = {
        "action": "patch",
        "reason": "eval is unsafe and wrong",
        "path": "src/calc/ops.py",
        "search": "    return eval(f'{a} + {b}')\n",
        "replace": "    return a + b\n",
    }
    coder = ScriptedCoder(fix, DONE)
    rt = Runtime.create(repo, request="fix PR 7", mode="implement")
    fixed = await fix_pr(rt, 7, ImplementOptions(coder=coder))
    result = fixed.result
    assert result is not None
    await rt.close(result.outcome.session_status)
    assert result.outcome.status == "completed", result.outcome.report()
    assert result.patch_path is not None and "+    return a + b" in result.patch_path.read_text()
    assert "Failing check: CI / test (ubuntu)" in coder.prompts[0]
    # PR 7's comments point at files this repo does not have: unclear, not sent as fixes
    assert {t.verdict for t in fixed.triaged} == {"unclear"}
    assert "src/widget/config.py" not in coder.prompts[0]
    assert fixed.next_steps()[:2] == [
        "git switch fix-empty-config",
        f"git apply {result.patch_path}",
    ]
    assert git(repo, "status", "--porcelain") == ""  # nothing touched in the user's tree


def test_cli_issue_json_and_offline_error(home: Path, repo: Path, fake_gh: Path) -> None:
    allow_github(home)
    result = CliRunner().invoke(app, ["issue", "42", "--repo", str(repo), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["source"] == "live"
    missing = CliRunner().invoke(app, ["pr", "99", "--repo", str(repo), "--offline"])
    assert missing.exit_code == 2 and "not cached" in missing.output


def test_no_github_remote(home: Path, calc_repo: Path) -> None:
    result = CliRunner().invoke(app, ["issue", "1", "--repo", str(calc_repo)])
    assert result.exit_code != 0
    assert "no github.com `origin` remote" in result.output
    assert shutil.which("git") and subprocess.run(["true"]).returncode == 0
