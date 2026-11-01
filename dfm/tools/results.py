"""Normalized test results and compiler diagnostics.

Repair loops need structure (which test, which file and line, which message),
not five thousand lines of log. Parsers here are deterministic; language
adapters pick one by reporter. `ran = False` means the test command
itself failed (missing tool, import/collection error, crash): a different
problem from tests that ran and failed, and the summary says so.
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast

from dfm.processes import ProcessResult

Severity = Literal["error", "warning", "note"]


@dataclass(frozen=True)
class Diagnostic:
    severity: Severity
    message: str
    file: str | None = None
    line: int | None = None
    column: int | None = None
    code: str | None = None
    tool: str = ""

    @property
    def location(self) -> str:
        if self.file is None:
            return "?"
        parts = [self.file] + [str(p) for p in (self.line, self.column) if p is not None]
        return ":".join(parts)


@dataclass(frozen=True)
class FailedTest:
    id: str
    message: str
    file: str | None = None
    line: int | None = None
    trace: str = ""


_SKIP_DIRS = {".git", ".build", "node_modules", "target", ".venv", "build", "dist", ".dfm"}


def _unique_match(base: Path, name: str) -> Path | None:
    """The single file called `name` under `base` (None when absent or ambiguous)."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        if name in filenames:
            found.append(Path(dirpath) / name)
            if len(found) > 1:
                return None
    return found[0] if found else None


def repo_path(path: str, repo: Path, cwd: str = ".") -> str:
    """`path` relative to the repository when it lies inside it; unchanged otherwise.

    A bare file name that does not exist where the tool ran (Swift Testing and go
    print only the base name) resolves to its unique match below that directory.
    """
    path = path.removeprefix("file://")
    root = repo.resolve()
    candidate = Path(path) if path.startswith("/") else root / cwd / path
    if not path.startswith("/") and "/" not in path and not candidate.exists():
        candidate = _unique_match(root / cwd, path) or candidate
    try:
        return candidate.resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return path


@dataclass
class TestReport:
    framework: str
    passed: int = 0
    failed: list[FailedTest] = field(default_factory=lambda: [])
    skipped: int = 0
    errors: list[str] = field(default_factory=lambda: [])  # collection/setup errors
    duration_s: float | None = None
    ran: bool = True
    exit_code: int | None = None
    command_missing: bool = False  # the test tool itself is not installed here

    __test__ = False  # not a pytest test class

    def localize(self, repo: Path, cwd: str = ".") -> TestReport:
        """Rewrite failure paths as repo-relative (vitest and node report absolute ones)."""
        self.failed = [
            replace(f, file=repo_path(f.file, repo, cwd)) if f.file else f for f in self.failed
        ]
        return self

    @property
    def ok(self) -> bool:
        return self.ran and not self.failed and not self.errors and (self.exit_code in (0, None))

    @property
    def total(self) -> int:
        return self.passed + len(self.failed) + self.skipped

    def summary(self) -> str:
        if not self.ran:
            return (
                f"{self.framework}: test command did not run tests (exit {self.exit_code}): "
                + "; ".join(self.errors)[:300]
            )
        parts = [f"{self.passed} passed", f"{len(self.failed)} failed"]
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        if self.errors:
            parts.append(f"{len(self.errors)} errors")
        return f"{self.framework}: " + ", ".join(parts)


# ---------------------------------------------------------------- pytest / unittest

# "=== 1 failed, 2 passed in 0.12s ===" (default) or "1 failed, 1 passed in 0.01s" (-q)
_PYTEST_SUMMARY = re.compile(
    r"^(?:=+ )?((?:\d+ \w+(?:, )?)+|no tests ran) in ([\d.]+)s(?: \([^)]*\))?(?: =+)?$", re.M
)
_PYTEST_COUNTS = re.compile(
    r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed|deselected|warnings?)"
)
_PYTEST_FAILED = re.compile(r"^FAILED (\S+?)(?: - (.*))?$", re.M)
_PYTEST_ERROR = re.compile(r"^ERROR (\S+?)(?: - (.*))?$", re.M)
_PY_LOCATION = re.compile(r"^(\S+\.py):(\d+): ", re.M)


