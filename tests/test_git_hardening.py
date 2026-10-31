import subprocess
from pathlib import Path

from dfm.tools.git.hardening import harden_git_env


def test_forces_fsmonitor_off_and_keeps_existing_config() -> None:
    env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.name", "GIT_CONFIG_VALUE_0": "x"}
    harden_git_env(env)
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert (env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]) == ("core.fsmonitor", "false")
    harden_git_env(env)  # idempotent
    assert env["GIT_CONFIG_COUNT"] == "2"


def test_a_planted_fsmonitor_does_not_run(tmp_path: Path, git_repo: Path) -> None:
    marker = tmp_path / "ran"
    hook = tmp_path / "fsmonitor.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    subprocess.run(["git", "-C", str(git_repo), "config", "core.fsmonitor", str(hook)], check=True)
    env: dict[str, str] = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    harden_git_env(env)
    subprocess.run(["git", "-C", str(git_repo), "status"], env=env, capture_output=True, check=True)
    assert not marker.exists()
