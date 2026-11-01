"""Git config DFM forces on its own git processes; see `dfm.execution.git_hardening`.

Kept here for the git tool layer and tests; the implementation lives with the execution
environment, which applies it to every child process.
"""

from dfm.execution.git_hardening import FORCED, harden_git_env, harden_process

__all__ = ["FORCED", "harden_git_env", "harden_process"]
