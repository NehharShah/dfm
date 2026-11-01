import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from dfm.config import DFMConfig
from dfm.execution import ExecutionEnvironment
from dfm.policy import Policy
from dfm.processes import ProcessManager, ProcessRegistry
from dfm.tools.filesystem import FsTools, ToolError
from dfm.tools.git import GitGuard, GitTools, WorktreeManager, parse_status_v2
from tests import repo_fixtures as fx
from tests.helpers import make_env

Git = Callable[..., str]


def runtime(repo: Path, tmp_path: Path) -> tuple[ExecutionEnvironment, ProcessManager, Policy]:
    env = make_env(repo, tmp_path, session_id="g")
    return env, ProcessManager(ProcessRegistry(env.tmp / "p")), Policy(DFMConfig())


# ---------------------------------------------------------------- 026 status / diff / log


def test_parse_status_v2_fixture() -> None:
    raw = "\0".join(
        [
            "# branch.oid abc123",
            "# branch.head main",
            "# branch.upstream origin/main",
            "# branch.ab +2 -1",
            "1 .M N... 100644 100644 100644 aaa bbb app.py",
            "1 MM N... 100644 100644 100644 aaa bbb both.py",
            "2 R. N... 100644 100644 100644 aaa bbb R100 new.py",
            "old.py",
            "u UU N... 100644 100644 100644 100644 a b c conflict.py",
            "1 .M S.M. 160000 160000 160000 aaa bbb vendor/lib",
            "? notes.txt",
            "",
        ]
    )
    status = parse_status_v2(raw)
    assert (status.branch, status.head, status.upstream, status.ahead, status.behind) == (
        "main",
        "abc123",
        "origin/main",
        2,
        1,
    )
    assert status.unstaged() == ["app.py", "both.py", "vendor/lib"]
    assert status.staged() == ["both.py", "new.py"]
    assert status.partially_staged() == ["both.py"]
    assert status.conflicted() == ["conflict.py"]
    assert status.untracked() == ["notes.txt"]
    renamed = next(e for e in status.entries if e.kind == "renamed")
    assert renamed.orig_path == "old.py"
    assert next(e for e in status.entries if e.path == "vendor/lib").submodule


async def test_status_diff_log(git_repo: Path, git: Git, tmp_path: Path) -> None:
    tools = GitTools(*runtime(git_repo, tmp_path))
    status = await tools.status()
    assert status.clean and status.branch == "main" and not status.detached
    (git_repo / "app.py").write_text("def add(a, b):\n    return a + b + 0\n")
    (git_repo / "new.txt").write_text("x\n")
    git(git_repo, "mv", "README.md", "DOCS.md")
    status = await tools.status()
    assert "app.py" in status.unstaged() and status.untracked() == ["new.txt"]
    diff = await tools.diff()
    assert [f.path for f in diff.files] == ["app.py"]
    staged = await tools.diff(staged=True)
    assert [f.path for f in staged.files] == ["DOCS.md"]
    log = await tools.log(5)
    assert log[0].subject == "init"
    assert (await tools.show_file("README.md")) == "# demo\n"
    with pytest.raises(ToolError):
        await tools.diff(ref="--output=/tmp/x")


async def test_add_only_dfm_files_and_commit_only_when_asked(
    git_repo: Path, git: Git, tmp_path: Path
) -> None:
    tools = GitTools(*runtime(git_repo, tmp_path))
    (git_repo / "app.py").write_text("changed\n")
    (git_repo / "README.md").write_text("user edit\n")
    with pytest.raises(ToolError, match="did not modify"):
        await tools.add(["README.md"], allowed={"app.py"})
    await tools.add(["app.py"], allowed={"app.py"})
    assert "M  app.py" in git(git_repo, "status", "--short")
    with pytest.raises(ToolError, match="only when you ask"):
        await tools.commit("msg", user_requested=False)
    with pytest.raises(
        ToolError, match="policy denied"
    ):  # non-interactive: LOCAL_STATE_CHANGE asks → deny
        await tools.commit("msg", user_requested=True)


# ---------------------------------------------------------------- 027 safety


def test_dirty_files_protected_until_planned(git_repo: Path, tmp_path: Path) -> None:
    (git_repo / "README.md").write_text("developer's unfinished notes\n")
    guard = GitGuard(git_repo)
    assert guard.start() == []
    verdict = guard.check_write("README.md")
    assert verdict is not None and "uncommitted changes" in verdict.reason
    assert guard.check_write("app.py") is None
    guard.plan_paths({"README.md"})
    assert guard.check_write("README.md") is None


