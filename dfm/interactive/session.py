"""The interactive session: what `dfm` does in a repository.

One Runtime serves the whole session, so the local model loads once and stays loaded
between requests (it unloads itself after `idle_unload_s` without use). Each request is a
turn. Review requests review the working-tree diff. Questions are answered by the coder
looking at the repository in read-only mode (read, search, read-only commands, never an
edit), or from repository facts when no coder is configured. Every other request runs the
agent loop, with the same policy, sandbox and verification as `dfm implement`. `!command`
runs a shell command for the developer, through the same policy and sandbox. Every turn
lands in one session trace.

Each turn's edits get their own checkpoint id (`<session>-t<n>`), so `/undo` reverts the
latest turn only, and only files that are still exactly as DFM left them. Files DFM edited
in an earlier turn are handed to the next turn's Git guard as planned, so DFM can keep
working on its own changes; a file you have edited since stays protected.
"""

from __future__ import annotations

import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from dfm.interactive.display import Display
from dfm.models.base import Model, ModelError
from dfm.orchestration.agent import AgentLoop
from dfm.orchestration.ask import ask
from dfm.orchestration.plan import plan
from dfm.orchestration.review import review
from dfm.orchestration.runtime import Runtime
from dfm.recovery import Checkpointer
from dfm.routing import CODER_ROUTES, RouteDecision, RoutingContext, is_resume_request
from dfm.security.sandbox import Sandbox
from dfm.tools.git.state import file_hash, is_git_repo
from dfm.tools.shell.runner import CommandRunner

TurnKind = Literal["task", "question", "review", "plan", "shell"]
CONTEXT_TURNS = 6  # earlier turns the coder is told about
ANSWER_STEPS = 12  # a question gets fewer steps than a change
SHELL_LINES = 200  # command output shown for `!command`

HELP = """\
Type what you want done, or ask a question about the repository.

  /diff            the working-tree diff against HEAD
  /undo            revert the files DFM changed in its latest request
  /plan <task>     plan a change without editing anything
  /review          review the working-tree diff
  !<command>       run a shell command yourself (policy and sandbox apply; not interactive)
  /ask <question>  answer by reading the code, never edit
  /do <task>       run the agent loop even if the request reads like a question
  /status          session, models and what changed
  /clear           forget earlier requests (files are not touched)
  /help            this list
  /exit            end the session (Ctrl-D also works)

Ctrl-C stops the current request; the session stays open.
Esc then Enter inserts a newline."""

COMMANDS = (
    "/diff",
    "/undo",
    "/plan",
    "/review",
    "/ask",
    "/do",
    "/status",
    "/clear",
    "/help",
    "/exit",
)


@dataclass
class Turn:
    n: int
    request: str
    kind: TurnKind
    checkpoint_id: str | None = None
    status: str = "running"
    summary: str = ""
    changed: list[str] = field(default_factory=lambda: [])
    left: dict[str, str | None] = field(default_factory=lambda: {})  # path -> sha256 DFM left
    undone: bool = False

    def line(self) -> str:
        changed = f"; changed {', '.join(self.changed)}" if self.changed else ""
        undone = " (undone by the developer)" if self.undone else ""
        return f"- {self.request} -> {self.status}: {self.summary}{changed}{undone}"


