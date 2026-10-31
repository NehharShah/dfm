"""Policy vocabulary shared by every tool: what an action is and what policy decided."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal


class ActionClass(StrEnum):
    SAFE_READ = "SAFE_READ"
    SAFE_BUILD = "SAFE_BUILD"
    SAFE_LOCAL = "SAFE_LOCAL"
    LOCAL_STATE_CHANGE = "LOCAL_STATE_CHANGE"
    WORKTREE_WRITE = "WORKTREE_WRITE"
    PACKAGE_NETWORK = "PACKAGE_NETWORK"
    REMOTE_READ = "REMOTE_READ"
    REMOTE_WRITE = "REMOTE_WRITE"
    LOCAL_DESTRUCTIVE = "LOCAL_DESTRUCTIVE"
    DESTRUCTIVE = "DESTRUCTIVE"
    DESTRUCTIVE_REMOTE_WRITE = "DESTRUCTIVE_REMOTE_WRITE"
    PRIVILEGED = "PRIVILEGED"
    UNKNOWN = "UNKNOWN"


# Higher is more dangerous; a compound action takes its most dangerous part.
SEVERITY: dict[ActionClass, int] = {
    ActionClass.SAFE_READ: 0,
    ActionClass.SAFE_LOCAL: 0,
    ActionClass.SAFE_BUILD: 1,
    ActionClass.WORKTREE_WRITE: 2,
    ActionClass.LOCAL_STATE_CHANGE: 3,
    ActionClass.REMOTE_READ: 3,
    ActionClass.PACKAGE_NETWORK: 4,
    ActionClass.UNKNOWN: 5,
    ActionClass.LOCAL_DESTRUCTIVE: 6,
    ActionClass.REMOTE_WRITE: 7,
    ActionClass.DESTRUCTIVE: 8,
    ActionClass.PRIVILEGED: 9,
    ActionClass.DESTRUCTIVE_REMOTE_WRITE: 10,
}


def most_severe(classes: list[ActionClass]) -> ActionClass:
    return max(classes, key=SEVERITY.__getitem__) if classes else ActionClass.UNKNOWN


class Decision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class Confirmation(StrEnum):
    NONE = "none"
    NORMAL = "normal"  # y/N, may be remembered for this session
    HARD = "hard"  # the user types the target; never remembered


class NetworkCategory(StrEnum):
    """Keys of `[network]` in the config (other = `other_network`)."""

    REMOTE_INFERENCE = "remote_inference"
    GITHUB = "github"
    GIT_REMOTE = "git_remote"
    PACKAGE_REGISTRIES = "package_registries"
    DOCUMENTATION = "documentation"
    CONTAINER_REGISTRIES = "container_registries"
    BLOCKCHAIN_RPC_READ = "blockchain_rpc_read"
    BLOCKCHAIN_RPC_WRITE = "blockchain_rpc_write"
    KUBERNETES = "kubernetes"
    OTHER = "other_network"


ActionKind = Literal["shell", "fs", "network", "git", "model", "process"]

# Classifier reason for commands that carry or create key material (private keys,
# mnemonics, keystores). Policy denies these outright when a model proposes them.
KEY_MATERIAL = "handles private keys"


@dataclass(frozen=True)
class ProposedAction:
    kind: ActionKind
    action_class: ActionClass
    summary: str  # what the user sees, e.g. the redacted command line
    # "user" | "agent" | "model:<name>" | "tool:<name>" | "untrusted:<source>" (always asks)
    proposed_by: str
    target: str | None = None  # path, host, kube context, chain; named in hard confirmations
    network: NetworkCategory | None = None
    inside_boundary: bool = True
    reasons: tuple[str, ...] = ()

    def approval_key(self) -> str:
        return f"{self.kind}\x00{self.action_class}\x00{self.summary}\x00{self.target or ''}"


@dataclass(frozen=True)
class PolicyVerdict:
    decision: Decision
    action_class: ActionClass
    reason: str
    confirmation: Confirmation = Confirmation.NONE
    network: NetworkCategory | None = None
    approved_by: Literal["policy", "user", "session"] | None = None
    details: tuple[str, ...] = field(default=())