async def test_unrelated_user_work_preserved_end_to_end(
    git_repo: Path, git: Git, tmp_path: Path
) -> None:
    (git_repo / "README.md").write_text("developer's unfinished notes\n")
    (git_repo / "scratch.txt").write_text("untracked user file\n")
    before_readme = (git_repo / "README.md").read_bytes()
    guard = GitGuard(git_repo)
    guard.start()
    env, _pm, policy = runtime(git_repo, tmp_path)
    fs = FsTools(env, policy, guard=guard.check_write)
    await fs.write_file("app.py", "def add(a, b):\n    return a + b\n")
    with pytest.raises(ToolError, match="uncommitted changes"):
        await fs.write_file("README.md", "overwritten")
    with pytest.raises(ToolError, match="uncommitted changes"):
        await fs.write_file("scratch.txt", "overwritten")
    assert (git_repo / "README.md").read_bytes() == before_readme
    assert guard.end_check(fs.modified) == []
    (git_repo / "README.md").write_text("something else touched it\n")
    assert guard.end_check(fs.modified) == [
        "README.md changed during the session but DFM did not edit it"
    ]


def test_partial_staging_reported(git_repo: Path, git: Git) -> None:
    (git_repo / "app.py").write_text("def add(a, b):\n    return a + b + 1\n")
    git(git_repo, "add", "app.py")
    (git_repo / "app.py").write_text("def add(a, b):\n    return a + b + 1  # wip\n")
    guard = GitGuard(git_repo)
    guard.start()
    assert guard.snapshot is not None and guard.snapshot.partially_staged == ["app.py"]
    verdict = guard.check_write("app.py")
    assert verdict is not None and "partially staged" in verdict.reason


def test_merge_conflict_blocks(git_repo: Path, git: Git) -> None:
    git(git_repo, "checkout", "-q", "-b", "other")
    (git_repo / "app.py").write_text("other\n")
    git(git_repo, "commit", "-qam", "other")
    git(git_repo, "checkout", "-q", "main")
    (git_repo / "app.py").write_text("main\n")
    git(git_repo, "commit", "-qam", "main")
    subprocess.run(
        ["git", "-C", str(git_repo), "merge", "-q", "other"], capture_output=True, check=False
    )
    guard = GitGuard(git_repo)
    reasons = guard.start()
    assert any("merge in progress" in r for r in reasons)
    assert any("app.py" in r for r in reasons)
    verdict = guard.check_write("README.md")
    assert verdict is not None and "merge in progress" in verdict.reason


def test_detached_lfs_nested(git_repo: Path, git: Git) -> None:
    fx.write(git_repo, {".gitattributes": "*.psd filter=lfs diff=lfs merge=lfs -text\n"})
    git(git_repo, "add", ".")
    git(git_repo, "commit", "-qm", "lfs")
    git(git_repo, "checkout", "-q", "--detach")
    nested = git_repo / "vendor" / "lib"
    nested.mkdir(parents=True)
    git(nested, "init", "-q")
    guard = GitGuard(git_repo)
    state = guard.inspect()
    assert state.detached and any("detached" in w for w in state.warnings)
    assert state.nested_repos == ["vendor/lib"]
    lfs = guard.check_write("art/logo.psd")
    assert lfs is not None and "LFS" in lfs.reason
    inner = guard.check_write("vendor/lib/x.py")
    assert inner is not None and "nested repository" in inner.reason


# ---------------------------------------------------------------- 028 worktrees


async def test_worktree_lifecycle(git_repo: Path, git: Git, tmp_path: Path) -> None:
    manager = WorktreeManager(git_repo, tmp_path / "data", Policy(DFMConfig()), session_id="s1")
    wt = await manager.create("HEAD")
    assert (wt.path / "app.py").exists()
    assert str(wt.path) in git(git_repo, "worktree", "list")
    (wt.path / "app.py").write_text("def add(a, b):\n    return a + b + 1\n")
    (wt.path / "added.py").write_text("X = 1\n")
    patch = manager.diff(wt)
    assert "+    return a + b + 1" in patch and "added.py" in patch
    assert git(git_repo, "status", "--porcelain") == ""  # user's tree untouched
    assert [w.id for w in manager.owned()] == [wt.id]
    manager.remove(wt)
    assert str(wt.path) not in git(git_repo, "worktree", "list")
    assert manager.owned() == []


async def test_never_removes_foreign_worktrees(git_repo: Path, git: Git, tmp_path: Path) -> None:
    mine = tmp_path / "users-own-worktree"
    git(git_repo, "worktree", "add", "-q", "--detach", str(mine))
    manager = WorktreeManager(git_repo, tmp_path / "data", Policy(DFMConfig()))
    with pytest.raises(ToolError, match="not created by DFM"):
        manager.remove("deadbeef")
    assert str(mine) in git(git_repo, "worktree", "list")


async def test_worktree_needs_consent_when_not_preferred(git_repo: Path, tmp_path: Path) -> None:
    manager = WorktreeManager(git_repo, tmp_path / "data", Policy(DFMConfig()), prefer=False)
    with pytest.raises(ToolError, match="policy denied"):
        await manager.create()
    with pytest.raises(ToolError, match="unknown ref"):
        await WorktreeManager(git_repo, tmp_path / "data", Policy(DFMConfig())).create("nope")
