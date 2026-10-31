"""The prompt loop around InteractiveSession.

Input comes from prompt_toolkit (history, editing, Esc-Enter for a newline, slash-command
completion). A request runs as its own task: Ctrl-C cancels that task, which stops the
running command or model call and leaves the session open. Ctrl-C at the prompt clears the
line; Ctrl-D or /exit ends the session.

Approvals use the same prompt machinery, so Ctrl-C at a confirmation is a clean "no".
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory, History, InMemoryHistory
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.output import Output
from rich.console import Console

from dfm import __version__
from dfm.config import ConfigOverrides
from dfm.interactive.display import Display
from dfm.interactive.session import COMMANDS, InteractiveSession
from dfm.models.base import Model
from dfm.orchestration.runtime import Runtime
from dfm.policy import InteractiveApprover
from dfm.sessions import SessionStatus

PROMPT = "› "  # noqa: RUF001


def _history(path: Path) -> History:
    """Typed requests can hold anything: the history file is readable by the user only."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)
        path.chmod(0o600)
        return FileHistory(str(path))
    except OSError:
        return InMemoryHistory()


def _bindings() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("escape", "enter")
    def _(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    return kb


def banner(rt: Runtime, display: Display) -> None:
    models = rt.loaded.config.models
    coder = models.primary.model or models.fallback.model
    display.line(f"dfm {__version__} · {rt.repo}", "bold")
    if coder:
        display.note(f"coder: {Path(coder).name} (loads on first use, stays loaded)")
    else:
        display.line(
            "No local coding model configured: run `dfm setup` to choose one. "
            "Questions still work from repository facts.",
            "yellow",
        )
    display.note(
        f"network: {rt.env.network.value} · nothing leaves this Mac for inference · "
        "/help for commands"
    )


async def run(
    repo: Path,
    overrides: ConfigOverrides | None = None,
    *,
    console: Console | None = None,
    pt_input: Input | None = None,
    pt_output: Output | None = None,
    coder: Model | None = None,
) -> int:
    console = console or Console()
    prompts: PromptSession[str] = PromptSession(
        history=None, input=pt_input, output=pt_output, key_bindings=_bindings()
    )
    display_ref: list[Display] = []

    async def ask_developer(question: str) -> str:
        if display_ref:
            display_ref[0].stop()
        try:
            return await prompts.prompt_async(question)
        except (KeyboardInterrupt, EOFError):
            return ""  # no answer is a "no"

    approver = InteractiveApprover(console=console, ainput=ask_developer, is_tty=lambda: True)
    rt = Runtime.create(
        repo,
        request="interactive session",
        mode="interactive",
        overrides=overrides,
        approver=approver,
    )
    display = Display(console, rt.repo)
    display_ref.append(display)
    rt.listeners.append(display.event)
    session = InteractiveSession(rt, display, coder=coder)
    requests: PromptSession[str] = PromptSession(
        history=_history(rt.dirs.data / "prompt_history") if pt_input is None else None,
        input=pt_input,
        output=pt_output,
        key_bindings=_bindings(),
        completer=WordCompleter(list(COMMANDS), sentence=True),
        complete_while_typing=False,
    )
    banner(rt, display)
    loop = asyncio.get_running_loop()
    status: SessionStatus = "completed"
    try:
        while True:
            try:
                line = await requests.prompt_async(PROMPT)
            except KeyboardInterrupt:
                continue
            except EOFError:
                break
            task = asyncio.create_task(session.handle(line))
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(signal.SIGINT, task.cancel)
            try:
                keep_going = await task
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise  # the session itself is being cancelled
                display.stop()
                display.line("Stopped. Changes made so far are kept; /undo reverts them.", "yellow")
                keep_going = True
            finally:
                with contextlib.suppress(NotImplementedError, RuntimeError):
                    loop.remove_signal_handler(signal.SIGINT)
            if not keep_going:
                break
    except BaseException:
        status = "failed"
        raise
    finally:
        display.stop()
        await rt.close(status, f"{len(session.turns)} requests")
    return 0