def parse_pytest(text: str, exit_code: int | None = None) -> TestReport:
    report = TestReport("pytest", exit_code=exit_code)
    summaries = _PYTEST_SUMMARY.findall(text)
    if summaries:
        counts, duration = summaries[-1]
        report.duration_s = float(duration)
        for number, kind in _PYTEST_COUNTS.findall(counts):
            if kind == "passed":
                report.passed = int(number)
            elif kind == "skipped":
                report.skipped = int(number)
    sections = _pytest_failure_sections(text)
    for test_id, message in _PYTEST_FAILED.findall(text):
        body = sections.get(test_id.split("::")[-1], "")
        loc = _PY_LOCATION.findall(body)
        file, line = (loc[-1][0], int(loc[-1][1])) if loc else (test_id.split("::")[0], None)
        report.failed.append(FailedTest(test_id, message or "failed", file, line, body))
    for target, message in _PYTEST_ERROR.findall(text):
        report.errors.append(f"{target}: {message}".strip(": "))
    if "no tests ran" in text or (exit_code in (4, 5) and not report.passed and not report.failed):
        report.ran = report.passed > 0
        if exit_code == 5:
            report.errors.append("no tests collected")
    if exit_code not in (0, 1, None) and not report.failed and not report.passed:
        report.ran = False
        if not report.errors:
            report.errors.append(_tail(text))
    if "ERROR collecting" in text or "errors during collection" in text:
        report.errors = report.errors or ["collection error"]
    return report


