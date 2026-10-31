from dataclasses import replace

import pytest

from dfm.config.model import DFMConfig, NetworkSettings
from dfm.policy import (
    ActionClass,
    Approval,
    Confirmation,
    Decision,
    InteractiveApprover,
    NetworkCategory,
    Policy,
    PolicyVerdict,
    ProposedAction,
    most_severe,
)
from dfm.tools.shell.classify import classify

A = ActionClass


def action(cls: ActionClass, **kw: object) -> ProposedAction:
    fields: dict[str, object] = {
        "kind": "shell",
        "action_class": cls,
        "summary": f"cmd-{cls}",
        "proposed_by": "agent",
    }
    fields.update(kw)
    return ProposedAction(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("cls", "decision", "confirmation"),
    [
        (A.SAFE_READ, Decision.ALLOW, Confirmation.NONE),
        (A.SAFE_LOCAL, Decision.ALLOW, Confirmation.NONE),
        (A.SAFE_BUILD, Decision.ALLOW, Confirmation.NONE),
        (A.WORKTREE_WRITE, Decision.ALLOW, Confirmation.NONE),
        (A.PACKAGE_NETWORK, Decision.ASK, Confirmation.NORMAL),
        (A.REMOTE_READ, Decision.ASK, Confirmation.NORMAL),
        (A.LOCAL_STATE_CHANGE, Decision.ASK, Confirmation.NORMAL),
        (A.REMOTE_WRITE, Decision.ASK, Confirmation.NORMAL),
        (A.LOCAL_DESTRUCTIVE, Decision.ASK, Confirmation.NORMAL),
        (A.UNKNOWN, Decision.ASK, Confirmation.NORMAL),
        (A.DESTRUCTIVE, Decision.ASK, Confirmation.HARD),
        (A.DESTRUCTIVE_REMOTE_WRITE, Decision.ASK, Confirmation.HARD),
        (A.PRIVILEGED, Decision.ASK, Confirmation.HARD),
    ],
)
def test_default_table(cls: ActionClass, decision: Decision, confirmation: Confirmation) -> None:
    verdict = Policy(DFMConfig()).evaluate(action(cls))
    assert (verdict.decision, verdict.confirmation) == (decision, confirmation)


def test_every_action_class_has_a_default() -> None:
    policy = Policy(DFMConfig())
    for cls in ActionClass:
        policy.evaluate(action(cls))


def test_outside_boundary_denied_even_for_reads() -> None:
    verdict = Policy(DFMConfig()).evaluate(action(A.SAFE_READ, inside_boundary=False))
    assert verdict.decision is Decision.DENY


def config_with(**network: str) -> DFMConfig:
    return DFMConfig(network=NetworkSettings(**network))  # type: ignore[arg-type]


def test_network_rule_allow_lets_package_network_through() -> None:
    policy = Policy(config_with(package_registries="allow"))
    verdict = policy.evaluate(action(A.PACKAGE_NETWORK, network=NetworkCategory.PACKAGE_REGISTRIES))
    assert verdict.decision is Decision.ALLOW


def test_network_rule_deny_beats_class_default() -> None:
    policy = Policy(config_with(github="deny"))
    verdict = policy.evaluate(action(A.REMOTE_WRITE, network=NetworkCategory.GITHUB))
    assert verdict.decision is Decision.DENY


def test_network_allow_never_relaxes_writes() -> None:
    policy = Policy(config_with(github="allow"))
    verdict = policy.evaluate(action(A.REMOTE_WRITE, network=NetworkCategory.GITHUB))
    assert verdict.decision is Decision.ASK


def test_remote_inference_denied_by_default() -> None:
    verdict = Policy(DFMConfig()).evaluate(
        action(A.REMOTE_READ, kind="model", network=NetworkCategory.REMOTE_INFERENCE)
    )
    assert verdict.decision is Decision.DENY


def test_most_severe() -> None:
    assert most_severe([A.SAFE_READ, A.DESTRUCTIVE, A.SAFE_BUILD]) is A.DESTRUCTIVE
    assert most_severe([]) is A.UNKNOWN


class ScriptedApprover:
    def __init__(self, *answers: Approval) -> None:
        self.answers = list(answers)
        self.asked: list[ProposedAction] = []

    async def ask(self, action: ProposedAction, verdict: PolicyVerdict) -> Approval:
        self.asked.append(action)
        return self.answers.pop(0)


async def test_non_interactive_ask_becomes_deny() -> None:
    verdict = await Policy(DFMConfig()).authorize(action(A.UNKNOWN))
    assert verdict.decision is Decision.DENY


async def test_allow_never_asks() -> None:
    approver = ScriptedApprover()
    verdict = await Policy(DFMConfig(), approver).authorize(action(A.SAFE_READ))
    assert verdict.decision is Decision.ALLOW
    assert approver.asked == []


