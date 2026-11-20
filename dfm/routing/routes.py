"""The routes a request can take."""

from enum import StrEnum


class Route(StrEnum):
    TOOLS_ONLY = "TOOLS_ONLY"  # deterministic tools answer it; no model
    APPLE = "APPLE"  # Apple FM alone (short text work)
    SMALL_CODER = "SMALL_CODER"  # dfm-small
    PRIMARY_CODER = "PRIMARY_CODER"  # dfm-primary
    APPLE_THEN_CODER = "APPLE_THEN_CODER"  # Apple FM structures, coder does the work
    TOOLS_THEN_CODER = "TOOLS_THEN_CODER"  # tools gather facts, coder reasons
    CODER_THEN_APPLE = "CODER_THEN_APPLE"  # coder works, Apple FM summarizes


class Intent(StrEnum):
    """What the developer asked for. It picks the read-only or the editing path of a session;
    it is never a permission: every action either path takes still goes through policy."""

    INVESTIGATE = "investigate"  # explain, find, why: answered by reading, never edits
    PLAN = "plan"  # a read-only plan
    MODIFY = "modify"  # change code through the agent loop
    RUN = "run"  # run a tool such as the tests
    REVIEW = "review"  # review the diff
    GIT_CHANGE = "git_change"  # change Git state (checkout, switch)


# Intents that go to the editing path; every other intent stays read-only.
EDITING_INTENTS = frozenset({Intent.MODIFY, Intent.GIT_CHANGE})
