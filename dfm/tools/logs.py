"""Streaming log / JSONL inspection.

One pass, constant memory, any file size: head, tail (read backwards from the end),
grep, JSON field filters (`level=error`, `ctx.user=7`), time windows, reservoir
sampling, error clustering by message template (numbers, ids, hex, quoted strings
masked), stack-trace grouping, and level / per-minute frequencies. The output is a
compact summary meant for model context, and every emitted line is redacted.
"""

from __future__ import annotations

import json
import random
import re
from collections import Counter, deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from dfm.secrets import redact_text

MAX_TEMPLATES = 5000  # distinct templates kept; beyond this, counted as "other"
MAX_LINE = 2000  # characters kept per emitted line

_ANSI = re.compile(r"(?:\x1b|\^\[)\[[0-9;]*m")
_ERRORISH = re.compile(
    rb"(?i)\b(error|fatal|critical|exception|panic|traceback|failed)\b|\"level\":\s*\"(error|fatal)"
)
_LEVEL = re.compile(r"(?i)\b(trace|debug|info|warn(?:ing)?|error|fatal|critical)\b")
_ISO_BYTES = re.compile(rb"(\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d)?)")
_LEVEL_BYTES = re.compile(rb"(?i)\b(trace|debug|info|warn(?:ing)?|error|fatal|critical)\b")
_ISO = re.compile(r"(\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d)?)")
_MASKS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
        "<uuid>",
    ),
    (_ISO, "<ts>"),
    (re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\"[^\"]{0,200}\"|'[^']{0,200}'"), "<str>"),
    (re.compile(r"(?<![\w<])-?\d+(?:\.\d+)?(?:ms|s|b|kb|mb)?\b", re.I), "<n>"),
)
_STACK_START = re.compile(
    r"^(Traceback \(most recent call last\)|goroutine \d+ \[|thread '.*' panicked at|Exception in thread|\s*at [\w$.<>]+\(|panic: )"
)
_STACK_FRAME = re.compile(
    r'^\s+(File "|at |[\w./-]+\.go:\d+|\d+: )'  # Python, JVM/JS, Go file lines, Rust frames
    r"|^[\w.*/$\[\]-]+\(.*\)\s*$"  # Go function lines (`main.main()`, `pkg.(*T).M(...)`)
)


def template(message: str) -> str:
    """A message with its variable parts masked, so repeats of one error collapse together."""
    text = message.strip()
    for pattern, mask in _MASKS:
        text = pattern.sub(mask, text)
    return re.sub(r"\s+", " ", text)[:300]


def _clean(raw: bytes) -> str:
    return _ANSI.sub("", raw.decode(errors="replace").rstrip("\r\n"))


def emit_line(line: str) -> str:
    return redact_text(line[:MAX_LINE])


def parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _json(line: str) -> dict[str, Any] | None:
    if not line.startswith("{"):
        return None
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def _field(record: dict[str, Any], path: str) -> Any:
    value: Any = record
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = cast(dict[str, Any], value).get(key)
    return value


def _timestamp(line: str, record: dict[str, Any] | None) -> datetime | None:
    if record is not None:
        for key in ("ts", "time", "timestamp", "@timestamp", "t", "date"):
            value = record.get(key)
            if isinstance(value, str):
                return parse_time(value)
            if isinstance(value, int | float):
                seconds = value / 1000 if value > 1e11 else value
                return datetime.fromtimestamp(seconds, UTC)
    match = _ISO.search(line[:64])
    return parse_time(match.group(1)) if match else None


def _level(line: str, record: dict[str, Any] | None) -> str | None:
    if record is not None:
        value = record.get("level") or record.get("severity") or record.get("lvl")
        if isinstance(value, str):
            return value.lower().replace("warning", "warn")
    match = _LEVEL.search(line[:120])
    return match.group(1).lower().replace("warning", "warn") if match else None


@dataclass
class Query:
    grep: re.Pattern[str] | None = None
    fields: dict[str, str] = field(default_factory=lambda: {})  # dotted path → expected value
    since: datetime | None = None
    until: datetime | None = None

    @property
    def active(self) -> bool:
        return bool(self.grep or self.fields or self.since or self.until)


@dataclass
class Cluster:
    template: str
    count: int = 0
    first_line: int = 0
    example: str = ""


