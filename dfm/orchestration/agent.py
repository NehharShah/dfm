"""The DFM engineering loop.

    analysis → plan → (act → observe → repair)* → verify → summarize

The coder model chooses one JSON action per step (read, search, patch, create,
run, test, done, give_up). DFM executes it through the same policy-checked
tools a developer's request would use: FsTools (boundary, redaction guard,
checkpoints, Git safety), CommandRunner (classification, policy, sandbox).
Refusals, patch conflicts and failing tests become observations for the next
step, not crashes. A repair budget and no-progress detection stop loops.

Claims in the final report come only from the verification ledger: a command
that ran, with its exit code. Anything not backed by evidence is listed under
"Not verified".
"""

from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, Field, field_validator

from dfm.context import ContextEngine, Task, TokenBudget
from dfm.integrations.repokg.structure import roles_from_canon, structural_diff
from dfm.models.base import (
    GenerateRequest,
    GenerateResult,
    Message,
    Model,
    ModelError,
    Reasoning,
)
from dfm.models.structured import structured
from dfm.observability.metrics import metric
from dfm.orchestration.runtime import Runtime
from dfm.policy import ActionClass
from dfm.provenance import ContextItem
from dfm.recovery import Checkpointer, Stage
from dfm.repository.intelligence import RepositoryIntelligence, gather
from dfm.secrets import redact_text
from dfm.security.injection import DATA_RULE, untrusted_block
from dfm.security.sandbox import Sandbox
from dfm.services import ServiceError, ServiceManager
from dfm.services import detect as detect_services
from dfm.sessions import SessionStatus
from dfm.tools.errors import ToolError
from dfm.tools.filesystem import FsTools, SearchReplace
from dfm.tools.generated import generated_info
from dfm.tools.git import GitGuard, GitTools
from dfm.tools.git.state import file_hash, is_git_repo
from dfm.tools.languages import (
    LanguageAdapter,
    ToolchainCommands,
    ToolCommand,
    select_adapter,
)
from dfm.tools.packages import LOCKFILES, missing_packages
from dfm.tools.results import TestReport, compress
from dfm.tools.shell.classify import classify
from dfm.tools.shell.runner import CommandRunner

Status = Literal["completed", "failed", "blocked"]
_SHELLS = {"bash", "sh", "zsh"}


def _tool(name: str, description: str, **params: str) -> dict[str, Any]:
    """A function schema. Params map to "str" or "int"; a trailing "?" marks them optional."""
    properties: dict[str, Any] = {
        "reason": {"type": "string", "description": "One sentence: why this step"}
    }
    required = ["reason"]
    for key, kind in params.items():
        properties[key] = {"type": "integer" if kind.rstrip("?") == "int" else "string"}
        if not kind.endswith("?"):
            required.append(key)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


# DFM's actions as native tool definitions: models whose chat template supports tools answer in
# the call format they were trained on (Qwen: <tool_call><function=...>), parsed back to actions.
ACTION_TOOLS: tuple[dict[str, Any], ...] = (
    _tool(
        "read",
        "Read a file, optionally lines start..end (1-based).",
        path="str",
        start="int?",
        end="int?",
    ),
    _tool("search", "Search the repository with a regex.", pattern="str"),
    _tool(
        "patch",
        "Replace exact existing text (enough lines to be unique) in a file.",
        path="str",
        search="str",
        replace="str",
    ),
    _tool("create", "Create a new file with this full content.", path="str", content="str"),
    _tool("run", "Run a read-only investigation or build command.", command="str"),
    _tool("test", "Run the repository's tests (DFM picks the command)."),
    _tool("done", "The request is satisfied and the tests pass.", summary="str"),
    _tool("give_up", "You are blocked; say what is missing.", summary="str"),
)
THINKING_BUDGET = {"low": 512, "medium": 1536, "high": 3072}  # tokens per step
HISTORY_CHARS = 16_000  # beyond this, the oldest steps are compacted to one line each
HISTORY_KEEP_CHARS = 8_000  # what compaction keeps verbatim


class _Metered:
    """The coder, counting the output tokens of a whole task (its token budget)."""

    def __init__(self, model: Model) -> None:
        self.model = model
        self.name = model.name
        self.tokens_out = 0

    async def healthcheck(self) -> Any:
        return await self.model.healthcheck()

    async def generate(self, request: GenerateRequest) -> GenerateResult:
        result = await self.model.generate(request)
        self.tokens_out += result.tokens_out or 0
        return result

    def stream(self, request: GenerateRequest) -> Any:
        return self.model.stream(request)


def _changes_per_step(item: ContextItem, modified: set[str]) -> bool:
    return item.source in {"git", "test"} or (item.path is not None and item.path in modified)


