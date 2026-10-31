"""`gh run view --log-failed` output → the failing step, its script and its error lines.

Format (one line per log line): `<job>\t<step or UNKNOWN STEP>\t<ISO timestamp> <text>`.
Steps start with `##[group]Run <title>`; the script is echoed in cyan right after; a
failing step ends with `##[error]Process completed with exit code N`. The log is
untrusted text: callers pass it to models only inside an untrusted block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_STAMP = re.compile(r"^﻿?\d{4}-\d\d-\d\dT[\d:.]+Z ?")
# gh prints escape codes in caret notation ("^[[36;1m"); raw logs have real ESC bytes.
_ESC = r"(?:\x1b|\^\[)"
_ANSI = re.compile(_ESC + r"\[[0-9;]*m")
_SCRIPT = re.compile("^" + _ESC + r"\[36;1m(.*?)" + _ESC + r"\[0m$")


@dataclass
class FailedStep:
    job: str
    title: str  # the `##[group]Run …` title
    script: list[str] = field(default_factory=lambda: [])
    output: list[str] = field(default_factory=lambda: [])
    errors: list[str] = field(default_factory=lambda: [])
    exit_code: int | None = None
    header_done: bool = False  # the `##[group]Run` header ended: what follows is output

    def excerpt(self, lines: int = 80) -> str:
        """The end of the step's output plus its error lines, for context."""
        body = self.output[-lines:]
        extra = [e for e in self.errors if e not in body]
        return "\n".join([*body, *extra])


def parse_failed_log(text: str) -> list[FailedStep]:
    steps: dict[str, list[FailedStep]] = {}
    for raw in text.splitlines():
        parts = raw.split("\t", 2)
        if len(parts) < 3:
            continue
        job, _step, rest = parts
        line = _STAMP.sub("", rest)
        current = steps.setdefault(job, [])
        if line.startswith("##[group]Run "):
            current.append(FailedStep(job, line.removeprefix("##[group]Run ").strip()))
            continue
        if not current:
            continue
        step = current[-1]
        # The script is echoed only inside the Run header. Cyan lines after it are the step's
        # own output, which could otherwise inject commands that `dfm ci` runs locally.
        script = _SCRIPT.match(line)
        if script and not step.output and not step.header_done:
            step.script.append(script.group(1))
            continue
        clean = _ANSI.sub("", line)
        if clean.startswith("##[endgroup]") and not step.header_done:
            step.header_done = True
            continue
        if clean.startswith("##[error]"):
            message = clean.removeprefix("##[error]")
            step.errors.append(message)
            code = re.search(r"exit code (\d+)", message)
            if code:
                step.exit_code = int(code.group(1))
        elif (
            clean.startswith(("##[group]", "##[endgroup]", "shell: ", "env:", "  "))
            and not step.output
            and not step.header_done
        ):
            continue  # the step header (shell, env) before its output starts
        else:
            step.output.append(clean)
    return [s for job_steps in steps.values() for s in job_steps if s.errors]
