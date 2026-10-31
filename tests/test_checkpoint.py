from collections.abc import Callable
from pathlib import Path

from dfm.recovery import Checkpointer, FileChange, Stage
from dfm.tools.git.state import dirty_paths, head, worktree_state


def test_git_state(git_repo: Path, git: Callable[..., str]) -> None:
    assert head(git_repo) == git(git_repo, "rev-parse", "HEAD").strip()
    assert dirty_paths(git_repo) == []
    (git_repo / "app.py").write_text("changed\n")
    (git_repo / "new.txt").write_text("n\n")
    git(git_repo, "mv", "README.md", "DOCS.md")
    assert sorted(dirty_paths(git_repo)) == ["DOCS.md", "app.py", "new.txt"]
    before = worktree_state(git_repo).fingerprint
    (git_repo / "app.py").write_text("changed again\n")
    assert worktree_state(git_repo).fingerprint != before


def test_head_none_outside_git(tmp_path: Path) -> None:
    assert head(tmp_path) is None
    assert dirty_paths(tmp_path) == []


def test_save_and_latest(git_repo: Path) -> None:
    cp = Checkpointer(git_repo, "s1")
    assert cp.latest() is None
    first = cp.save(Stage.ANALYSIS, state={"step": 1})
    second = cp.save(Stage.PLAN, plan={"steps": ["edit app.py"]}, pending=["edit"])
    assert (first.n, second.n) == (1, 2)
    latest = cp.latest()
    assert latest is not None
    assert latest.stage is Stage.PLAN
    assert latest.plan == {"steps": ["edit app.py"]}
    assert latest.pending == ["edit"]
    assert latest.head is not None and len(latest.head) == 40
    assert [c.stage for c in cp.all()] == [Stage.ANALYSIS, Stage.PLAN]


def test_torn_checkpoint_skipped(git_repo: Path) -> None:
    cp = Checkpointer(git_repo, "s1")
    cp.save(Stage.ANALYSIS)
    (cp.dir / "2.json").write_text('{"session_id": "s1", "n": 2, "sta')
    latest = cp.latest()
    assert latest is not None and latest.n == 1
    assert cp.save(Stage.EDIT).n == 3


def test_originals_and_modified(git_repo: Path) -> None:
    cp = Checkpointer(git_repo, "s1")
    original = (git_repo / "app.py").read_bytes()
    cp.record_original("app.py")
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    cp.record_original("app.py")  # second call keeps the first content
    cp.record_original("created.py")
    (git_repo / "created.py").write_text("x = 1\n")
    assert cp.original_content("app.py") == original
    assert cp.original_content("created.py") is None
    modified = cp.modified_files()
    assert modified["created.py"].before is None
    assert modified["app.py"].before != modified["app.py"].after
    saved = cp.save(Stage.EDIT)
    latest = cp.latest()
    assert latest is not None and latest.n == saved.n
    assert isinstance(latest.modified["app.py"], FileChange)
    assert "app.py" in latest.dirty


def test_secret_files_hash_only(git_repo: Path) -> None:
    (git_repo / ".env").write_text("API_KEY=supersecret\n")
    cp = Checkpointer(git_repo, "s1")
    digest = cp.record_original(".env")
    assert digest is not None
    assert cp.original_content(".env") is None
    stored = b"".join(p.read_bytes() for p in cp.originals_dir.iterdir())
    assert b"supersecret" not in stored


def test_state_dir_excluded(git_repo: Path, git: Callable[..., str]) -> None:
    cp = Checkpointer(git_repo, "s1")
    cp.save(Stage.ANALYSIS)
    assert ".dfm" not in git(git_repo, "status", "--porcelain")