@dataclass
class Report:
    path: str
    size: int
    lines: int = 0
    matched: int = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    levels: Counter[str] = field(default_factory=lambda: Counter())
    per_minute: Counter[str] = field(default_factory=lambda: Counter())
    clusters: dict[str, Cluster] = field(default_factory=lambda: {})
    other_errors: int = 0
    stacks: dict[str, Cluster] = field(default_factory=lambda: {})
    selected: list[tuple[int, str]] = field(default_factory=lambda: [])  # head/grep/sample output
    tail: deque[tuple[int, str]] = field(default_factory=lambda: deque())

    def render(self, *, top: int = 10) -> str:
        out = [
            f"{self.path}: {self.size:,} bytes, {self.lines:,} lines scanned, {self.matched:,} matched"
        ]
        if self.first_ts and self.last_ts:
            out.append(f"time: {self.first_ts.isoformat()} .. {self.last_ts.isoformat()}")
        if self.levels:
            out.append("levels: " + ", ".join(f"{k}={v:,}" for k, v in self.levels.most_common()))
        if self.clusters:
            out.append(f"error clusters ({len(self.clusters)} templates, top {top}):")
            for c in sorted(self.clusters.values(), key=lambda c: -c.count)[:top]:
                out.append(f"  {c.count:>8,}x  {emit_line(c.template)}")
                out.append(
                    f"            first at line {c.first_line}: {emit_line(c.example)[:240]}"
                )
            if self.other_errors:
                out.append(
                    f"  {self.other_errors:>8,}x  (other templates beyond the {MAX_TEMPLATES} kept)"
                )
        if self.stacks:
            out.append(
                f"stack traces ({sum(s.count for s in self.stacks.values()):,}, {len(self.stacks)} distinct):"
            )
            for s in sorted(self.stacks.values(), key=lambda s: -s.count)[:top]:
                out.append(
                    f"  {s.count:>8,}x  {emit_line(s.template)}  (first at line {s.first_line})"
                )
        if self.per_minute:
            busiest = self.per_minute.most_common(5)
            out.append("busiest minutes: " + ", ".join(f"{m} ({n:,})" for m, n in busiest))
        for number, line in [*self.selected, *self.tail]:
            out.append(f"{number:>8}: {emit_line(line)}")
        return "\n".join(out)


def _iter(path: Path) -> Iterator[tuple[int, bytes]]:
    with path.open("rb") as fh:
        yield from enumerate(fh, 1)


def tail_lines(path: Path, n: int, block: int = 64 * 1024) -> list[str]:
    """The last `n` lines, reading backwards from the end (never the whole file)."""
    with path.open("rb") as fh:
        fh.seek(0, 2)
        end = fh.tell()
        data = b""
        while end > 0 and data.count(b"\n") <= n:
            start = max(0, end - block)
            fh.seek(start)
            data = fh.read(end - start) + data
            end = start
    return [_clean(line) for line in data.splitlines()[-n:]]


def _stack_signature(block: list[str]) -> str:
    frames = [ln.strip() for ln in block if _STACK_FRAME.match(ln)][-3:]
    final = next(
        (
            ln.strip()
            for ln in reversed(block)
            if ln.strip() and not _STACK_FRAME.match(ln) and not ln.startswith((" ", "\t"))
        ),
        block[0],
    )
    return template(" | ".join([final, *frames]))


class _Stacks:
    """Groups multi-line stack traces (Python, Java/JVM, Go, Rust) by signature."""

    def __init__(self, report: Report) -> None:
        self.report = report
        self.block: list[str] = []
        self.start = 0

    def feed(self, number: int, line: str) -> None:
        if not self.block:
            if _STACK_START.match(line):
                self.block, self.start = [line], number
            return
        if _STACK_FRAME.match(line) or line.startswith((" ", "\t")):
            self.block.append(line)
            return
        if self.block[0].startswith("Traceback") and line.strip():
            self.block.append(line)  # Python ends with the exception line itself
            self.close()
            return
        self.close()
        self.feed(number, line)  # the line that ended this trace may start the next one

    def close(self) -> None:
        if len(self.block) > 1:
            signature = _stack_signature(self.block)
            stacks = self.report.stacks
            entry = stacks.get(signature)
            if entry is None and len(stacks) < MAX_TEMPLATES:
                entry = stacks[signature] = Cluster(signature, 0, self.start, self.block[-1])
            if entry is not None:
                entry.count += 1
        self.block = []


