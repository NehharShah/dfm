"""Applying edits without clobbering files.

Two patch forms: search/replace blocks and unified diffs. Each is applied
exactly first; if the text moved or whitespace drifted, the block is relocated
by whitespace-insensitive line matching (a unique match is required). If it
still cannot be placed, `PatchConflict` carries the closest candidate regions so
the caller can re-read and regenerate the edit. There is no fallback to
rewriting the whole file.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field


class PatchConflict(Exception):
    def __init__(self, message: str, candidates: list[str] | None = None) -> None:
        super().__init__(message)
        self.candidates = candidates or []


@dataclass(frozen=True)
class SearchReplace:
    search: str
    replace: str


@dataclass(frozen=True)
class UnifiedDiff:
    text: str


Patch = list[SearchReplace] | UnifiedDiff


@dataclass
class ApplyResult:
    text: str
    relocated: int = 0  # blocks/hunks placed by fuzzy matching
    notes: list[str] = field(default_factory=lambda: [])


def _norm(line: str) -> str:
    return " ".join(line.split())


def _find_exact(lines: list[str], needle: list[str], hint: int = 0) -> list[int]:
    n = len(needle)
    hits = [i for i in range(len(lines) - n + 1) if lines[i : i + n] == needle]
    return sorted(hits, key=lambda i: abs(i - hint))


def _find_fuzzy(lines: list[str], needle: list[str]) -> list[int]:
    target = [_norm(x) for x in needle]
    normalized = [_norm(x) for x in lines]
    n = len(needle)
    return [i for i in range(len(lines) - n + 1) if normalized[i : i + n] == target]


def _candidates(lines: list[str], needle: list[str], limit: int = 3) -> list[str]:
    n = max(1, len(needle))
    joined = "\n".join(needle)
    scored: list[tuple[float, int]] = []
    for i in range(0, max(1, len(lines) - n + 1)):
        window = "\n".join(lines[i : i + n])
        scored.append((difflib.SequenceMatcher(None, joined, window).ratio(), i))
    scored.sort(reverse=True)
    out: list[str] = []
    for ratio, i in scored[:limit]:
        if ratio < 0.4:
            break
        out.append(
            f"lines {i + 1}-{i + n} (similarity {ratio:.2f}):\n" + "\n".join(lines[i : i + n])
        )
    return out


def _reindent(replacement: list[str], original: list[str], matched: list[str]) -> list[str]:
    """Shift the replacement by the indentation difference found when fuzzy-matching."""

    def indent(s: str) -> int:
        return len(s) - len(s.lstrip())

    firsts = [(o, m) for o, m in zip(original, matched, strict=False) if o.strip()]
    if not firsts:
        return replacement
    delta = indent(firsts[0][1]) - indent(firsts[0][0])
    if delta == 0:
        return replacement
    out: list[str] = []
    for line in replacement:
        if not line.strip():
            out.append(line)
        elif delta > 0:
            out.append(" " * delta + line)
        else:
            out.append(line[min(-delta, indent(line)) :])
    return out


def apply_search_replace(text: str, blocks: list[SearchReplace]) -> ApplyResult:
    result = ApplyResult(text)
    for number, block in enumerate(blocks, 1):
        if block.search == "":
            raise PatchConflict(
                f"block {number}: empty search text (use create_file for new files)"
            )
        lines = result.text.split("\n")
        search, replace = block.search, block.replace
        # Matching is line-based: a block that ends in a newline ("    a - b\n", as models
        # usually write it) means "these whole lines", not "these lines plus an empty one".
        whole_lines = search.endswith("\n") and bool(search.strip("\n"))
        if whole_lines:
            search = search[:-1]
            replace = replace.removesuffix("\n")
        needle = search.split("\n")
        replacement = [] if whole_lines and block.replace == "" else replace.split("\n")
        exact = _find_exact(lines, needle)
        if len(exact) == 1:
            start = exact[0]
            matched = needle
        elif len(exact) > 1:
            raise PatchConflict(
                f"block {number}: search text occurs {len(exact)} times; add surrounding lines to make it unique"
            )
        else:
            fuzzy = _find_fuzzy(lines, needle)
            if len(fuzzy) != 1:
                why = (
                    "not found" if not fuzzy else f"matches {len(fuzzy)} places ignoring whitespace"
                )
                raise PatchConflict(
                    f"block {number}: search text {why}", _candidates(lines, needle)
                )
            start = fuzzy[0]
            matched = lines[start : start + len(needle)]
            replacement = _reindent(replacement, needle, matched)
            result.relocated += 1
            result.notes.append(
                f"block {number} relocated to line {start + 1} (whitespace differed)"
            )
        lines[start : start + len(matched)] = replacement
        result.text = "\n".join(lines)
    return result


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old: list[str]
    new: list[str]


@dataclass(frozen=True)
class FileDiff:
    old_path: str | None  # None for /dev/null (new file)
    new_path: str | None  # None for deletion
    hunks: list[Hunk]


def _strip_prefix(path: str) -> str | None:
    path = path.split("\t", 1)[0].strip()
    if path == "/dev/null":
        return None
    return path[2:] if path.startswith(("a/", "b/")) else path


def parse_unified(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    lines = text.split("\n")

    def file_header(at: int) -> bool:
        # `--- path` followed by `+++ path`; a removed line `-- comment` (SQL, Lua) also
        # starts with "--- " but is never followed by "+++ "
        return (
            at + 1 < len(lines)
            and lines[at].startswith("--- ")
            and lines[at + 1].startswith("+++ ")
        )

    i = 0
    while i < len(lines):
        if file_header(i):
            old_path = _strip_prefix(lines[i][4:])
            new_path = _strip_prefix(lines[i + 1][4:])
            i += 2
            hunks: list[Hunk] = []
            while i < len(lines) and not file_header(i):
                match = _HUNK.match(lines[i])
                if not match:
                    i += 1
                    continue
                old_start = int(match.group(1))
                old: list[str] = []
                new: list[str] = []
                i += 1
                while i < len(lines) and not lines[i].startswith("@@") and not file_header(i):
                    line = lines[i]
                    if line.startswith("\\"):
                        pass  # "\ No newline at end of file"
                    elif line.startswith("-"):
                        old.append(line[1:])
                    elif line.startswith("+"):
                        new.append(line[1:])
                    elif line.startswith(" ") or line == "":
                        old.append(line[1:] if line else "")
                        new.append(line[1:] if line else "")
                    i += 1
                # a trailing empty line from text splitting is not context
                while old and new and old[-1] == "" and new[-1] == "" and i >= len(lines):
                    old.pop()
                    new.pop()
                hunks.append(Hunk(old_start, old, new))
            files.append(FileDiff(old_path, new_path, hunks))
        else:
            i += 1
    if not files:
        raise PatchConflict("no file headers (--- / +++) found in unified diff")
    return files


def apply_hunks(text: str, hunks: list[Hunk]) -> ApplyResult:
    result = ApplyResult(text)
    offset = 0
    for number, hunk in enumerate(hunks, 1):
        lines = result.text.split("\n")
        hint = max(0, hunk.old_start - 1 + offset)
        if not hunk.old:
            position = min(hint, len(lines))
            lines[position:position] = hunk.new
            result.text = "\n".join(lines)
            offset += len(hunk.new)
            continue
        exact = _find_exact(lines, hunk.old, hint)
        if exact:
            start = exact[0]  # nearest to where the diff says it should be
            new_lines = hunk.new
            if start != hint:
                result.notes.append(
                    f"hunk {number} applied at line {start + 1} (offset {start - hint:+d})"
                )
        else:
            fuzzy = _find_fuzzy(lines, hunk.old)
            if len(fuzzy) != 1:
                why = "context not found" if not fuzzy else "context ambiguous ignoring whitespace"
                raise PatchConflict(f"hunk {number}: {why}", _candidates(lines, hunk.old))
            start = fuzzy[0]
            new_lines = _reindent(hunk.new, hunk.old, lines[start : start + len(hunk.old)])
            result.relocated += 1
            result.notes.append(
                f"hunk {number} relocated to line {start + 1} (whitespace differed)"
            )
        lines[start : start + len(hunk.old)] = new_lines
        offset += len(new_lines) - len(hunk.old)
        result.text = "\n".join(lines)
    return result
