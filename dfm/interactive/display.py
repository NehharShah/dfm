"""What the interactive session shows while it works.

`Display.event` is a Runtime listener: every step the agent takes (the action the model
chose, reads, edits with their diff, commands, test results, policy refusals) is printed as
it happens, with a spinner while DFM waits on the model or a command. Model-controlled text
is printed literally (no Rich markup), like the approval prompt does.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.status import Status
from rich.text import Text

from dfm.orchestration.agent import TaskOutcome
from dfm.secrets import is_secret_path

_VERBS = {
    "read": "Read",
    "search": "Search",
    "patch": "Edit",
    "create": "Create",
    "run": "Run",
    "test": "Test",
}
DIFF_LINES = 40  # per edit; the full diff is one `/diff` away
_MAX_SNAPSHOT = 512 * 1024


def _one_line(text: object, limit: int = 120) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


class Display:
    def __init__(self, console: Console, repo: Path) -> None:
        self.console = console
        self.repo = repo
        self._status: Status | None = None
        self._before: dict[str, str] = {}  # path -> content when an edit started
        self._in_tests = False
        self._acted = False  # the model has chosen an action in this turn
        self._last_action: str | None = None

    # ---------------------------------------------------------------- spinner

    def working(self, text: str) -> None:
        if not self.console.is_terminal:
            return
        if self._status is None:
            self._status = self.console.status(text, spinner="dots")
            self._status.start()
        else:
            self._status.update(text)

    def stop(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None

    def start_turn(self) -> None:
        self._acted = False
        self._last_action = None
        self._in_tests = False
        self._before.clear()

    # ---------------------------------------------------------------- output

    def line(self, text: str | Text, style: str = "") -> None:
        self.console.print(text, style=style, markup=False, highlight=False)

    def note(self, text: str) -> None:
        self.line(text, "dim")

    def error(self, text: str) -> None:
        self.stop()
        self.line(text, "red")

    def markdown(self, text: str) -> None:
        self.stop()
        self.console.print(Markdown(text))

    def step(self, verb: str, target: str = "", reason: str = "") -> None:
        out = Text("● ", style="bold")
        out.append(verb, style="bold")
        if target:
            out.append(" " + _one_line(target, 100))
        if reason:
            out.append("  " + _one_line(reason, 90), style="dim")
        self.line(out)

    def detail(self, text: str, style: str = "dim") -> None:
        self.line(Text("  ⎿ ", style="dim") + Text(text, style=style))

    # ---------------------------------------------------------------- runtime events

    def event(self, event_type: str, payload: dict[str, Any]) -> None:
        handler = getattr(self, "_on_" + event_type, None)
        if handler is not None:
            handler(payload)

    def _on_context_selected(self, p: dict[str, Any]) -> None:
        self.working("Reading the repository")

    def _on_model_started(self, p: dict[str, Any]) -> None:
        step, effort = p.get("step"), p.get("reasoning")
        if step:
            self.working(f"Thinking · step {step}" + (f" · {effort} effort" if effort else ""))
        else:
            self.working(f"Thinking · {p.get('model', 'model')}")

    def _on_model_finished(self, p: dict[str, Any]) -> None:
        action = p.get("action")
        if action not in _VERBS:
            return  # done / give_up are the outcome; helper calls have no action
        self.stop()
        self._acted = True
        self._last_action = action
        target = p.get("target") or ("the repository's tests" if action == "test" else "")
        self.step(_VERBS[action], str(target), str(p.get("reason") or ""))

    def _on_test_started(self, p: dict[str, Any]) -> None:
        self._in_tests = True
        if not self._acted:
            self.stop()
            self.step("Baseline tests", " ".join(p.get("argv", [])))
        self.working("Running tests")

    def _on_test_finished(self, p: dict[str, Any]) -> None:
        self._in_tests = False
        self.stop()
        if not p.get("ran", True):
            self.detail("tests did not run", "yellow")
            return
        passed, failed = p.get("passed", 0), p.get("failed", 0)
        ok = bool(p.get("ok"))
        self.detail(f"{passed} passed, {failed} failed", "green" if ok else "red")

    def _on_command_started(self, p: dict[str, Any]) -> None:
        if self._in_tests:
            return
        command = " ".join(p.get("argv", []))
        if self._last_action != "run":  # DFM's own commands (services, installs)
            self.stop()
            self.detail("$ " + _one_line(command, 110))
        self.working("Running " + _one_line(command, 60))

    def _on_command_finished(self, p: dict[str, Any]) -> None:
        if p.get("refused"):
            self.stop()
            self.detail("refused by policy: " + str(p["refused"]), "red")
            return
        if self._in_tests or self._last_action != "run":
            return
        self.stop()
        code = p.get("exit_code")
        style = "dim" if code == 0 else "red"
        self.detail(f"exit {code}" + (" (timed out)" if p.get("timed_out") else ""), style)
        self._last_action = None

    def _on_patch_started(self, p: dict[str, Any]) -> None:
        path = str(p.get("path", ""))
        target = self.repo / path
        if (
            path
            and not is_secret_path(path)
            and target.is_file()
            and target.stat().st_size <= _MAX_SNAPSHOT
        ):
            self._before[path] = target.read_text(errors="replace")

    def _on_patch_finished(self, p: dict[str, Any]) -> None:
        self.stop()
        path = str(p.get("path", ""))
        if p.get("applied") is False:
            self.detail("not applied: " + _one_line(p.get("conflict", ""), 100), "yellow")
            return
        kind = p.get("kind")
        if kind == "delete":
            self.detail(f"deleted {path}", "yellow")
            return
        if kind == "move":
            self.detail(f"moved {p.get('source')} → {path}")
            return
        target = self.repo / path
        if is_secret_path(path) or not target.is_file():
            self.detail(f"wrote {path}")
            return
        after = target.read_text(errors="replace")
        before = self._before.pop(path, None)
        if before is None:
            self.detail(f"wrote {path} ({len(after.splitlines())} lines)")
            return
        self.diff(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=1))

    def diff(self, lines: Iterable[str], limit: int = DIFF_LINES) -> None:
        shown = 0
        hidden = 0
        for raw in lines:
            if raw.startswith(("---", "+++")):
                continue
            if shown >= limit:
                hidden += 1
                continue
            style = "green" if raw.startswith("+") else "red" if raw.startswith("-") else "dim"
            self.line(Text("    " + raw, style=style))
            shown += 1
        if hidden:
            self.note(f"    … {hidden} more lines (/diff shows everything)")

    def _on_failure(self, p: dict[str, Any]) -> None:
        self.stop()
        self.detail(_one_line(p.get("error", "failed"), 160), "red")

    def _on_note(self, p: dict[str, Any]) -> None:
        if p.get("text"):
            self.stop()
            self.detail(_one_line(p["text"], 140))

    def _on_verification(self, p: dict[str, Any]) -> None:
        self.working("Verifying")

    # ---------------------------------------------------------------- results

    def outcome(self, outcome: TaskOutcome) -> None:
        """The same facts as `TaskOutcome.report()`, laid out for a terminal."""
        self.stop()
        mark, style = {
            "completed": ("✓", "green"),
            "failed": ("✗", "red"),
            "blocked": ("■", "yellow"),
        }[outcome.status]
        head = Text(f"{mark} {outcome.status}", style=f"bold {style}")
        head.append(f": {outcome.summary}" if outcome.summary else "", style="")
        self.console.print()
        self.line(head)
        if outcome.changed:
            self.line(Text("Changed  ", style="bold") + Text(", ".join(outcome.changed)))
        if outcome.verified:
            self.line(Text("Verified", style="bold"))
            for e in outcome.verified:
                self.line(Text(f"  {e.claim}: `{e.command}` (exit {e.exit_code})", style="green"))
        if outcome.not_verified:
            self.line(Text("Not verified", style="bold"))
            for item in outcome.not_verified:
                self.line(Text(f"  {item}", style="yellow"))
        if outcome.risks:
            self.line(Text("Risks", style="bold"))
            for item in outcome.risks:
                self.line(Text(f"  {item}", style="yellow"))
