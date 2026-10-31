"""Decide allow / ask / deny for a proposed action, independent of who proposed it.

`evaluate` is pure given config and the session's approvals. `authorize` adds the
human: it asks through an Approver when the verdict is ASK. Without an interactive
user, ASK means DENY.
"""

import asyncio
import re
import sys
from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

from rich.console import Console
from rich.markup import escape as rich_escape

from dfm.config.model import DFMConfig
from dfm.policy.types import (
    KEY_MATERIAL,
    ActionClass,
    Confirmation,
    Decision,
    NetworkCategory,
    PolicyVerdict,
    ProposedAction,
)

_DEFAULTS: dict[ActionClass, tuple[Decision, Confirmation]] = {
    ActionClass.SAFE_READ: (Decision.ALLOW, Confirmation.NONE),
    ActionClass.SAFE_LOCAL: (Decision.ALLOW, Confirmation.NONE),
    ActionClass.SAFE_BUILD: (Decision.ALLOW, Confirmation.NONE),
    ActionClass.WORKTREE_WRITE: (Decision.ALLOW, Confirmation.NONE),
    # Network-bound classes: the network rule decides; without a category, ask.
    ActionClass.PACKAGE_NETWORK: (Decision.ALLOW, Confirmation.NONE),
    ActionClass.REMOTE_READ: (Decision.ALLOW, Confirmation.NONE),
    ActionClass.LOCAL_STATE_CHANGE: (Decision.ASK, Confirmation.NORMAL),
    ActionClass.REMOTE_WRITE: (Decision.ASK, Confirmation.NORMAL),
    ActionClass.LOCAL_DESTRUCTIVE: (Decision.ASK, Confirmation.NORMAL),
    ActionClass.UNKNOWN: (Decision.ASK, Confirmation.NORMAL),
    ActionClass.DESTRUCTIVE: (Decision.ASK, Confirmation.HARD),
    ActionClass.DESTRUCTIVE_REMOTE_WRITE: (Decision.ASK, Confirmation.HARD),
    ActionClass.PRIVILEGED: (Decision.ASK, Confirmation.HARD),
}

_NETWORK_BOUND = {ActionClass.PACKAGE_NETWORK, ActionClass.REMOTE_READ}
_CHAIN_WRITES = {ActionClass.REMOTE_WRITE, ActionClass.DESTRUCTIVE_REMOTE_WRITE}
_CLUSTER_WRITES = {
    ActionClass.REMOTE_WRITE,
    ActionClass.DESTRUCTIVE_REMOTE_WRITE,
    ActionClass.PRIVILEGED,
}
# "prod", "production", "live", "prd" as a word in a context/namespace name
PRODUCTION = re.compile(r"(^|[^a-z0-9])(prod|production|prd|live)([^a-z0-9]|$)", re.I)
_RANK = {Decision.ALLOW: 0, Decision.ASK: 1, Decision.DENY: 2}


class Approval(StrEnum):
    ONCE = "once"
    SESSION = "session"  # only honoured for NORMAL confirmations
    DENY = "deny"


class Approver(Protocol):
    async def ask(self, action: ProposedAction, verdict: PolicyVerdict) -> Approval: ...


class NonInteractiveApprover:
    """No human present: every ASK becomes DENY."""

    async def ask(self, action: ProposedAction, verdict: PolicyVerdict) -> Approval:
        return Approval.DENY


class InteractiveApprover:
    def __init__(
        self,
        input_fn: Callable[[str], str] = input,
        console: Console | None = None,
        is_tty: Callable[[], bool] = lambda: sys.stdin.isatty(),
    ) -> None:
        self._input = input_fn
        self._console = console or Console(stderr=True)
        self._is_tty = is_tty

    async def ask(self, action: ProposedAction, verdict: PolicyVerdict) -> Approval:
        if not self._is_tty():
            return Approval.DENY
        # Everything below except the class name comes from the model or the repository: shown
        # literally, so it cannot hide or restyle part of itself in the confirmation.
        self._console.print(
            f"[bold]{action.action_class}[/bold] proposed by {_shown(action.proposed_by)}: "
            f"{_shown(action.summary)}\n  reason: {_shown(verdict.reason)}"
        )
        if verdict.confirmation is Confirmation.HARD:
            phrase = _plain(hard_phrase(action))
            typed = await asyncio.to_thread(self._input, f"Type '{phrase}' to allow once: ")
            return Approval.ONCE if typed.strip() == phrase else Approval.DENY
        answer = await asyncio.to_thread(
            self._input, "Allow? [y] once / [s] this session / [N] no: "
        )
        match answer.strip().lower():
            case "y" | "yes":
                return Approval.ONCE
            case "s" | "session":
                return Approval.SESSION
            case _:
                return Approval.DENY


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # terminal escapes, keeping \t and \n


def _plain(text: str) -> str:
    return _CONTROL.sub("?", text)


def _shown(text: str) -> str:
    return rich_escape(_plain(text))


def hard_phrase(action: ProposedAction) -> str:
    return action.target or action.action_class.value


VerdictListener = Callable[[ProposedAction, PolicyVerdict], None]