def _pytest_failure_sections(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in re.finditer(
        r"^_{3,} (?:\S+\.)?(\S+?) _{3,}$\n(.*?)(?=^_{3,} |^=+ )", text, re.M | re.S
    ):
        out[match.group(1).split(".")[-1]] = match.group(2).strip()
    return out


_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in ([\d.]+)s", re.M)
_UNITTEST_RESULT = re.compile(r"^(OK|FAILED)(?: \((.*)\))?$", re.M)
_UNITTEST_FAIL = re.compile(r"^(FAIL|ERROR): (\S+) \(([^)]+)\)", re.M)


def parse_unittest(text: str, exit_code: int | None = None) -> TestReport:
    report = TestReport("unittest", exit_code=exit_code)
    ran = _UNITTEST_RAN.search(text)
    if not ran:
        report.ran = False
        report.errors.append(_tail(text))
        return report
    total = int(ran.group(1))
    report.duration_s = float(ran.group(2))
    result = _UNITTEST_RESULT.search(text)
    details = dict(re.findall(r"(\w+)=(\d+)", result.group(2) or "")) if result else {}
    skipped = int(details.get("skipped", 0))
    for kind, name, where in _UNITTEST_FAIL.findall(text):
        block = text.split(f"{kind}: {name} ({where})", 1)[1]
        block = re.sub(r"^\s*-{70}\n", "", block)
        block = re.split(r"\n(?:-{70}|={70})", block, maxsplit=1)[0]
        last = [line for line in block.strip().splitlines() if line.strip()]
        loc = re.findall(r'File "([^"]+)", line (\d+)', block)
        file, line = (loc[-1][0], int(loc[-1][1])) if loc else (None, None)
        test_id = (
            where if where.endswith(f".{name}") else f"{where}.{name}"
        )  # 3.12+ prints the full id
        report.failed.append(
            FailedTest(test_id, last[-1] if last else kind, file, line, block.strip())
        )
    report.skipped = skipped
    report.passed = max(0, total - len(report.failed) - skipped)
    return report


# ---------------------------------------------------------------- go test -json


def parse_go_test_json(text: str, exit_code: int | None = None) -> TestReport:
    report = TestReport("go test", exit_code=exit_code)
    outputs: dict[str, list[str]] = {}
    build_errors: list[str] = []
    any_event = False
    for line in text.splitlines():
        if not line.startswith("{"):
            if line.strip():
                build_errors.append(line)
            continue
        try:
            event = cast(dict[str, Any], json.loads(line))
        except json.JSONDecodeError:
            continue
        any_event = True
        test = event.get("Test")
        action = event.get("Action")
        if action == "build-output":  # go >= 1.24: compiler output as events, no Package key
            text_out = str(event.get("Output", "")).rstrip()
            if text_out and not text_out.startswith("#"):
                build_errors.append(text_out)
            continue
        key = f"{event.get('Package', '')}.{test}" if test else None
        if action == "output" and key:
            outputs.setdefault(key, []).append(str(event.get("Output", "")))
        elif action == "pass" and key:
            report.passed += 1
        elif action == "skip" and key:
            report.skipped += 1
        elif action == "fail" and key:
            out = "".join(outputs.get(key, []))
            loc = re.findall(r"(\S+_test\.go):(\d+):", out)
            file, lineno = (loc[0][0], int(loc[0][1])) if loc else (None, None)
            message = next(
                (ln.strip() for ln in out.splitlines() if re.search(r"_test\.go:\d+:", ln)), "FAIL"
            )
            report.failed.append(FailedTest(key, message, file, lineno, out))
        elif action == "fail" and not test and event.get("FailedBuild"):
            report.errors.append(f"package {event.get('Package')} did not build")
        elif action == "fail" and not test and event.get("Output") is None and not report.failed:
            report.errors.append(f"package {event.get('Package')} failed (build error?)")
    if not any_event:
        report.ran = False
    if build_errors and (not report.passed and not report.failed):
        report.ran = False
        report.errors.extend(build_errors[:20])
    return report


# ---------------------------------------------------------------- cargo test

_CARGO_RESULT = re.compile(
    r"test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored"
)
_CARGO_PANIC = re.compile(r"panicked at ([^:\s]+):(\d+):\d+:\n(.*)")


def parse_cargo_test(text: str, exit_code: int | None = None) -> TestReport:
    report = TestReport("cargo test", exit_code=exit_code)
    results = _CARGO_RESULT.findall(text)
    if not results:
        report.ran = False
        report.errors.append(_tail(text))
        return report
    for passed, _failed, ignored in results:
        report.passed += int(passed)
        report.skipped += int(ignored)
    for name in re.findall(r"^test (\S+) \.\.\. FAILED$", text, re.M):
        section = re.search(
            rf"^---- {re.escape(name)} stdout ----\n(.*?)(?=^---- |\Z|^failures:)",
            text,
            re.M | re.S,
        )
        body = section.group(1).strip() if section else ""
        panic = _CARGO_PANIC.search(body)
        if panic:
            report.failed.append(
                FailedTest(name, panic.group(3).strip(), panic.group(1), int(panic.group(2)), body)
            )
        else:
            report.failed.append(
                FailedTest(name, body.splitlines()[-1] if body else "FAILED", trace=body)
            )
    return report


# ---------------------------------------------------------------- jest / vitest JSON


def parse_jest_json(text: str, exit_code: int | None = None, framework: str = "jest") -> TestReport:
    report = TestReport(framework, exit_code=exit_code)
    start = text.find("{")
    try:
        data = cast(dict[str, Any], json.loads(text[start:])) if start != -1 else {}
    except json.JSONDecodeError:
        data = {}
    if "numTotalTests" not in data:
        report.ran = False
        report.errors.append(_tail(text))
        return report
    report.passed = int(data.get("numPassedTests", 0))
    report.skipped = int(data.get("numPendingTests", 0)) + int(data.get("numTodoTests", 0))
    for suite in cast(list[dict[str, Any]], data.get("testResults", [])):
        file = suite.get("name")
        if suite.get("status") == "failed" and not suite.get("assertionResults"):
            report.errors.append(f"{file}: {str(suite.get('message', ''))[:300]}")
        for case in cast(list[dict[str, Any]], suite.get("assertionResults", [])):
            if case.get("status") != "failed":
                continue
            messages = cast(list[str], case.get("failureMessages", []))
            body = "\n".join(messages)
            loc: Any = case.get("location")
            line = cast(dict[str, Any], loc).get("line") if isinstance(loc, dict) else None
            if line is None and file:  # vitest omits locations by default; the stack has it
                frame = re.search(rf"{re.escape(str(file))}:(\d+):\d+", body)
                line = int(frame.group(1)) if frame else None
            first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "failed")
            report.failed.append(
                FailedTest(str(case.get("fullName") or case.get("title")), first, file, line, body)
            )
    return report


# ---------------------------------------------------------------- forge test

_FORGE_CASE = re.compile(r"^\[(PASS|FAIL(?:[.:][^\]]*)?)\] (\S+)\(", re.M)
_FORGE_SUITE_HEAD = re.compile(r"^Ran \d+ tests? for (?P<file>[^:\s]+):(?P<contract>\w+)", re.M)
_FORGE_SUITE = re.compile(r"Suite result: \w+\. (\d+) passed; (\d+) failed; (\d+) skipped")