class InteractiveSession:
    def __init__(
        self,
        rt: Runtime,
        display: Display,
        *,
        coder: Model | None = None,
        max_steps: int = 25,
    ) -> None:
        self.rt = rt
        self.display = display
        self.coder = coder  # tests inject one; otherwise the router picks per request
        self.max_steps = max_steps
        self.turns: list[Turn] = []
        self.forgotten = 0  # turns before this index are not given to the coder (/clear)

    # ---------------------------------------------------------------- entry point

    async def handle(self, line: str) -> bool:
        """Run one input line. False when the session should end."""
        text = line.strip()
        if not text:
            return True
        self.display.start_turn()
        try:
            if text.startswith("/"):
                return await self._command(text)
            if text.startswith("!"):
                await self.run_shell(text[1:].strip())
                return True
            await self._request(text)
            return True
        finally:
            self.display.stop()

    async def _command(self, text: str) -> bool:
        name, _, arg = text.partition(" ")
        arg = arg.strip()
        simple: dict[str, Callable[[], None]] = {
            "/help": lambda: self.display.line(HELP),
            "/diff": self.show_diff,
            "/undo": self.undo,
            "/status": self.show_status,
            "/clear": self.clear,
        }
        needs_arg: dict[str, Callable[[str], Awaitable[None]]] = {
            "/plan": self.run_plan,
            "/ask": self.run_question,
            "/do": self.run_task,
        }
        if name in {"/exit", "/quit"}:
            return False
        if name in simple:
            simple[name]()
        elif name == "/review":
            await self.run_review()
        elif name in needs_arg:
            if not arg:
                self.display.error(f"{name} needs text, for example: {name} fix the failing test")
            else:
                await needs_arg[name](arg)
        else:
            self.display.error(f"unknown command {name}; /help lists the commands")
        return True

    async def _request(self, text: str) -> None:
        decision = await self.rt.router.route(
            text, RoutingContext(apple_available=self.rt.pool.apple is not None)
        )
        kind = self.classify(text, decision)
        if kind == "review":
            await self.run_review()
        elif kind == "question":
            await self.run_question(text, decision)
        else:
            await self.run_task(text, decision)

    @staticmethod
    def classify(text: str, decision: RouteDecision) -> TurnKind:
        """Code work goes to the agent loop; reviews and everything else are read-only."""
        if is_resume_request(text):
            return "question"
        first = decision.reasons[0] if decision.reasons else ""
        if first.startswith("review"):
            return "review"
        explains = first.startswith("repository explanation")
        if decision.route in CODER_ROUTES and decision.source != "fallback" and not explains:
            return "task"
        return "question"

    # ---------------------------------------------------------------- turns

    def _new_turn(self, request: str, kind: TurnKind) -> Turn:
        turn = Turn(len(self.turns) + 1, request, kind)
        self.turns.append(turn)
        return turn

    def _pick_coder(self, decision: RouteDecision | None) -> Model | None:
        if self.coder is not None:
            return self.coder
        try:
            return self.rt.pool.coder(decision.coder if decision and decision.coder else "primary")
        except ModelError:
            return None

    def _own_edits(self) -> set[str]:
        """Files earlier turns edited that are still exactly as DFM left them."""
        own: set[str] = set()
        for turn in self.turns:
            if turn.undone:
                continue
            for path, digest in turn.left.items():
                if file_hash(self.rt.repo / path) == digest:
                    own.add(path)
        return own

    def _earlier(self) -> str | None:
        done = [t for t in self.turns[self.forgotten :] if t.status != "running"]
        if not done:
            return None
        lines = [t.line() for t in done[-CONTEXT_TURNS:]]
        return "Earlier requests in this session (finished; for context only):\n" + "\n".join(lines)

    async def run_task(self, text: str, decision: RouteDecision | None = None) -> None:
        coder = self._pick_coder(decision)
        if coder is None:
            self.display.error(
                "No local coding model is configured, so DFM cannot edit code yet. Run "
                "`dfm setup` (outside this session) to choose one; questions still work."
            )
            return
        earlier = self._earlier()
        turn = self._new_turn(text, "task")
        turn.checkpoint_id = f"{self.rt.session.id}-t{turn.n}"
        loop = AgentLoop(
            self.rt,
            coder=coder,
            max_steps=self.max_steps,
            planned_paths=self._own_edits(),
            checkpoint_id=turn.checkpoint_id,
        )
        if earlier:
            loop.history.append(earlier)
        try:
            outcome = await loop.run(text)
        except BaseException:
            turn.status, turn.summary = "interrupted", "stopped by the developer"
            self.rt.event("note", text=f"request {turn.n} interrupted")
            if loop.services is not None:
                await loop.services.release_all()
            raise
        finally:
            changes = loop.checkpointer.modified_files()
            turn.left = {path: change.after for path, change in changes.items()}
            turn.changed = sorted(p for p, c in changes.items() if c.before != c.after)
        turn.status, turn.summary = outcome.status, outcome.summary
        self.display.outcome(outcome)

    async def run_question(self, text: str, decision: RouteDecision | None = None) -> None:
        coder = None if is_resume_request(text) else self._pick_coder(decision)
        if coder is None:
            await self._facts_answer(text, decision)
            return
        earlier = self._earlier()
        turn = self._new_turn(text, "question")
        loop = AgentLoop(
            self.rt, coder=coder, max_steps=ANSWER_STEPS, read_only=True, services=False
        )
        if earlier:
            loop.history.append(earlier)
        try:
            answer = await loop.answer(text)
        except BaseException:
            turn.status, turn.summary = "interrupted", "stopped by the developer"
            raise
        turn.status = answer.status
        turn.summary = answer.text.strip().splitlines()[0][:120] if answer.text.strip() else ""
        if answer.status == "failed":
            self.display.error(answer.text)
            return
        self.display.markdown(answer.text)
        if answer.status == "unanswered":
            self.display.note("The repository did not answer this.")

    async def _facts_answer(self, text: str, decision: RouteDecision | None) -> None:
        """No coder (or a resume question): RepoCanon and RepoKG facts, phrased by a helper."""
        turn = self._new_turn(text, "question")
        self.display.working("Reading the repository")
        answer = await ask(self.rt, text, decision)
        turn.status = "answered"
        turn.summary = answer.text.splitlines()[0][:120] if answer.text.strip() else ""
        if answer.model is None:  # the facts themselves: their line breaks carry the structure
            self.display.stop()
            self.display.line(answer.text)
        else:
            self.display.markdown(answer.text)
        self.display.note(answer.sources + (f" · {answer.model}" if answer.model else ""))
        for item in answer.degraded:
            self.display.note(f"degraded: {item}")

    async def run_shell(self, command: str) -> None:
        """`!command`: the developer's own command, classified and sandboxed like any other
        (a network or destructive command asks); output is shown, nothing is sent to a model."""
        if not command:
            self.display.error("! needs a command, for example: !git status")
            return
        turn = self._new_turn(f"!{command}", "shell")
        self.display.user_command(command)
        runner = CommandRunner(
            self.rt.env,
            self.rt.pm,
            self.rt.policy,
            on_event=self.rt.event,
            wrap=Sandbox().wrap,
            proposed_by="user",
        )
        outcome = await runner.run(command, timeout=600)
        if outcome.result is None:
            turn.status, turn.summary = "refused", outcome.verdicts[-1].reason
            return
        result = outcome.result
        turn.status, turn.summary = "ran", f"exit {result.exit_code}"
        lines = (result.stdout + result.stderr).splitlines()
        self.display.output(lines, SHELL_LINES, result.exit_code)

    async def run_plan(self, text: str) -> None:
        turn = self._new_turn(text, "plan")
        self.display.working("Planning (read-only)")
        result = await plan(self.rt, text)
        turn.status, turn.summary = "planned", f"{len(result.steps)} steps"
        self.display.markdown(result.render())
        self.display.note("Run it with /do, or describe the change in your own words.")

    async def run_review(self) -> None:
        diff = self._git("diff", "--no-color", "--no-ext-diff", "--no-textconv", "HEAD")
        if diff is None:
            return
        if not diff.strip():
            self.display.line("No changes against HEAD to review.")
            return
        turn = self._new_turn("review the diff", "review")
        self.display.working("Reviewing the diff")
        report = await review(self.rt, diff)
        turn.status, turn.summary = "reviewed", f"{len(report.findings)} findings"
        self.display.markdown(report.render())

    # ---------------------------------------------------------------- commands

    def _git(self, *args: str) -> str | None:
        if not is_git_repo(self.rt.repo):
            self.display.error("This is not a Git repository.")
            return None
        out = subprocess.run(  # noqa: S603  (fixed argv, DFM's own read-only git call)
            ["git", "-C", str(self.rt.repo), *args],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode != 0:
            self.display.error(f"git {args[0]} failed: {out.stderr.strip()[:300]}")
            return None
        return out.stdout

    def show_diff(self) -> None:
        diff = self._git("diff", "--no-color", "--no-ext-diff", "--no-textconv", "HEAD")
        if diff is None:
            return
        if not diff.strip():
            self.display.line("No changes against HEAD.")
        else:
            self.display.diff(diff.splitlines(), limit=2000)
        untracked = self._git("ls-files", "--others", "--exclude-standard")
        if untracked and untracked.strip():
            self.display.note("Untracked: " + ", ".join(untracked.split()))

    def undo(self) -> None:
        turn = next(
            (t for t in reversed(self.turns) if t.kind == "task" and t.left and not t.undone),
            None,
        )
        if turn is None or turn.checkpoint_id is None:
            self.display.line("Nothing to undo: DFM has not changed any files in this session.")
            return
        result = Checkpointer(self.rt.repo, turn.checkpoint_id).undo(turn.left)
        turn.undone = True
        self.rt.event(
            "note",
            text=f"undid request {turn.n}",
            restored=result.restored,
            removed=result.removed,
            skipped=result.skipped,
        )
        self.display.line(f"Undid request {turn.n}: {turn.request}")
        for path in result.restored:
            self.display.detail(f"restored {path}")
        for path in result.removed:
            self.display.detail(f"removed {path} (DFM had created it)")
        for item in result.skipped:
            self.display.detail(f"kept {item}", "yellow")

    def show_status(self) -> None:
        rt = self.rt
        models = rt.loaded.config.models
        lines = [f"Session {rt.session.id} · {rt.repo}"]
        for role, settings, model in (
            ("primary coder", models.primary, rt.pool.primary),
            ("small coder", models.fallback, rt.pool.small),
        ):
            if settings.model:
                state = "loaded" if getattr(model, "loaded", False) else "not loaded"
                lines.append(f"{role}: {settings.model} ({state})")
        lines.append(f"Apple FM: {'available' if rt.pool.apple is not None else 'off'}")
        lines.append(f"network: {rt.env.network.value}")
        changed = sorted({p for t in self.turns if not t.undone for p in t.changed})
        lines.append(f"requests: {len(self.turns)}; files changed: {', '.join(changed) or 'none'}")
        for line in lines:
            self.display.line(line)

    def clear(self) -> None:
        self.forgotten = len(self.turns)
        self.display.line("Earlier requests forgotten. Files are unchanged; /undo still works.")
