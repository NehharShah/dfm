import os
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from tests import repo_fixtures as fx
from tests.helpers import BUGGY, configure_fake


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return out.stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A throwaway Git repository with one commit (never the user's repository)."""
    repo = tmp_path / "gitrepo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "dfm-test@example.invalid")
    _git(repo, "config", "user.name", "DFM Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "README.md").write_text("# demo\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def git() -> Callable[..., str]:
    return _git


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    (h / ".config" / "dfm").mkdir(parents=True)
    (h / ".config" / "dfm" / "config.toml").write_text("[models.apple]\nenabled = false\n")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return h


@pytest.fixture
def calc_repo(git_repo: Path, git: Callable[..., str]) -> Path:
    """A Git repo with a tiny Python package and one failing pytest test."""
    fx.write(
        git_repo,
        {
            "pyproject.toml": '[project]\nname = "calc"\nversion = "0"\n[tool.pytest.ini_options]\npythonpath = ["src"]\n',
            "src/calc/__init__.py": "",
            "src/calc/ops.py": BUGGY,
            "tests/test_ops.py": "from calc.ops import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
            ".gitignore": ".pytest_cache/\n",
        },
    )
    git(git_repo, "add", ".")
    git(git_repo, "commit", "-qm", "calc")
    return git_repo


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`gh` on PATH serving tests/fixtures/github; returns the call log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "gh"
    script.write_text(f"#!{sys.executable}\n" + (Path(__file__).parent / "fake_gh.py").read_text())
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "gh.log"
    log.touch()
    configure_fake(
        bin_dir, fixtures=str(Path(__file__).parent / "fixtures" / "github"), log=str(log)
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return log