def parse_forge_test(text: str, exit_code: int | None = None) -> TestReport:
    report = TestReport("forge test", exit_code=exit_code)
    suites = _FORGE_SUITE.findall(text)
    if not suites:
        report.ran = False
        report.errors.append(_tail(text))
        return report
    for passed, _failed, skipped in suites:
        report.passed += int(passed)
        report.skipped += int(skipped)
    seen: set[str] = set()
    recap = text.find("Failing tests:")
    heads = list(_FORGE_SUITE_HEAD.finditer(text[: recap if recap != -1 else len(text)]))
    for case in _FORGE_CASE.finditer(text):
        status, name = case.group(1), case.group(2)
        if recap != -1 and case.start() > recap:
            break  # failures are repeated in a recap
        if status.startswith("FAIL") and name not in seen:
            seen.add(name)
            suite = next((h for h in reversed(heads) if h.start() < case.start()), None)
            reason = status[5:].strip() if len(status) > 4 else "failed"
            report.failed.append(
                FailedTest(name, reason or "failed", suite.group("file") if suite else None)
            )
    return report


# ---------------------------------------------------------------- Cairo: snforge and scarb cairo-test

_SNFORGE_CASE = re.compile(r"^\[(?P<status>PASS|FAIL|IGNORE|SKIP)\] (?P<name>\S+)", re.M)
_SNFORGE_TOTALS = re.compile(
    r"^Tests: (\d+) passed, (\d+) failed, (?:(\d+) skipped, )?(\d+) ignored", re.M
)
_CAIRO_CASE = re.compile(r"^test (?P<name>\S+) \.\.\. (?P<status>ok|fail|ignored)\b", re.M)
_CAIRO_PANIC = re.compile(r"^\s+(?P<name>\S+) - (?P<msg>.+)$", re.M)


def parse_cairo_test(text: str, exit_code: int | None = None) -> TestReport:
    """snforge (`[PASS] pkg::test`) and Scarb's cairo-test (`test pkg::t ... fail`)."""
    text = strip_ansi(text)
    report = TestReport("cairo test", exit_code=exit_code)
    if _SNFORGE_TOTALS.search(text) or _SNFORGE_CASE.search(text):
        report.framework = "snforge"
        cases = list(_SNFORGE_CASE.finditer(text))
        for i, case in enumerate(cases):
            status, name = case.group("status"), case.group("name")
            if status == "PASS":
                report.passed += 1
            elif status in {"IGNORE", "SKIP"}:
                report.skipped += 1
            else:
                end = cases[i + 1].start() if i + 1 < len(cases) else len(text)
                block = text[case.end() : end]
                data = re.search(r"Failure data:\s*\n\s*(.+)", block)
                report.failed.append(
                    FailedTest(
                        name, data.group(1).strip() if data else "failed", trace=block.strip()
                    )
                )
        return report
    cases = list(_CAIRO_CASE.finditer(text))
    if not cases:
        report.ran = False
        report.errors.append(_tail(text))
        return report
    panics = {m.group("name"): m.group("msg") for m in _CAIRO_PANIC.finditer(text)}
    for case in cases:
        if case.group("status") == "ok":
            report.passed += 1
        elif case.group("status") == "ignored":
            report.skipped += 1
        else:
            name = case.group("name")
            report.failed.append(FailedTest(name, panics.get(name, "failed")))
    return report


# ---------------------------------------------------------------- mocha (hardhat test)

_MOCHA_FAILURE = re.compile(
    r"^\s+(\d+)\) (?P<title>.+?):?\n(?P<body>(?:(?!^\s+\d+\) ).*\n?)*)", re.M
)


def parse_mocha(text: str, exit_code: int | None = None) -> TestReport:
    """Mocha's default spec reporter (hardhat test): counts plus the numbered failure list."""
    text = strip_ansi(text)
    report = TestReport("mocha", exit_code=exit_code)
    passing = re.search(r"^\s+(\d+) passing", text, re.M)
    if passing is None:
        report.ran = False
        report.errors.append(_tail(text))
        return report
    report.passed = int(passing.group(1))
    pending = re.search(r"^\s+(\d+) pending", text, re.M)
    report.skipped = int(pending.group(1)) if pending else 0
    failing = re.search(r"^\s+\d+ failing", text, re.M)
    if failing:
        for m in _MOCHA_FAILURE.finditer(text[failing.end() :]):
            body = m.group("body")
            lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
            title = " ".join(
                [
                    m.group("title").strip(),
                    *[ln.rstrip(":") for ln in lines[:1] if ln.endswith(":")],
                ]
            )
            message = next((ln for ln in lines if not ln.endswith(":")), "failed")
            where = re.search(r"\(([^()\s]+?):(\d+):\d+\)", body)
            report.failed.append(
                FailedTest(
                    title,
                    message,
                    where.group(1) if where else None,
                    int(where.group(2)) if where else None,
                    body.strip(),
                )
            )
    return report


