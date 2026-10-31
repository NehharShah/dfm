"""Git config DFM forces on its own git processes.

DFM runs git outside the sandbox (status, diff, worktrees, checkpoints). Repository code can't
plant code in `.git/config` during a session (the sandbox denies it), but a read such as
`git status` would otherwise run any `core.fsmonitor` command found there, as the developer.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping

FORCED = {"core.fsmonitor": "false"}


def harden_git_env(environ: MutableMapping[str, str]) -> None:
    """Add FORCED to git's environment config (GIT_CONFIG_COUNT/KEY_n/VALUE_n), keeping any set."""
    count = int(environ.get("GIT_CONFIG_COUNT", "0") or 0)
    present = {environ.get(f"GIT_CONFIG_KEY_{i}") for i in range(count)}
    for key, value in FORCED.items():
        if key in present:
            continue
        environ[f"GIT_CONFIG_KEY_{count}"] = key
        environ[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    environ["GIT_CONFIG_COUNT"] = str(count)


def harden_process() -> None:
    harden_git_env(os.environ)