_MAYBE_STACK = re.compile(rb"^(Traceback|goroutine \d|thread '|Exception in thread|panic: |\s)")


def inspect(
    path: Path,
    query: Query | None = None,
    *,
    head: int = 0,
    sample: int = 0,
    cluster: bool = True,
    stacks: bool = True,
    keep_tail: int = 0,
    seed: int = 0,
) -> Report:
    """One streaming pass. Memory: bounded by MAX_TEMPLATES, `head`, `sample` and `keep_tail`."""
    query = query or Query()
    report = Report(str(path), path.stat().st_size)
    report.tail = deque(maxlen=keep_tail)
    rng = random.Random(seed)  # noqa: S311  (sampling, not security)
    grouper = _Stacks(report)
    current_ts: datetime | None = None
    every_line = query.active or bool(head or sample or keep_tail)

    for number, raw in _iter(path):
        report.lines = number
        errorish = _ERRORISH.search(raw) is not None
        stacky = stacks and (bool(grouper.block) or _MAYBE_STACK.match(raw) is not None)
        if not (every_line or errorish or stacky):
            # a plain line: level and timestamp from the raw bytes, no decoding or JSON parsing
            report.matched += 1
            if level_match := _LEVEL_BYTES.search(raw, 0, 120):
                report.levels[level_match.group(1).decode().lower().replace("warning", "warn")] += 1
            if (ts_match := _ISO_BYTES.search(raw, 0, 100)) and (
                ts := parse_time(ts_match.group(1).decode())
            ):
                current_ts = ts
                report.first_ts = report.first_ts or ts
                report.last_ts = ts
                report.per_minute[ts.strftime("%Y-%m-%dT%H:%M")] += 1
                if len(report.per_minute) > 10_000:  # bounded: keep the busiest
                    report.per_minute = Counter(dict(report.per_minute.most_common(5_000)))
            continue
        line = _clean(raw)
        if stacky:
            grouper.feed(number, line)
        record = _json(line)
        ts = _timestamp(line, record)
        if ts is not None:
            current_ts = ts
        if query.since and (current_ts is None or current_ts < query.since):
            continue
        if query.until and current_ts is not None and current_ts > query.until:
            continue
        if query.grep and not query.grep.search(line):
            continue
        if query.fields and (
            record is None or any(str(_field(record, k)) != v for k, v in query.fields.items())
        ):
            continue
        report.matched += 1
        if current_ts is not None:
            report.first_ts = report.first_ts or current_ts
            report.last_ts = current_ts
            report.per_minute[current_ts.strftime("%Y-%m-%dT%H:%M")] += 1
            if len(report.per_minute) > 10_000:  # bounded: keep the busiest
                report.per_minute = Counter(dict(report.per_minute.most_common(5_000)))
        level = _level(line, record)
        if level:
            report.levels[level] += 1
        if cluster and errorish:
            message = (
                str(_field(record, "msg") or _field(record, "message") or line) if record else line
            )
            key = template(message)
            entry = report.clusters.get(key)
            if entry is None and len(report.clusters) >= MAX_TEMPLATES:
                report.other_errors += 1
            else:
                if entry is None:
                    entry = report.clusters[key] = Cluster(key, 0, number, line)
                entry.count += 1
        if head and len(report.selected) < head:
            report.selected.append((number, line))
        elif sample:
            if len(report.selected) < sample:
                report.selected.append((number, line))
            elif (j := rng.randrange(report.matched)) < sample:
                report.selected[j] = (number, line)
        if keep_tail:
            report.tail.append((number, line))
    grouper.close()
    report.selected.sort()
    return report


def parse_window(text: str) -> tuple[datetime | None, datetime | None]:
    """`START..END` (either side may be empty): ISO timestamps or dates."""
    start, _, end = text.partition("..")
    return (parse_time(start) if start else None, parse_time(end) if end else None)


def parse_fields(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise ValueError(f"field filter needs FIELD=VALUE: {pair!r}")
        out[key.strip()] = value.strip()
    return out
