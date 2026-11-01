"""The one path from a proposed command to a running process.

    classify → every part authorized by policy → [sandbox wrap]
      → ProcessManager.run → command_started / command_finished trace events

Commands may come from the developer, RepoCanon, a language adapter or a model:
all of them take this path, so none of them can skip policy.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from dfm.execution import BoundaryViolation, ExecutionEnvironment
from dfm.policy import Decision, Policy, PolicyVerdict
from dfm.policy.types import NetworkCategory, ProposedAction
from dfm.processes import OutputCallback, ProcessManager, ProcessResult
from dfm.secrets import redact_argv
from dfm.tools.errors import ToolError
from dfm.tools.kubernetes import kube_context
from dfm.tools.shell.classify import SHELL_TOKENS, Classification, classify, shell_tokens
from dfm.tools.shell.rules import option_value

Wrap = Callable[[Sequence[str], ExecutionEnvironment, Classification], Sequence[str]]


@dataclass(frozen=True)
class CommandOutcome:
    argv: tuple[str, ...]
    classification: Classification
    verdicts: tuple[PolicyVerdict, ...]
    result: ProcessResult | None  # None when policy refused

    @property
    def allowed(self) -> bool:
        return self.result is not None


EventSink = Callable[..., None]


def _noop(*args: Any, **kwargs: Any) -> None:
    return None


class CommandRunner:
    def __init__(
        self,
        env: ExecutionEnvironment,
        pm: ProcessManager,
        policy: Policy,
        *,
        proposed_by: str = "agent",
        on_event: EventSink | None = None,
        wrap: Wrap | None = None,
    ) -> None:
        self.env = env
        self.pm = pm
        self.policy = policy
        self.proposed_by = proposed_by
        self._emit: EventSink = on_event or _noop
        self.wrap = wrap

    def _with_kube_context(self, action: ProposedAction, argv: Sequence[str]) -> ProposedAction:
        """Cluster commands carry their context/namespace: shown in the approval and the trace.

        `argv` is the classified part (the kubectl command itself, also inside `bash -c`), read
        with the environment and kubeconfig the command will run with.
        """
        if action.network is not NetworkCategory.KUBERNETES:
            return action
        ctx = kube_context(
            action.target,
            kubeconfig=option_value(list(argv), "--kubeconfig"),
            environ=self.env.child_env(),
        )
        if ctx is None:
            self._emit("note", text="kube context unknown (no kubectl or no current context)")
            return action
        namespace = option_value(list(argv), "-n", "--namespace") or ctx.namespace
        where = f"{ctx.name}/{namespace}"  # the phrase a hard confirmation asks for
        self._emit("note", text=f"kube context {ctx.name}, namespace {namespace}")
        return replace(action, target=where, summary=f"[{where}] {action.summary}")

    async def run(
        self,
        command: str | Sequence[str],
        *,
        cwd: str | Path | None = None,
        timeout: float | None = None,
        secret_env: Sequence[str] = (),
        on_output: OutputCallback | None = None,
        proposed_by: str | None = None,
        pty: bool = False,
    ) -> CommandOutcome:
        try:
            tokens = shell_tokens(command)
        except ValueError as exc:
            raise ToolError(f"cannot parse command: {exc}") from exc
        if not tokens:
            raise ToolError("empty command")
        # Classification models shell semantics (pipes, redirects, `&&`); so must execution, or
        # `find . > -delete` is classified as a read and a redirect but runs `find . '>' -delete`.
        argv: tuple[str, ...]
        if any(t in SHELL_TOKENS for t in tokens):
            script = (
                command
                if isinstance(command, str)
                else " ".join(t if t in SHELL_TOKENS else shlex.quote(t) for t in tokens)
            )
            argv = ("/bin/sh", "-c", script)
        else:
            argv = tuple(shlex.split(command)) if isinstance(command, str) else tuple(command)
        workdir = self.env.cwd
        if cwd is not None:
            try:
                workdir = self.env.resolve(str(cwd))
            except BoundaryViolation as exc:
                raise ToolError(str(exc)) from exc
        classification = classify(
            command if isinstance(command, str) else list(command), inside=self.env.is_inside
        )
        verdicts: list[PolicyVerdict] = []
        actions = classification.to_actions(proposed_by or self.proposed_by)
        for part, action in zip(classification.parts, actions, strict=True):
            action = self._with_kube_context(action, part.argv)
            verdict = await self.policy.authorize(action)
            verdicts.append(verdict)
            if verdict.decision is not Decision.ALLOW:
                self._emit(
                    "command_finished",
                    argv=redact_argv(list(argv)),
                    exit_code=None,
                    refused=verdict.reason,
                    action_class=classification.action.value,
                )
                return CommandOutcome(argv, classification, tuple(verdicts), None)
        final = tuple(self.wrap(argv, self.env, classification)) if self.wrap else argv
        self._emit(
            "command_started",
            argv=redact_argv(list(argv)),
            cwd=str(workdir.relative_to(self.env.repo_root))
            if workdir.is_relative_to(self.env.repo_root)
            else str(workdir),
            action_class=classification.action.value,
        )
        try:
            result = await self.pm.run(
                final,
                self.env,
                timeout=timeout,
                cwd=workdir,
                secret_env=secret_env,
                on_output=on_output,
                pty=pty,
            )
        except FileNotFoundError:
            result = ProcessResult(final, 127, "", f"{argv[0]}: command not found", 0)
        self._emit(
            "command_finished",
            argv=redact_argv(list(argv)),
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            timed_out=result.timed_out,
        )
        return CommandOutcome(argv, classification, tuple(verdicts), result)