# ---------------------------------------------------------------- swift test (Swift Testing + XCTest)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_ST_ISSUE = re.compile(
    r"^✘ Test (?P<name>\S+?)(?:\(\))? recorded an issue at (?P<file>[^:\s]+):(?P<line>\d+):\d+: (?P<msg>.+)$",
    re.M,
)
_ST_RESULT = re.compile(
    r"^(?P<mark>[✔✘]) Test (?P<name>\S+?)(?:\(\))? (?P<verdict>passed|failed) after", re.M
)
_ST_SKIP = re.compile(r"^➜ Test (?P<name>\S+?)(?:\(\))? skipped", re.M)
_XC_CASE = re.compile(
    r"^Test Case '-\[(?P<cls>[\w.]+) (?P<name>\w+)\]' (?P<verdict>passed|failed|skipped)", re.M
)
_XC_ISSUE = re.compile(
    r"^(?P<file>[^:\s]+):(?P<line>\d+): error: -\[(?P<cls>[\w.]+) (?P<name>\w+)\] : (?P<msg>.+)$",
    re.M,
)


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def parse_swift_test(text: str, exit_code: int | None = None) -> TestReport:
    """`swift test` output: Swift Testing (✔/✘ lines) and/or XCTest (Test Case lines)."""
    text = strip_ansi(text)
    report = TestReport("swift test", exit_code=exit_code)
    issues: dict[str, re.Match[str]] = {}
    for m in _ST_ISSUE.finditer(text):
        issues.setdefault(m.group("name"), m)
    for m in _XC_ISSUE.finditer(text):
        issues.setdefault(f"{m.group('cls')}/{m.group('name')}", m)
    seen = False
    for m in _ST_RESULT.finditer(text):
        seen = True
        if m.group("verdict") == "passed":
            report.passed += 1
            continue
        issue = issues.get(m.group("name"))
        report.failed.append(
            FailedTest(
                m.group("name"),
                issue.group("msg") if issue else "failed",
                issue.group("file") if issue else None,
                int(issue.group("line")) if issue else None,
            )
        )
    report.skipped += len(_ST_SKIP.findall(text))
    for m in _XC_CASE.finditer(text):
        seen = True
        key = f"{m.group('cls')}/{m.group('name')}"
        if m.group("verdict") == "passed":
            report.passed += 1
        elif m.group("verdict") == "skipped":
            report.skipped += 1
        else:
            issue = issues.get(key)
            report.failed.append(
                FailedTest(
                    key,
                    issue.group("msg") if issue else "failed",
                    issue.group("file") if issue else None,
                    int(issue.group("line")) if issue else None,
                )
            )
    if not seen:
        report.ran = False
        errors = [ln for ln in text.splitlines() if ": error: " in ln]
        report.errors.extend(errors[:20] or [_tail(text)])
    return report


# ---------------------------------------------------------------- TAP / JUnit


_TAP_LOCATION = re.compile(r"^\s+location: '(?P<file>.+?):(?P<line>\d+):\d+'$", re.M)
_TAP_ERROR = re.compile(
    r"^\s+error: (?:\|-?\n(?P<block>(?:\s{4,}.*\n?)+)|'?(?P<inline>.+?)'?$)", re.M
)


def _tap_failure(name: str, block: str) -> FailedTest:
    """A failed TAP case with node --test's YAML diagnostics (location, error) when present."""
    location = _TAP_LOCATION.search(block)
    error = _TAP_ERROR.search(block)
    message = "not ok"
    if error:
        raw = error.group("block") or error.group("inline") or ""
        message = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "not ok")
    return FailedTest(
        name,
        message,
        location.group("file") if location else None,
        int(location.group("line")) if location else None,
        block.strip(),
    )