class Policy:
    def __init__(self, config: DFMConfig, approver: Approver | None = None) -> None:
        self._config = config
        self._approver: Approver = approver or NonInteractiveApprover()
        self._session_approvals: set[str] = set()
        self._listeners: list[VerdictListener] = []

    @property
    def config(self) -> DFMConfig:
        return self._config

    def subscribe(self, listener: VerdictListener) -> None:
        """Called with every final verdict (the session trace subscribes here)."""
        self._listeners.append(listener)

    def evaluate(self, action: ProposedAction) -> PolicyVerdict:
        if not action.inside_boundary:
            return PolicyVerdict(
                Decision.DENY,
                action.action_class,
                "outside the repository boundary",
                network=action.network,
                approved_by="policy",
            )

        decision, confirmation = _DEFAULTS[action.action_class]
        reason = f"{action.action_class} default"
        if action.network is not None:
            rule = Decision(getattr(self._config.network, action.network.value))
            if _RANK[rule] > _RANK[decision]:
                decision = rule
                confirmation = Confirmation.NORMAL if rule is Decision.ASK else confirmation
                reason = f"network.{action.network.value} = {rule}"
        elif action.action_class in _NETWORK_BOUND:
            decision, confirmation = Decision.ASK, Confirmation.NORMAL
            reason = f"{action.action_class} without a known network category"

        if (
            action.action_class is ActionClass.PRIVILEGED
            and KEY_MATERIAL in action.reasons
            and action.proposed_by.startswith("model")
        ):
            decision, confirmation = Decision.DENY, Confirmation.NONE
            reason = "key material on the command line, proposed by a model"
        elif (
            action.network is NetworkCategory.BLOCKCHAIN_RPC_WRITE
            and action.action_class in _CHAIN_WRITES
        ):
            decision, confirmation, reason = self._chain_write(action, decision, reason)
        if action.network is NetworkCategory.KUBERNETES and action.action_class in _CLUSTER_WRITES:
            decision, confirmation, reason = self._cluster_write(action, decision, reason)

        if (
            action.proposed_by.startswith("untrusted")
            and decision is Decision.ALLOW
            and action.action_class not in {ActionClass.SAFE_READ, ActionClass.SAFE_LOCAL}
        ):
            # derived from repository or tool output (a package name a test printed): a human
            # decides, whatever the network rules would allow on their own
            decision, confirmation = Decision.ASK, Confirmation.NORMAL
            reason = f"{reason}; proposed from untrusted output"
        if decision is Decision.DENY:
            confirmation = Confirmation.NONE
        if (
            decision is Decision.ASK
            and confirmation is Confirmation.NORMAL
            and action.approval_key() in self._session_approvals
        ):
            return PolicyVerdict(
                Decision.ALLOW,
                action.action_class,
                "approved earlier in this session",
                network=action.network,
                approved_by="session",
            )
        return PolicyVerdict(
            decision,
            action.action_class,
            reason,
            confirmation=confirmation,
            network=action.network,
            approved_by="policy" if decision is not Decision.ASK else None,
        )

    def _cluster_write(
        self, action: ProposedAction, decision: Decision, reason: str
    ) -> tuple[Decision, Confirmation, str]:
        """No silent cluster mutation: always a hard confirmation naming the context;
        an unknown or production-looking context is denied unless the user's config allows it."""
        if decision is Decision.DENY:
            return decision, Confirmation.NONE, reason
        if not action.target:
            return Decision.DENY, Confirmation.NONE, "cluster write with an unknown kube context"
        if (
            PRODUCTION.search(action.target)
            and not self._config.security.allow_production_cluster_writes
        ):
            return (
                Decision.DENY,
                Confirmation.NONE,
                f"production-looking kube context {action.target!r} "
                "(security.allow_production_cluster_writes = false)",
            )
        return Decision.ASK, Confirmation.HARD, f"cluster write to {action.target}"

    def _chain_write(
        self, action: ProposedAction, decision: Decision, reason: str
    ) -> tuple[Decision, Confirmation, str]:
        """Signing is never automatic. `network.blockchain_rpc_write` is the ceiling (deny by
        default); below it a testnet transaction asks, a mainnet or unknown one asks with a hard
        confirmation naming the chain."""
        if decision is Decision.DENY:
            return decision, Confirmation.NONE, reason
        target = action.target or "mainnet:unknown"
        if target.startswith(("testnet:", "local:")):
            return Decision.ASK, Confirmation.NORMAL, f"transaction on {target}"
        return Decision.ASK, Confirmation.HARD, f"transaction on {target} (irreversible)"

    async def authorize(self, action: ProposedAction) -> PolicyVerdict:
        verdict = self.evaluate(action)
        if verdict.decision is Decision.ASK:
            verdict = await self._ask(action, verdict)
        for listener in self._listeners:
            listener(action, verdict)
        return verdict

    async def _ask(self, action: ProposedAction, verdict: PolicyVerdict) -> PolicyVerdict:
        approval = await self._approver.ask(action, verdict)
        if approval is Approval.DENY:
            return PolicyVerdict(
                Decision.DENY,
                action.action_class,
                f"not approved ({verdict.reason})",
                network=action.network,
                approved_by="user",
            )
        if approval is Approval.SESSION and verdict.confirmation is Confirmation.NORMAL:
            self._session_approvals.add(action.approval_key())
        return PolicyVerdict(
            Decision.ALLOW,
            action.action_class,
            f"approved by user ({verdict.reason})",
            network=action.network,
            approved_by="user",
        )