async def test_session_approval_is_scoped_to_exact_action() -> None:
    approver = ScriptedApprover(Approval.SESSION, Approval.DENY)
    policy = Policy(DFMConfig(), approver)
    first = await policy.authorize(action(A.UNKNOWN, summary="make gen"))
    again = await policy.authorize(action(A.UNKNOWN, summary="make gen"))
    other = await policy.authorize(action(A.UNKNOWN, summary="make clean"))
    assert first.decision is Decision.ALLOW
    assert again.approved_by == "session"
    assert other.decision is Decision.DENY
    assert len(approver.asked) == 2


async def test_session_approval_never_applies_to_hard_confirmations() -> None:
    approver = ScriptedApprover(Approval.SESSION, Approval.DENY)
    policy = Policy(DFMConfig(), approver)
    first = await policy.authorize(action(A.DESTRUCTIVE, target="prod"))
    second = await policy.authorize(action(A.DESTRUCTIVE, target="prod"))
    assert first.decision is Decision.ALLOW
    assert second.decision is Decision.DENY


async def test_new_policy_has_no_remembered_approvals() -> None:
    policy = Policy(DFMConfig(), ScriptedApprover(Approval.SESSION))
    await policy.authorize(action(A.UNKNOWN))
    fresh = await Policy(DFMConfig()).authorize(action(A.UNKNOWN))
    assert fresh.decision is Decision.DENY


async def test_listeners_see_final_verdicts() -> None:
    seen: list[Decision] = []
    policy = Policy(DFMConfig(), ScriptedApprover(Approval.ONCE))
    policy.subscribe(lambda a, v: seen.append(v.decision))
    await policy.authorize(action(A.SAFE_READ))
    await policy.authorize(action(A.UNKNOWN))
    await policy.authorize(action(A.SAFE_READ, inside_boundary=False))
    assert seen == [Decision.ALLOW, Decision.ALLOW, Decision.DENY]


async def test_interactive_hard_confirmation_requires_exact_target() -> None:
    typed = iter(["yes", "kind-prod"])
    approver = InteractiveApprover(input_fn=lambda _: next(typed), is_tty=lambda: True)
    policy = Policy(DFMConfig(), approver)
    act = action(A.DESTRUCTIVE_REMOTE_WRITE, target="kind-prod")
    assert (await policy.authorize(act)).decision is Decision.DENY
    assert (await policy.authorize(act)).decision is Decision.ALLOW


async def test_interactive_normal_answers() -> None:
    typed = iter(["", "y", "s"])
    approver = InteractiveApprover(input_fn=lambda _: next(typed), is_tty=lambda: True)
    policy = Policy(DFMConfig(), approver)
    assert (await policy.authorize(action(A.UNKNOWN))).decision is Decision.DENY
    assert (await policy.authorize(action(A.UNKNOWN))).decision is Decision.ALLOW
    assert (await policy.authorize(action(A.UNKNOWN))).decision is Decision.ALLOW
    assert (await policy.authorize(action(A.UNKNOWN))).approved_by == "session"


async def test_interactive_without_tty_denies() -> None:
    approver = InteractiveApprover(input_fn=lambda _: "y", is_tty=lambda: False)
    assert (await Policy(DFMConfig(), approver).authorize(action(A.UNKNOWN))).decision is (
        Decision.DENY
    )


async def test_approval_prompt_shows_model_text_literally() -> None:
    from io import StringIO

    from rich.console import Console

    out = StringIO()
    approver = InteractiveApprover(
        input_fn=lambda prompt: "n",
        console=Console(file=out, force_terminal=False, width=200),
        is_tty=lambda: True,
    )
    action = classify("rm -rf build").to_actions("model:coder")[0]
    hidden = replace(action, summary="echo safe [conceal]&& rm -rf ~[/conceal]\x1b[2K")
    verdict = Policy(DFMConfig()).evaluate(hidden)
    await approver.ask(hidden, verdict)
    shown = out.getvalue()
    assert "[conceal]&& rm -rf ~[/conceal]" in shown and "\x1b[2K" not in shown


def test_actions_from_untrusted_output_always_ask() -> None:
    network = NetworkSettings(package_registries="allow")
    policy = Policy(DFMConfig(network=network))
    (install,) = classify("npm install left-pad").to_actions("untrusted:test-output")
    verdict = policy.evaluate(install)
    assert verdict.decision is Decision.ASK and "untrusted output" in verdict.reason
    (same,) = classify("npm install left-pad").to_actions("dfm")
    assert policy.evaluate(same).decision is Decision.ALLOW  # the developer's own allow rule