def parse_tap(text: str, exit_code: int | None = None) -> TestReport:
    report = TestReport("tap", exit_code=exit_code)
    seen = False
    lines = text.splitlines()
    for i, line in enumerate(lines):
        match = re.match(
            r"^(not ok|ok)\s+\d+\s*-?\s*(.*?)(?:\s*#\s*(SKIP|TODO).*)?$", line.strip(), re.I
        )
        if not match or line.startswith(" "):  # indented: a subtest, counted by its parent
            continue
        seen = True
        if match.group(3):
            report.skipped += 1
        elif match.group(1).lower() == "ok":
            report.passed += 1
        else:
            block: list[str] = []
            if i + 1 < len(lines) and lines[i + 1].strip() == "---":
                for follow in lines[i + 2 :]:
                    if follow.strip() == "...":
                        break
                    block.append(follow)
            report.failed.append(_tap_failure(match.group(2) or "unnamed", "\n".join(block)))
    report.ran = seen
    if not seen:
        report.errors.append(_tail(text))
    return report


def parse_junit_xml(text: str, exit_code: int | None = None) -> TestReport:
    report = TestReport("junit", exit_code=exit_code)
    try:
        root = ET.fromstring(text)  # noqa: S314  (our own tool output, not untrusted network XML)
    except ET.ParseError as exc:
        report.ran = False
        report.errors.append(f"invalid JUnit XML: {exc}")
        return report
    duration = 0.0
    for case in root.iter("testcase"):
        duration += float(case.get("time") or 0)
        name = ".".join(x for x in (case.get("classname"), case.get("name")) if x)
        failure = case.find("failure")
        error = case.find("error")
        if case.find("skipped") is not None:
            report.skipped += 1
        elif (node := failure if failure is not None else error) is not None:
            message = (
                node.get("message") or (node.text or "").strip().splitlines()[-1:] or ["failed"]
            )
            msg = message if isinstance(message, str) else message[0]
            line = case.get("line")
            report.failed.append(
                FailedTest(
                    name, msg, case.get("file"), int(line) if line else None, node.text or ""
                )
            )
        else:
            report.passed += 1
    report.duration_s = duration
    return report


# ---------------------------------------------------------------- diagnostics

_GENERIC_DIAG = re.compile(
    r"^(?P<file>[^\s:()]+\.[A-Za-z0-9]+):(?P<line>\d+)(?::(?P<col>\d+))?:\s*(?P<sev>error|warning|note|fatal error)(?:\[(?P<code>[^\]]+)\])?:\s*(?P<msg>.+)$",
    re.M,
)
_TSC_DIAG = re.compile(
    r"^(?P<file>[^\s()]+)\((?P<line>\d+),(?P<col>\d+)\): (?P<sev>error|warning) (?P<code>TS\d+): (?P<msg>.+)$",
    re.M,
)

# rustc / swiftc-style: "error[E0277]: message" then "  --> file:line:col" on the next line
_RUSTC_DIAG = re.compile(
    r"^(?P<sev>error|warning)(?:\[(?P<code>[A-Za-z0-9_:]+)\])?: (?P<msg>.+)\n\s*--> (?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+)",
    re.M,
)


def parse_diagnostics_text(text: str, tool: str = "") -> list[Diagnostic]:
    text = strip_ansi(text)  # swiftc colors diagnostics even when piped
    out: list[Diagnostic] = []
    for regex in (_TSC_DIAG, _RUSTC_DIAG, _GENERIC_DIAG):
        for m in regex.finditer(text):
            sev = "error" if "error" in m.group("sev") else cast(Severity, m.group("sev"))
            col = m.group("col")
            out.append(
                Diagnostic(
                    sev,
                    m.group("msg").strip(),
                    m.group("file"),
                    int(m.group("line")),
                    int(col) if col else None,
                    m.group("code"),
                    tool,
                )
            )
    return out


def parse_cargo_json(text: str) -> list[Diagnostic]:
    out: list[Diagnostic] = []
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            msg = cast(dict[str, Any], json.loads(line))
        except json.JSONDecodeError:
            continue
        if msg.get("reason") != "compiler-message":
            continue
        inner = cast(dict[str, Any], msg.get("message", {}))
        level = str(inner.get("level", "error"))
        if level not in {"error", "warning"}:
            continue
        spans = [
            s for s in cast(list[dict[str, Any]], inner.get("spans", [])) if s.get("is_primary")
        ]
        span = spans[0] if spans else {}
        code = cast(dict[str, Any] | None, inner.get("code"))
        out.append(
            Diagnostic(
                cast(Severity, level),
                str(inner.get("message", "")),
                span.get("file_name"),
                span.get("line_start"),
                span.get("column_start"),
                code.get("code") if code else None,
                "rustc",
            )
        )
    return out