class AgentAction(BaseModel):
    action: Literal["read", "search", "patch", "create", "run", "test", "done", "give_up"]
    reason: str = Field(description="One sentence: why this step")
    path: str | None = Field(default=None, description="File path for read / patch / create")
    start: int | None = Field(default=None, description="First line to read (1-based)")
    end: int | None = Field(default=None, description="Last line to read")
    pattern: str | None = Field(default=None, description="Regex for search")
    search: str | None = Field(default=None, description="Exact existing text to replace (patch)")
    replace: str | None = Field(default=None, description="New text (patch)")
    content: str | None = Field(default=None, description="Full content of a new file (create)")
    command: str | None = Field(default=None, description="Shell command (run)")
    summary: str | None = Field(
        default=None, description="What was done or why stopping (done / give_up)"
    )

    @field_validator("command", mode="before")
    @classmethod
    def _argv_command(cls, value: Any) -> Any:
        """Models send argv lists too; `["bash", "-lc", X]` is X, so X itself gets classified."""
        if not isinstance(value, list):
            return value
        items = cast(list[object], value)
        if not all(isinstance(v, str) for v in items):
            return items  # pydantic reports the bad type
        argv = [str(v) for v in items]
        if len(argv) == 3 and Path(argv[0]).name in _SHELLS and argv[1] in {"-c", "-lc"}:
            return argv[2]
        return shlex.join(argv)


INSTRUCTIONS = f"""You are the coding worker inside DFM, a local engineering agent. You fix and change code in a real repository by choosing ONE action per turn, answered as a JSON object.

Actions:
- read: path, optional start/end lines. Read before you patch.
- search: pattern (regex) to find code.
- patch: path, search (exact text copied from the file, enough lines to be unique), replace.
- create: path, content (new files only).
- test: run the repository's tests (DFM picks the right command).
- run: command (read-only investigation or build commands; destructive ones are refused).
- done: summary, when the request is satisfied and tests pass.
- give_up: summary, when you are blocked; say what is missing.

Rules:
- Make the smallest change that satisfies the request. Do not reformat unrelated code.
- Never write text such as [REDACTED] into files; ask for the real value instead (give_up).
- If a patch conflicts, read the file again and use the exact current text.
- Edit generator inputs, not generated files.
- Do not claim success: DFM verifies by running the tests itself.
{DATA_RULE}"""

ANSWER_INSTRUCTIONS = f"""You are the read-only assistant inside DFM, a local engineering agent. You answer the developer's question about a real repository by looking at it, choosing ONE action per turn, answered as a JSON object.

Actions:
- read: path, optional start/end lines.
- search: pattern (regex) to find code.
- run: command (read-only commands only, such as ls, git log, git status, grep).
- test: run the repository's tests (read-only; DFM picks the command).
- done: summary = your complete answer to the developer, in Markdown. Cite files as path:line.
- give_up: summary, when the repository does not contain the answer; say what is missing.

Rules:
- Look before you answer: read or search the files the question is about, and answer from what you saw, not from memory.
- If the request is a shell command (for example `ls` or `git status`), run it and give its result.
- You cannot change anything here. If the developer wants a change, describe it; they can ask for it next.
{DATA_RULE}"""

_ANSWER_ACTIONS = {"read", "search", "run", "test"}
ANSWER_TOOLS: tuple[dict[str, Any], ...] = (
    *(t for t in ACTION_TOOLS if t["function"]["name"] in _ANSWER_ACTIONS),
    _tool("done", "Your complete answer to the developer, in Markdown.", summary="str"),
    _tool(
        "give_up", "The repository does not contain the answer; say what is missing.", summary="str"
    ),
)


@dataclass(frozen=True)
class Answer:
    status: Literal["answered", "unanswered", "failed"]
    text: str
    steps: int


@dataclass(frozen=True)
class Evidence:
    claim: str
    command: str
    exit_code: int | None
    ok: bool
    detail: str = ""


@dataclass
class VerificationLedger:
    entries: list[Evidence] = field(default_factory=lambda: [])

    def record(
        self, claim: str, command: str, exit_code: int | None, ok: bool, detail: str = ""
    ) -> Evidence:
        evidence = Evidence(claim, command, exit_code, ok, detail)
        self.entries.append(evidence)
        return evidence

    def verified(self) -> list[Evidence]:
        return [e for e in self.entries if e.ok]

    def failed(self) -> list[Evidence]:
        return [e for e in self.entries if not e.ok]


