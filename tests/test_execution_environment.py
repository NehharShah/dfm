import os
from pathlib import Path

import pytest

from dfm.config import ConfigOverrides
from dfm.execution import BoundaryViolation, ExecutionEnvironment, scrub_env
from dfm.policy.network import NetworkMode
from dfm.tools.shell.classify import classify
from tests.helpers import make_env


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    (h / ".ssh").mkdir(parents=True)
    (h / ".ssh" / "id_rsa").write_text("KEY")
    (h / ".aws").mkdir()
    (h / "elsewhere").mkdir()
    (h / "elsewhere" / "notes.txt").write_text("hi")
    return h


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    (r / "src" / "a.py").write_text("x = 1\n")
    (r / ".git").mkdir()
    return r


def make(
    repo: Path,
    home: Path,
    environ: dict[str, str] | None = None,
    overrides: ConfigOverrides | None = None,
) -> ExecutionEnvironment:
    return make_env(
        repo, home.parent, home=home, environ=environ, overrides=overrides, session_id="s1"
    )


def test_resolves_inside_repo(repo: Path, home: Path) -> None:
    env = make(repo, home)
    assert env.resolve("src/a.py") == Path(os.path.realpath(repo / "src/a.py"))
    assert env.resolve(repo / "src") == Path(os.path.realpath(repo / "src"))
    assert env.resolve("new/file.txt", write=True).name == "file.txt"


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "~/elsewhere/notes.txt"])
def test_outside_paths_rejected(repo: Path, home: Path, path: str) -> None:
    with pytest.raises(BoundaryViolation):
        make(repo, home).resolve(path)


def test_symlink_escape_rejected(repo: Path, home: Path) -> None:
    (repo / "keys").symlink_to(home / ".ssh")
    (repo / "notes").symlink_to(home / "elsewhere")
    env = make(repo, home)
    with pytest.raises(BoundaryViolation, match="protected"):
        env.resolve("keys/id_rsa")
    with pytest.raises(BoundaryViolation, match="outside"):
        env.resolve("notes/notes.txt")


def test_protected_paths_cannot_be_approved(repo: Path, home: Path) -> None:
    env = make(repo, home)
    with pytest.raises(BoundaryViolation):
        env.approve_path("~/.ssh")
    env.approve_path("~")  # approving the parent still leaves .ssh denied
    assert env.resolve("~/elsewhere/notes.txt").name == "notes.txt"
    with pytest.raises(BoundaryViolation):
        env.resolve("~/.ssh/id_rsa")
    with pytest.raises(BoundaryViolation):
        env.resolve("~/.aws/credentials")


def test_git_internals_not_writable(repo: Path, home: Path) -> None:
    env = make(repo, home)
    assert env.resolve(".git/HEAD").name == "HEAD"
    with pytest.raises(BoundaryViolation, match="Git internals"):
        env.resolve(".git/hooks/pre-commit", write=True)


def test_state_dirs_inside_boundary(repo: Path, home: Path) -> None:
    env = make(repo, home)
    assert env.is_inside(str(env.tmp / "scratch.txt"))


def test_scrub_env() -> None:
    environ = {
        "PATH": "/bin",
        "HOME": "/h",
        "CARGO_HOME": "/h/.cargo",
        "AWS_SECRET_ACCESS_KEY": "x",
        "GITHUB_TOKEN": "x",
        "OPENAI_API_KEY": "x",
        "DATABASE_URL": "postgres://u:p@h/db",
        "UV_INDEX_URL": "https://u:p@idx",
        "UV_PUBLISH_TOKEN": "x",
        "RANDOM_TOOL_SETTING": "x",
        "LC_ALL": "C",
    }
    assert scrub_env(environ) == {
        "PATH": "/bin",
        "HOME": "/h",
        "CARGO_HOME": "/h/.cargo",
        "LC_ALL": "C",
    }


def test_child_env(repo: Path, home: Path) -> None:
    env = make(repo, home, environ={"GH_TOKEN": "ghp_secret", "LANG": "en_US.UTF-8"})
    child = env.child_env()
    assert "GH_TOKEN" not in child
    assert child["TMPDIR"] == str(env.tmp)
    assert child["LANG"] == "en_US.UTF-8"
    assert child["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert "HF_HUB_OFFLINE" not in child
    assert env.child_env(["GH_TOKEN"])["GH_TOKEN"] == "ghp_secret"


def test_which_searches_the_child_path(repo: Path, home: Path, tmp_path: Path) -> None:
    """A presence check must agree with what a subprocess then runs: the environment's PATH,
    not DFM's own (they differ in tests, and when the developer's shell has extra tools)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool = bin_dir / "dfm-only-here"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    env = make(repo, home, environ={"PATH": f"{bin_dir}:/usr/bin:/bin"})
    assert env.which("dfm-only-here") == str(tool)
    assert env.which("sh") is not None
    assert make(repo, home).which("dfm-only-here") is None  # not on that PATH


def test_offline_env_hints(repo: Path, home: Path) -> None:
    env = make(repo, home, overrides=ConfigOverrides(offline=True))
    assert env.network is NetworkMode.OFFLINE
    child = env.child_env()
    assert child["UV_OFFLINE"] == "1"
    assert child["CARGO_NET_OFFLINE"] == "true"
    assert child["HF_HUB_OFFLINE"] == "1"


def test_tmp_cleanup(repo: Path, home: Path) -> None:
    env = make(repo, home)
    with env.session():
        (env.tmp / "x").write_text("1")
        assert env.tmp.exists()
    assert not env.tmp.exists()


def test_classifier_uses_environment_predicate(repo: Path, home: Path) -> None:
    env = make(repo, home)
    inside = classify(f"cat {repo}/src/a.py", inside=env.is_inside)
    assert inside.outside_boundary == ()
    leak = classify("cat ~/.ssh/id_rsa", inside=env.is_inside)
    assert leak.outside_boundary == ("~/.ssh/id_rsa",)