def parse_pyright_json(text: str) -> list[Diagnostic]:
    try:
        data = cast(dict[str, Any], json.loads(text[text.find("{") :]))
    except (json.JSONDecodeError, ValueError):
        return []
    out: list[Diagnostic] = []
    for d in cast(list[dict[str, Any]], data.get("generalDiagnostics", [])):
        start = cast(dict[str, Any], d.get("range", {}).get("start", {}))
        sev = d.get("severity", "error")
        severity: Severity = (
            "error" if sev == "error" else "warning" if sev == "warning" else "note"
        )
        out.append(
            Diagnostic(
                severity,
                str(d.get("message", "")),
                d.get("file"),
                int(start.get("line", 0)) + 1,
                int(start.get("character", 0)) + 1,
                d.get("rule"),
                "pyright",
            )
        )
    return out


def parse_ruff_json(text: str) -> list[Diagnostic]:
    try:
        items = cast(list[dict[str, Any]], json.loads(text[text.find("[") :]))
    except (json.JSONDecodeError, ValueError):
        return []
    out: list[Diagnostic] = []
    for item in items:
        loc = cast(dict[str, Any], item.get("location", {}))
        out.append(
            Diagnostic(
                "error",
                str(item.get("message", "")),
                item.get("filename"),
                loc.get("row"),
                loc.get("column"),
                item.get("code"),
                "ruff",
            )
        )
    return out


_SOLC_DIAG = re.compile(
    r"^(?P<sev>Error|Warning|TypeError|ParserError|DeclarationError|SyntaxError)(?: \((?P<code>\d+)\))?: (?P<msg>.+)\n\s*--> (?P<file>[^:\s]+):(?P<line>\d+):(?P<col>\d+):",
    re.M,
)


def parse_solc_text(text: str, tool: str = "solc") -> list[Diagnostic]:
    """solc / forge build text: `Error (7576): Undeclared identifier.` then `--> file:L:C:`."""
    out: list[Diagnostic] = []
    for m in _SOLC_DIAG.finditer(strip_ansi(text)):
        out.append(
            Diagnostic(
                "warning" if m.group("sev") == "Warning" else "error",
                m.group("msg").strip(),
                m.group("file"),
                int(m.group("line")),
                int(m.group("col")),
                m.group("code"),
                tool,
            )
        )
    return out


_GO_DIAG = re.compile(
    r"^(?:vet: )?(?:\./)?(?P<file>[^\s:]+\.go):(?P<line>\d+):(?P<col>\d+): (?P<msg>.+)$", re.M
)


def parse_go_diagnostics(text: str, tool: str = "go") -> list[Diagnostic]:
    """go build / go vet / golangci-lint text: `file.go:line:col: message`."""
    out: list[Diagnostic] = []
    for m in _GO_DIAG.finditer(text):
        msg = m.group("msg").strip()
        linter = re.search(r" \((\w[\w-]*)\)$", msg) if tool == "golangci-lint" else None
        out.append(
            Diagnostic(
                "error",
                msg[: linter.start()] if linter else msg,
                m.group("file"),
                int(m.group("line")),
                int(m.group("col")),
                linter.group(1) if linter else None,
                tool,
            )
        )
    return out


def parse_eslint_json(text: str) -> list[Diagnostic]:
    try:
        files = cast(list[dict[str, Any]], json.loads(text[text.find("[") :]))
    except (json.JSONDecodeError, ValueError):
        return []
    out: list[Diagnostic] = []
    for entry in files:
        for msg in cast(list[dict[str, Any]], entry.get("messages", [])):
            out.append(
                Diagnostic(
                    "error" if msg.get("severity") == 2 else "warning",
                    str(msg.get("message", "")),
                    entry.get("filePath"),
                    msg.get("line"),
                    msg.get("column"),
                    msg.get("ruleId"),
                    "eslint",
                )
            )
    return out


# ---------------------------------------------------------------- dispatch + compression

Reporter = Literal[
    "pytest",
    "unittest",
    "go-json",
    "cargo",
    "jest-json",
    "vitest-json",
    "forge",
    "swift",
    "mocha",
    "cairo",
    "tap",
    "junit",
    "auto",
]