@dataclass
class TaskOutcome:
    status: Status
    summary: str
    session_id: str
    changed: list[str] = field(default_factory=lambda: [])
    verified: list[Evidence] = field(default_factory=lambda: [])
    not_verified: list[str] = field(default_factory=lambda: [])
    architecture: str | None = None
    risks: list[str] = field(default_factory=lambda: [])
    steps: int = 0

    @property
    def session_status(self) -> SessionStatus:
        return "aborted" if self.status == "blocked" else self.status

    def report(self) -> str:
        """The final report: changed files, verification, what was not verified, risks."""

        def section(title: str, items: list[str]) -> list[str]:
            return [title, *(f"- {i}" for i in items)] if items else [title, "- (none)"]

        lines = [f"Status: {self.status}: {self.summary}", ""]
        lines += section("Changed", self.changed)
        lines += [
            "",
            *section(
                "Verified",
                [f"{e.claim}: `{e.command}` (exit {e.exit_code})" for e in self.verified],
            ),
        ]
        lines += ["", "Architecture", f"- {self.architecture or 'not analyzed'}"]
        lines += ["", *section("Not verified", self.not_verified)]
        lines += ["", *section("Files", self.changed)]
        lines += ["", *section("Risks", self.risks)]
        return "\n".join(lines)


def _signature(report: TestReport) -> str:
    return (
        "|".join(sorted(f"{f.id}:{f.message[:80]}" for f in report.failed))
        + "|"
        + "|".join(report.errors[:3])
    )


# Ways to turn a red check green without fixing anything (CI mode refuses edits adding them).
_SKIP_MARKERS = re.compile(
    r"@pytest\.mark\.(skip|xfail)|pytest\.(skip|xfail)\(|@unittest\.skip|\b(it|test|describe)\.(skip|todo|only)\("
    r"|\bx(it|describe|test)\(|#\[ignore\]|\bt\.Skip(Now|f)?\(|XCTSkip|vm\.skip\("
)
_WORKFLOW_WEAKENERS = re.compile(
    r"continue-on-error:\s*true|\|\|\s*(true|exit 0)|^\s*if:\s*false", re.M
)


_DATA_ACTIONS = {"read", "search", "run", "test"}


def weakens_checks(path: str, before: str, after: str) -> str | None:
    """Why an edit would silence a failing check instead of fixing it (None if it does not)."""
    if len(_SKIP_MARKERS.findall(after)) > len(_SKIP_MARKERS.findall(before)):
        return "adds a skip/xfail/only marker"
    if path.startswith(".github/workflows/") and len(_WORKFLOW_WEAKENERS.findall(after)) > len(
        _WORKFLOW_WEAKENERS.findall(before)
    ):
        return "makes a workflow step unable to fail"
    return None


def _focus_paths(report: TestReport | None) -> list[str]:
    if report is None:
        return []
    paths = [f.file for f in report.failed if f.file]
    return list(dict.fromkeys(p for p in paths if p and not p.startswith("/")))


