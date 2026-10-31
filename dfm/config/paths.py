"""Where DFM keeps configuration and state (XDG layout, also on macOS)."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

APP = "dfm"
CONFIG_FILE = "config.toml"
REPO_STATE_DIR = ".dfm"


def _home(env: Mapping[str, str]) -> Path:
    home = env.get("HOME")
    return Path(home) if home else Path.home()


def _xdg(env: Mapping[str, str], var: str, fallback: Path) -> Path:
    # The XDG spec says relative values are invalid and must be ignored.
    value = env.get(var)
    if value and Path(value).is_absolute():
        return Path(value) / APP
    return fallback / APP


def config_home(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return _xdg(env, "XDG_CONFIG_HOME", _home(env) / ".config")


def data_home(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return _xdg(env, "XDG_DATA_HOME", _home(env) / ".local" / "share")


def user_config_path(env: Mapping[str, str] | None = None) -> Path:
    return config_home(env) / CONFIG_FILE


def repo_config_path(repo: Path) -> Path:
    return repo / REPO_STATE_DIR / CONFIG_FILE


@dataclass(frozen=True)
class StateDirs:
    config: Path
    data: Path
    sessions: Path
    models: Path
    cache: Path
    logs: Path
    metrics: Path
    processes: Path

    def ensure(self) -> None:
        """Create every directory, private to the user. Loading config never calls this."""
        for path in (
            self.config,
            self.data,
            self.sessions,
            self.models,
            self.cache,
            self.logs,
            self.metrics,
            self.processes,
        ):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)


def state_dirs(env: Mapping[str, str] | None = None) -> StateDirs:
    data = data_home(env)
    return StateDirs(
        config=config_home(env),
        data=data,
        sessions=data / "sessions",
        models=data / "models",
        cache=data / "cache",
        logs=data / "logs",
        metrics=data / "metrics",
        processes=data / "processes",
    )


def discover_repo_root(start: Path) -> Path:
    """Nearest ancestor holding `.git` (directory, or file for worktrees); else `start`."""
    start = start.resolve()
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return start