_PARSERS: dict[str, Callable[[str, int | None], TestReport]] = {
    "pytest": parse_pytest,
    "unittest": parse_unittest,
    "go-json": parse_go_test_json,
    "cargo": parse_cargo_test,
    "jest-json": parse_jest_json,
    "vitest-json": lambda t, c: parse_jest_json(t, c, "vitest"),
    "forge": parse_forge_test,
    "swift": parse_swift_test,
    "mocha": parse_mocha,
    "cairo": parse_cairo_test,
    "tap": parse_tap,
    "junit": parse_junit_xml,
}


def detect_reporter(text: str) -> Reporter:
    if '"Action":' in text and '"Package":' in text:
        return "go-json"
    if "numTotalTests" in text:
        return "jest-json"
    if "(gas usage est.:" in text or _SNFORGE_TOTALS.search(text):
        return "cairo"  # before cargo: cairo-test also prints "test result:"
    if "test result:" in text and "running " in text:
        return "cargo"
    if "Suite result:" in text or ("Ran " in text and "tests for " in text):
        return "forge"
    if "◇ Test run started" in text or re.search(r"^Test Case '-\[", text, re.M):
        return "swift"
    if (
        re.search(r"^=+ .*(passed|failed|error|no tests ran).* =+$", text, re.M)
        or "short test summary" in text
    ):
        return "pytest"
    if _UNITTEST_RAN.search(text):
        return "unittest"
    if text.lstrip().startswith("<?xml") or "<testsuite" in text:
        return "junit"
    if re.search(r"^(not )?ok \d+", text, re.M):
        return "tap"
    return "pytest"


_EXEC_FAILED = re.compile(r"execvp\(\) of '([^']+)' failed: No such file or directory")


def parse_test_output(result: ProcessResult, reporter: Reporter = "auto") -> TestReport:
    text = result.stdout + ("\n" + result.stderr if result.stderr else "")
    if result.timed_out:
        report = TestReport(reporter, ran=False, exit_code=result.exit_code)
        report.errors.append(f"timed out after {result.duration_ms} ms")
        return report
    # a shell says "command not found" (127); the macOS sandbox wrapper reports exit 71
    missing = _EXEC_FAILED.search(result.stderr)
    if result.exit_code == 127 or "command not found" in result.stderr or missing:
        report = TestReport(reporter, ran=False, exit_code=result.exit_code, command_missing=True)
        if missing:
            report.errors.append(
                f"test command not found: `{missing.group(1)}` is not installed here"
            )
        else:
            report.errors.append(f"test command not found: {_tail(result.stderr)}")
        return report
    chosen = detect_reporter(text) if reporter == "auto" else reporter
    return _PARSERS[chosen](text, result.exit_code)


def _tail(text: str, lines: int = 12) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def compress(
    report: TestReport,
    repo_markers: tuple[str, ...] = (),
    max_failures: int = 8,
    max_frames: int = 6,
) -> str:
    """A compact, deterministic failure summary for model context."""
    lines = [report.summary()]
    seen: set[str] = set()
    for failure in report.failed:
        key = failure.message.strip()
        if key in seen:
            lines.append(f"- {failure.id}: same failure as above")
            continue
        seen.add(key)
        if len(seen) > max_failures:
            lines.append(f"- ... {len(report.failed) - max_failures} more failures")
            break
        where = f" ({failure.file}:{failure.line})" if failure.file else ""
        lines.append(f"- {failure.id}{where}: {failure.message.strip()[:300]}")
        frames = [ln for ln in failure.trace.splitlines() if ln.strip()]
        if repo_markers:
            own = [ln for ln in frames if any(m in ln for m in repo_markers)]
            frames = own or frames
        if len(frames) > max_frames:
            half = max_frames // 2
            frames = [*frames[:half], "    ...", *frames[-half:]]
        lines.extend("    " + ln.strip()[:200] for ln in frames)
    lines.extend(f"! {e[:300]}" for e in report.errors[:5])
    return "\n".join(lines)


def compress_diagnostics(diagnostics: list[Diagnostic], limit: int = 30) -> str:
    errors = [d for d in diagnostics if d.severity == "error"]
    shown = (errors or diagnostics)[:limit]
    lines = [f"{len(errors)} errors, {len(diagnostics) - len(errors)} warnings/notes"]
    lines.extend(
        f"- {d.location}: {d.severity}{f' [{d.code}]' if d.code else ''}: {d.message[:300]}"
        for d in shown
    )
    if len(errors or diagnostics) > limit:
        lines.append(f"- ... {len(errors or diagnostics) - limit} more")
    return "\n".join(lines)