class AgentLoop:
    def __init__(
        self,
        rt: Runtime,
        *,
        coder: Model | None = None,
        max_steps: int = 25,
        repair_budget: int = 5,
        context_tokens: int = 6000,
        sandbox: bool = True,
        planned_paths: set[str] | None = None,
        read_only: bool = False,
        target: ToolCommand | None = None,
        forbid_skips: bool = False,
        services: bool = True,
        allow_generated_edits: bool = False,
        checkpoint_id: str | None = None,
    ) -> None:
        self.rt = rt
        self.target = target  # a command that must pass (the failing CI step in `dfm ci`)
        self.forbid_skips = forbid_skips
        self.allow_generated_edits = allow_generated_edits
        self.proposed: set[tuple[str, str]] = set()  # missing packages already handled
        self.missing: list[str] = []  # missing packages that were not installed
        self.lockfiles: dict[str, str | None] = {}
        self.services = (
            ServiceManager(
                rt.env, rt.pm, rt.policy, rt.dirs.data, on_event=rt.event, sandbox=sandbox
            )
            if services
            else None
        )
        self.coder: _Metered | None = _Metered(coder) if coder is not None else None
        self.max_steps = max_steps
        config = rt.loaded.config
        # auto: medium, high right after a failure
        self.reasoning: Literal["auto", "low", "medium", "high"] = config.models.reasoning
        self.struggling = False  # the last step failed: think harder on the next one
        self.token_budget = config.limits.task_output_tokens
        self.compacted: list[str] = []  # one line per step that left the verbatim history
        self.repair_budget = repair_budget
        self.context_tokens = context_tokens
        self.ledger = VerificationLedger()
        self.history: list[str] = []
        # one id per task: an interactive session runs several, and undoes them one at a time
        self.checkpointer = Checkpointer(rt.repo, checkpoint_id or rt.session.id)
        self.guard = GitGuard(rt.repo) if is_git_repo(rt.repo) else None
        if self.guard is not None and planned_paths:
            self.guard.plan_paths(planned_paths)
        self.fs = FsTools(
            rt.env,
            rt.policy,
            checkpointer=self.checkpointer,
            on_event=rt.event,
            guard=self.guard.check_write if self.guard else None,
            proposed_by="model:coder",
        )
        self.read_only = read_only
        sandboxer = Sandbox(enabled=sandbox, read_only_repo=read_only)
        # read-only mode runs repository code (tests) only where the sandbox keeps it read-only
        self.can_run_read_only = sandboxer.enabled
        wrap = sandboxer.wrap
        self.runner = CommandRunner(
            rt.env, rt.pm, rt.policy, on_event=rt.event, wrap=wrap, proposed_by="model:coder"
        )
        self.dfm_runner = CommandRunner(
            rt.env, rt.pm, rt.policy, on_event=rt.event, wrap=wrap, proposed_by="dfm"
        )
        self.git = GitTools(rt.env, rt.pm, rt.policy) if self.guard else None
        self.adapter: LanguageAdapter | None = None
        self.commands: ToolchainCommands | None = None
        self.intel: RepositoryIntelligence | None = None
        self.last_report: TestReport | None = None

    # ---------------------------------------------------------------- tools DFM itself runs

    async def run_tests(self, command: ToolCommand | None = None) -> TestReport | None:
        if self.adapter is None or self.commands is None or self.commands.test is None:
            return None
        cmd = command or self.commands.test
        if self.read_only and not self.can_run_read_only:
            report = TestReport("not run", ran=False)
            report.errors.append(
                "tests not run: the repository cannot be kept read-only here (no sandbox, or "
                "already inside one), and plan mode changes nothing"
            )
            self.last_report = report
            return report
        self.rt.event("test_started", argv=list(cmd.argv))
        outcome = await self.dfm_runner.run(cmd.argv, cwd=cmd.cwd, timeout=600)
        if outcome.result is None:
            report = TestReport("refused", ran=False)
            report.errors.append(f"policy refused {cmd}: {outcome.verdicts[-1].reason}")
        else:
            report = self.adapter.parse_tests(outcome.result, self.commands)
            report.localize(self.rt.repo, cmd.cwd)
            if not report.ok and await self.handle_missing_packages(
                outcome.result.stdout + outcome.result.stderr
            ):
                return await self.run_tests(command)  # something was installed: run again
        self.rt.event(
            "test_finished",
            argv=list(cmd.argv),
            ok=report.ok,
            passed=report.passed,
            failed=len(report.failed),
            ran=report.ran,
        )
        self.last_report = report
        return report

    def _generated_refusal(self, rel: str) -> str | None:
        """Generated files are edited through their source (unless explicitly overridden)."""
        if self.allow_generated_edits:
            return None
        path = self.rt.repo / rel
        if not path.is_file():
            return None
        info = generated_info(path, self.rt.repo)
        if info is None:
            return None
        self.rt.event("note", text=f"refused edit to generated {rel}", sources=info.sources)
        return f"refused: {info.advice()}. Use `run` for the regeneration command."

    async def handle_missing_packages(self, output: str) -> bool:
        """Propose installing packages the output says are missing (policy asks); True when
        something was installed. Refused or offline: recorded and reported, work continues."""
        installed = False
        for proposal in missing_packages(output, self.rt.repo):
            key = (proposal.ecosystem, proposal.package)
            if key in self.proposed:
                continue
            self.proposed.add(key)
            self.rt.event("note", text=proposal.describe(), package=proposal.package)
            if self.read_only:  # plan mode changes nothing: report, never install
                self.missing.append(f"{proposal.describe()}: not installed (read-only mode)")
                continue
            if proposal.command is None:
                self.missing.append(proposal.describe())
                continue
            # the package name comes from test output: untrusted, so never auto-allowed
            outcome = await self.dfm_runner.run(
                proposal.command, timeout=900, proposed_by="untrusted:test-output"
            )
            if outcome.result is None:
                self.missing.append(
                    f"{proposal.describe()}: not installed ({outcome.verdicts[-1].reason})"
                )
                self.history.append(
                    f"The package {proposal.package} is missing and installing it was not approved. "
                    "Do not vendor or stub it; work on what does not need it."
                )
            elif outcome.result.exit_code != 0:
                self.missing.append(
                    f"{proposal.describe()}: install failed (exit {outcome.result.exit_code})"
                )
            else:
                installed = True
                self.history.append(
                    f"Installed {proposal.package} with `{' '.join(proposal.command)}`."
                )
        return installed

    async def ensure_services(self) -> None:
        """Start (or reuse) local services the tests need; refusals and failures are reported."""
        if self.services is None:
            return
        hints: list[str] = []
        if self.rt.reflex.enabled:
            hints = [p.text for p in await self.rt.reflex.practices(self.rt.repo)]
        detection = detect_services(self.rt.repo, hints)
        for note in detection.skipped:
            self.rt.event("note", text=note)
        for spec in detection.services:
            try:
                handle = await self.services.ensure(spec)
            except ServiceError as exc:
                self.missing.append(f"service {spec.name} ({spec.kind}:{spec.port}): {exc}")
                self.rt.event("failure", stage="services", error=str(exc), logs=exc.logs[-500:])
                continue
            how = "already running" if not handle.owned else f"started ({handle.method})"
            self.history.append(f"Service {spec.kind} on {handle.url}: {how}.")
            self.rt.event("note", text=f"service {spec.kind}:{spec.port} {how}", owned=handle.owned)

    async def run_target(self) -> tuple[bool, str]:
        """Run the target command; (passed, output tail)."""
        assert self.target is not None  # noqa: S101
        outcome = await self.dfm_runner.run(self.target.argv, cwd=self.target.cwd, timeout=900)
        if outcome.result is None:
            return False, f"refused by policy: {outcome.verdicts[-1].reason}"
        tail = (outcome.result.stdout + outcome.result.stderr)[-2000:]
        return outcome.result.exit_code == 0, f"exit {outcome.result.exit_code}\n{tail}"

    async def _run_check(self, claim: str, command: ToolCommand | None) -> None:
        if command is None:
            return
        outcome = await self.dfm_runner.run(command.argv, cwd=command.cwd, timeout=600)
        result = outcome.result
        if result is None:
            self.ledger.record(claim, str(command), None, False, "refused by policy")
            return
        self.ledger.record(
            claim,
            str(command),
            result.exit_code,
            result.exit_code == 0,
            (result.stdout + result.stderr)[-400:],
        )

    # ---------------------------------------------------------------- stages

    async def analyze(self, request: str) -> str | None:
        if self.guard is not None:
            blocking = self.guard.start()
            if blocking:
                return "; ".join(blocking)
        self.intel = await gather(self.rt.repo, self.rt.canon, self.rt.graph)
        self.lockfiles = {name: file_hash(self.rt.repo / name) for name in LOCKFILES}
        await self.ensure_services()
        canon = self.intel.canon
        self.adapter = select_adapter(self.rt.repo, canon)
        if self.adapter is not None:
            self.commands = self.adapter.commands(self.rt.repo, canon)
        self.rt.event("repocanon_loaded", state=self.intel.freshness.get("RepoCanon"))
        self.rt.event("repokg_loaded", state=self.intel.freshness.get("RepoKG"))
        baseline = await self.run_tests()
        if baseline is not None:
            self.history.append(
                "Baseline tests:\n" + untrusted_block("test-output", compress(baseline))
            )
        self.checkpointer.save(
            Stage.ANALYSIS, state={"baseline": baseline.summary() if baseline else None}
        )
        return None

    def _context(self, request: str) -> list[ContextItem]:
        failures: list[ContextItem] = []
        if self.last_report is not None and not self.last_report.ok:
            failures.append(
                ContextItem.make(
                    "test", compress(self.last_report), "latest test result", priority=2
                )
            )
        return failures

    def _compact_history(self) -> None:
        """Past HISTORY_CHARS, fold the oldest steps into one line each. Unlike a sliding window
        this keeps every step's gist, and the history changes only at a compaction, so the
        runtime's prefix cache keeps matching between compactions."""
        if sum(len(h) for h in self.history) <= HISTORY_CHARS:
            return
        while self.history and sum(len(h) for h in self.history) > HISTORY_KEEP_CHARS:
            oldest = self.history.pop(0)
            self.compacted.append("- " + oldest.splitlines()[0][:200])

    async def _next_action(
        self,
        request: str,
        step: int,
        *,
        instructions: str = INSTRUCTIONS,
        tools: tuple[dict[str, Any], ...] = ACTION_TOOLS,
    ) -> AgentAction:
        if self.coder is None:
            raise ModelError(
                "no coding model available (run `dfm setup` to configure a local MLX coder)"
            )
        extra = [self.rt.reflex.collector(self.rt.repo)] if self.rt.reflex.enabled else []
        engine = ContextEngine(self.rt.env, self.intel, extra=extra)
        focus = list(dict.fromkeys([*_focus_paths(self.last_report), *sorted(self.fs.modified)]))
        bundle = await engine.build(
            Task(request, failures=self._context(request), focus_paths=focus),
            TokenBudget(self.context_tokens),
        )
        self.rt.event("context_selected", items=bundle.provenance())
        commands = ""
        if self.commands is not None:
            commands = f"Test command: {self.commands.test}\nCheck command: {self.commands.check}\n"
        self._compact_history()
        earlier = (
            "Earlier steps (compacted):\n" + "\n".join(self.compacted) + "\n\n"
            if self.compacted
            else ""
        )
        history = earlier + "\n\n".join(self.history)
        # Every byte for the model is redacted here: context items already are, but tool
        # observations in the history carry file contents and command output too.
        # Least to most changeable, one message each: the runtime keeps the KV cache at the end
        # of every message but the last, so a step re-encodes only what changed since the last.
        # The diff, the latest test result and files DFM edited change with every patch: they go
        # in the last message, so editing does not invalidate the repository context.
        changed = [i for i in bundle.items if _changes_per_step(i, self.fs.modified)]
        stable = "\n\n".join(i.render() for i in bundle.items if i not in changed)
        task = redact_text(f"Task: {request}\n\n{commands}Repository context:\n{stable}")
        so_far = redact_text(f"What happened so far:\n{history or '(nothing yet)'}")
        now = "\n\n".join(i.render() for i in changed)
        turn = redact_text(
            (f"Current state:\n{now}\n\n" if now else "")
            + f"This is step {step} of {self.max_steps}. Choose the next action."
        )
        level: Reasoning
        if self.reasoning == "auto":
            level = "high" if self.struggling else "medium"
        else:
            level = self.reasoning
        self.rt.event("model_started", model=self.coder.name, step=step, reasoning=level)
        started = time.monotonic()
        action = await structured(
            self.coder,
            GenerateRequest(
                messages=(Message("user", task), Message("user", so_far), Message("user", turn)),
                instructions=instructions,
                purpose="code",
                timeout_s=300,
                reasoning=level,
                thinking_budget=THINKING_BUDGET[level],
                tools=tools,
            ),
            AgentAction,
        )
        self.rt.event(
            "model_finished",
            model=self.coder.name,
            step=step,
            action=action.action,
            target=action.path or action.pattern or action.command,
            reason=action.reason,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return action

    async def _execute(self, action: AgentAction) -> str:
        a = action.action
        if a == "read":
            if not action.path:
                return "error: read needs a path"
            sl = await self.fs.read_lines(action.path, action.start or 1, action.end)
            if sl.stub:
                return sl.text
            numbered = "\n".join(
                f"{n:>5} {line}" for n, line in enumerate(sl.text.split("\n"), sl.start)
            )
            total = sl.total_lines or "a large file (streamed)"
            return f"{sl.path} lines {sl.start}-{sl.end} of {total}:\n{numbered}"
        if a == "search":
            if not action.pattern:
                return "error: search needs a pattern"
            matches = await self.fs.search_text(action.pattern, limit=40)
            return "\n".join(f"{m.path}:{m.line}: {m.text}" for m in matches) or "no matches"
        if (
            a in {"create", "patch"}
            and action.path
            and (refusal := self._generated_refusal(action.path))
        ):
            return refusal
        if a == "create":
            if not action.path or action.content is None:
                return "error: create needs path and content"
            if self.forbid_skips and (why := weakens_checks(action.path, "", action.content)):
                self.rt.event("note", text=f"refused edit to {action.path}: {why}")
                return f"refused: this edit {why}. Fix the cause of the failure instead."
            await self.fs.create_file(action.path, action.content)
            self.rt.graph.mark_stale(self.rt.repo)
            self.checkpointer.save(Stage.EDIT, state={"action": "create", "path": action.path})
            return f"created {action.path}"
        if a == "patch":
            if not action.path or action.search is None or action.replace is None:
                return "error: patch needs path, search and replace"
            if self.forbid_skips and (
                why := weakens_checks(action.path, action.search, action.replace)
            ):
                self.rt.event("note", text=f"refused edit to {action.path}: {why}")
                return f"refused: this edit {why}. Fix the cause of the failure instead."
            result = await self.fs.patch_file(
                action.path, [SearchReplace(action.search, action.replace)]
            )
            if not result.applied:
                near = "\n\n".join(result.candidates[:2])
                return f"patch not applied: {result.conflict}" + (
                    f"\nClosest current text:\n{near}" if near else ""
                )
            self.rt.graph.mark_stale(self.rt.repo)
            self.checkpointer.save(Stage.EDIT, state={"action": "patch", "path": action.path})
            notes = f" ({'; '.join(result.notes)})" if result.notes else ""
            return f"patched {action.path}{notes}"
        if a == "run":
            if not action.command:
                return "error: run needs a command"
            outcome = await self.runner.run(action.command, timeout=300)
            if outcome.result is None:
                return (
                    f"refused by policy: {outcome.verdicts[-1].reason}. Do not retry this command."
                )
            out = (outcome.result.stdout + outcome.result.stderr)[-3000:]
            return f"exit {outcome.result.exit_code}\n{out}"
        if a == "test":
            report = await self.run_tests()
            if report is None:
                return "no test command known for this repository"
            self.checkpointer.save(Stage.TEST, state={"summary": report.summary()})
            return compress(report)
        return ""

    async def _verify(self) -> tuple[list[str], list[str], str | None]:
        not_verified: list[str] = []
        risks: list[str] = []
        if self.commands is None or self.commands.test is None:
            not_verified.append("tests: no test command found for this repository")
        else:
            report = await self.run_tests()
            assert report is not None  # noqa: S101
            ok = report.ok
            self.ledger.record(
                "tests pass" if ok else "tests run",
                str(self.commands.test),
                report.exit_code,
                ok,
                report.summary(),
            )
            if not ok:
                not_verified.append(f"tests: {report.summary()}")
            self.rt.event("verification", check="tests", ok=ok, detail=report.summary())
        if self.target is not None:
            passed, tail = await self.run_target()
            self.ledger.record(
                "CI step passes", str(self.target), 0 if passed else 1, passed, tail[-400:]
            )
            if not passed:
                not_verified.append(f"CI step `{self.target}` still fails")
            self.rt.event("verification", check="ci-step", ok=passed, detail=tail[-300:])
        if self.commands is not None:
            await self._run_check("type check / compile", self.commands.check)
            await self._run_check("lint", self.commands.lint)
            for e in self.ledger.entries[-2:]:
                if not e.ok and e.claim in {"type check / compile", "lint"}:
                    not_verified.append(f"{e.claim}: `{e.command}` exit {e.exit_code}")
        architecture: str | None = None
        if self.intel is not None and self.intel.graph is not None and self.fs.modified:
            after = await self.rt.graph.refresh(self.rt.repo, force=True)
            if after is not None:
                delta = structural_diff(self.intel.graph, after, roles_from_canon(self.intel.canon))
                architecture = delta.markdown()
                risks += [f"{w.edge}: {w.reason}" for w in delta.boundary_warnings]
        elif self.fs.modified:
            not_verified.append("architecture: RepoKG unavailable, structural impact not analyzed")
        if self.guard is not None:
            problems = self.guard.end_check(self.fs.modified)
            risks += problems
        changed_locks = [
            name
            for name, before in self.lockfiles.items()
            if file_hash(self.rt.repo / name) != before
        ]
        risks += [
            f"lockfile changed: {name} (review the dependency changes)" for name in changed_locks
        ]
        not_verified += self.missing
        self.checkpointer.save(
            Stage.VERIFICATION, state={"ledger": [e.__dict__ for e in self.ledger.entries]}
        )
        return not_verified, risks, architecture

    # ---------------------------------------------------------------- main

    def _observe(self, step: int, action: AgentAction, observation: str) -> None:
        self.struggling = observation.startswith(("error", "refused", "patch not applied"))
        shown = observation[:1500]
        if action.action in _DATA_ACTIONS and not observation.startswith("refused"):
            # file contents, search hits and command output are data, never instructions
            shown = untrusted_block(f"{action.action}-result", shown)
        self.history.append(f"Step {step}: {action.action} ({action.reason})\n{shown}")

    async def answer(self, question: str) -> Answer:
        """Answer a question by looking at the repository: read, search, read-only commands
        and (read-only) tests; never an edit. Use with `read_only=True, services=False`."""
        self.rt.event("user_request", text=question)
        self.intel = await gather(self.rt.repo, self.rt.canon, self.rt.graph)
        self.adapter = select_adapter(self.rt.repo, self.intel.canon)
        if self.adapter is not None:
            self.commands = self.adapter.commands(self.rt.repo, self.intel.canon)
        for step in range(1, self.max_steps + 1):
            if self.coder is not None and self.coder.tokens_out >= self.token_budget:
                return Answer("failed", f"token budget used up ({self.coder.tokens_out})", step)
            try:
                action = await self._next_action(
                    question, step, instructions=ANSWER_INSTRUCTIONS, tools=ANSWER_TOOLS
                )
            except ModelError as exc:
                self.rt.event("failure", stage="answer", error=f"coding model failed: {exc}")
                return Answer("failed", f"coding model failed: {exc}", step)
            if action.action in {"done", "give_up"}:
                status = "answered" if action.action == "done" else "unanswered"
                return Answer(status, action.summary or action.reason, step)
            if action.action not in _ANSWER_ACTIONS:
                observation = "refused: nothing can be changed while answering a question"
            elif action.action == "run" and not self._read_only_command(action.command or ""):
                observation = "refused: only read-only commands run while answering a question"
            else:
                try:
                    observation = await self._execute(action)
                except ToolError as exc:
                    observation = f"error: {exc}"
            self._observe(step, action, observation)
        return Answer("failed", f"no answer after {self.max_steps} steps", self.max_steps)

    def _read_only_command(self, command: str) -> bool:
        return classify(command, inside=self.rt.env.is_inside).action is ActionClass.SAFE_READ

    async def run(self, request: str) -> TaskOutcome:
        sid = self.rt.session.id
        self.rt.event("user_request", text=request)
        blocked = await self.analyze(request)
        if blocked:
            return TaskOutcome("blocked", blocked, sid)
        self.checkpointer.save(
            Stage.PLAN,
            plan={"goal": request, "test": str(self.commands.test) if self.commands else None},
        )

        repairs = 0
        no_progress = 0
        last_signature: str | None = None
        summary = ""
        finished: Literal["done", "give_up", "budget", "model", "no_progress"] = "budget"
        steps = 0
        for step in range(1, self.max_steps + 1):
            steps = step
            if self.coder is not None and self.coder.tokens_out >= self.token_budget:
                finished = "budget"
                summary = f"token budget used up ({self.coder.tokens_out} output tokens)"
                break
            try:
                action = await self._next_action(request, step)
            except ModelError as exc:
                summary = f"coding model failed: {exc}"
                self.rt.event("failure", stage="act", error=summary)
                finished = "model"
                break
            if action.action == "done" or action.action == "give_up":
                summary = action.summary or action.reason
                finished = "done" if action.action == "done" else "give_up"
                break
            try:
                observation = await self._execute(action)
            except ToolError as exc:
                observation = f"error: {exc}"
            self._observe(step, action, observation)
            if action.action in {"patch", "create"} and observation.startswith(
                ("patched", "created")
            ):
                if self.target is not None:
                    passed, tail = await self.run_target()
                    self.history.append(
                        f"`{self.target}` after the edit:\n"
                        + untrusted_block("ci-step-output", tail[-1200:])
                    )
                    if passed and self.commands is None:
                        continue
                report = await self.run_tests()
                if report is not None:
                    self.history.append(
                        "Tests after the edit:\n" + untrusted_block("test-output", compress(report))
                    )
                    self.struggling = not report.ok
                    if not report.ok:
                        repairs += 1
                        metric("agent.repairs", 1)
                        signature = _signature(report)
                        no_progress = no_progress + 1 if signature == last_signature else 0
                        last_signature = signature
                        self.checkpointer.save(
                            Stage.REPAIR, state={"repairs": repairs, "summary": report.summary()}
                        )
                        if no_progress >= 2:
                            finished = "no_progress"
                            summary = "the same failure persisted after three edits"
                            break
                        if repairs > self.repair_budget:
                            finished = "budget"
                            summary = f"repair budget ({self.repair_budget}) used up"
                            break
                        if no_progress == 1:
                            self.history.append(
                                "The last edit did not change the failure. Re-read the failing code and try a different approach."
                            )

        metric("agent.steps", steps)
        not_verified, risks, architecture = await self._verify()
        if self.services is not None:
            await self.services.release_all()
        changed = sorted(self.fs.modified)
        tests_ok = any(e.claim == "tests pass" for e in self.ledger.verified())
        target_ok = self.target is None or any(
            e.claim == "CI step passes" for e in self.ledger.verified()
        )
        no_tests = self.commands is None or self.commands.test is None
        failed_checks = [
            e.claim
            for e in self.ledger.entries
            if not e.ok and e.claim in {"type check / compile", "lint"}
        ]
        if finished == "done" and target_ok and (tests_ok or no_tests) and not failed_checks:
            status: Status = "completed"
        else:
            status = "failed"
            if finished == "budget" and not summary:
                summary = f"stopped after {self.max_steps} steps"
            if finished == "done" and failed_checks and (tests_ok or no_tests) and target_ok:
                summary = f"the coder reported done, but {' and '.join(failed_checks)} failed"
            elif finished == "done" and not (tests_ok and target_ok):
                summary = f"the coder reported done, but verification failed ({summary})"
        if not changed and status == "completed":
            not_verified.append("no files were changed")
        return TaskOutcome(
            status,
            summary,
            sid,
            changed,
            self.ledger.verified(),
            not_verified,
            architecture,
            risks,
            steps,
        )


def outcome_payload(outcome: TaskOutcome) -> dict[str, Any]:
    return {
        "status": outcome.status,
        "summary": outcome.summary,
        "changed": outcome.changed,
        "verified": [e.__dict__ for e in outcome.verified],
        "not_verified": outcome.not_verified,
        "risks": outcome.risks,
        "steps": outcome.steps,
        "session": outcome.session_id,
    }


def repo_relative(path: Path, repo: Path) -> str:
    return path.relative_to(repo).as_posix() if path.is_relative_to(repo) else str(path)
