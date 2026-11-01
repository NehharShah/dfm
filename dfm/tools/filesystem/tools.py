"""Policy-checked filesystem tools.

Reads: boundary-checked, binary and huge files refused (use the log/large-file
inspectors), secret files shown as keys only. Writes: boundary-checked (never
into .git/), policy-authorized, refused if they contain redaction markers
(writing "[REDACTED]" back would destroy the real value), checkpointed before
the first change to each file, written atomically with the file mode preserved.
Optional `guard` hook lets Git safety veto writes to files the developer
had already modified.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dfm.execution import BoundaryViolation, ExecutionEnvironment
from dfm.policy import ActionClass, Decision, Policy, PolicyVerdict, ProposedAction
from dfm.recovery import Checkpointer
from dfm.secrets import contains_redaction, is_secret_path, redact_secret_file
from dfm.tools.errors import ToolError
from dfm.tools.filesystem.kinds import STREAMABLE, classify_file, read_range, stub
from dfm.tools.filesystem.patching import (
    Patch,
    PatchConflict,
    SearchReplace,
    UnifiedDiff,
    apply_hunks,
    apply_search_replace,
    parse_unified,
)

MAX_READ_BYTES = 256 * 1024
_SKIP_DIRS = {
    ".git",
    ".dfm",
    "node_modules",
    ".venv",
    "venv",
    "target",
    "dist",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}


@dataclass(frozen=True)
class FileSlice:
    path: str
    text: str
    start: int
    end: int
    total_lines: int  # 0 when the file was streamed (too large to count cheaply)
    sha256: str
    stub: bool = False  # the text describes the file instead of containing it


@dataclass(frozen=True)
class TextMatch:
    path: str
    line: int
    text: str


@dataclass
class PatchResult:
    path: str
    applied: bool
    before_sha: str | None = None
    after_sha: str | None = None
    relocated: int = 0
    notes: list[str] = field(default_factory=lambda: [])
    conflict: str | None = None
    candidates: list[str] = field(default_factory=lambda: [])


EventSink = Callable[..., None]
WriteGuard = Callable[[str], PolicyVerdict | None]


def _noop(*args: Any, **kwargs: Any) -> None:
    return None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current


def _real_rg() -> str | None:
    """ripgrep binary on PATH (a shell function named rg does not count)."""
    path = shutil.which("rg")
    return path if path and os.access(path, os.X_OK) else None


class FsTools:
    def __init__(
        self,
        env: ExecutionEnvironment,
        policy: Policy,
        *,
        checkpointer: Checkpointer | None = None,
        proposed_by: str = "agent",
        on_event: EventSink | None = None,
        guard: WriteGuard | None = None,
    ) -> None:
        self.env = env
        self.policy = policy
        self.checkpointer = checkpointer
        self.proposed_by = proposed_by
        self._emit: EventSink = on_event or _noop
        self.guard = guard
        self.created: set[str] = set()  # files DFM created this session (deletable without asking)
        self.modified: set[str] = set()

    # ---------------------------------------------------------------- helpers

    def _rel(self, path: Path) -> str:
        return (
            path.relative_to(self.env.repo_root).as_posix()
            if path.is_relative_to(self.env.repo_root)
            else str(path)
        )

    def _resolve(self, p: str, *, write: bool = False) -> Path:
        try:
            return self.env.resolve(p, write=write)
        except BoundaryViolation as exc:
            raise ToolError(str(exc)) from exc

    async def _authorize(self, cls: ActionClass, summary: str, target: str) -> None:
        action = ProposedAction("fs", cls, summary, self.proposed_by, target=target)
        verdict = await self.policy.authorize(action)
        if verdict.decision is not Decision.ALLOW:
            raise ToolError(f"policy denied {summary}: {verdict.reason}")

    def _check_guard(self, rel: str) -> None:
        if self.guard is None:
            return
        verdict = self.guard(rel)
        if verdict is not None and verdict.decision is not Decision.ALLOW:
            raise ToolError(f"{rel}: {verdict.reason}")

    @staticmethod
    def _invalidate_bytecode(path: Path) -> None:
        """Python trusts a .pyc when source mtime (whole seconds) and size match, so an edit that
        keeps the size within the same second would run stale code. Drop that file's caches."""
        if path.suffix != ".py":
            return
        cache = path.parent / "__pycache__"
        if cache.is_dir():
            for pyc in cache.glob(f"{path.stem}.*.pyc"):
                pyc.unlink(missing_ok=True)

    def _write_atomic(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # an existing file keeps its mode; a new one follows the umask (mkstemp makes it 0600)
        mode = path.stat().st_mode & 0o7777 if path.exists() else 0o666 & ~_umask()
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".dfm-tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            Path(tmp).chmod(mode)
            Path(tmp).replace(path)
            self._invalidate_bytecode(path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # ---------------------------------------------------------------- reads

    async def read_lines(self, p: str, start: int = 1, end: int | None = None) -> FileSlice:
        path = self._resolve(p)
        if not path.is_file():
            raise ToolError(f"{p}: not a file")
        rel = self._rel(path)
        info = classify_file(path, text_limit=self.env.limits.read_text_bytes)
        if info.kind in STREAMABLE and end is not None:
            text, first, last = read_range(path, start, end, self.env.limits.read_range_lines)
            if is_secret_path(rel):
                text = redact_secret_file(rel, text)
            self._emit("file_read", path=rel, lines=[first, last], streamed=True)
            return FileSlice(rel, text, first, last, 0, "")
        if not info.readable:
            self._emit("file_read", path=rel, stub=True, kind=info.kind.value, size=info.size)
            return FileSlice(rel, stub(rel, info, path), 0, 0, 0, "", stub=True)
        data = path.read_bytes()
        text = data.decode(errors="replace")
        if is_secret_path(rel):
            text = redact_secret_file(rel, text)
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        start = max(1, start)
        stop = len(lines) if end is None else min(end, len(lines))
        self._emit("file_read", path=rel, lines=[start, stop])
        return FileSlice(
            rel, "\n".join(lines[start - 1 : stop]), start, stop, len(lines), _sha(data)
        )

    async def read_file(self, p: str) -> FileSlice:
        return await self.read_lines(p)

    async def list_directory(self, p: str = ".") -> list[str]:
        path = self._resolve(p)
        if not path.is_dir():
            raise ToolError(f"{p}: not a directory")
        entries = sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name))
        return [
            e.name + ("/" if e.is_dir() else "") for e in entries if e.name not in {".git", ".dfm"}
        ]

    async def search_files(self, pattern: str, p: str = ".", limit: int = 500) -> list[str]:
        root = self._resolve(p)

        def walk() -> list[str]:
            found: list[str] = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
                for name in filenames:
                    rel = self._rel(Path(dirpath) / name)
                    if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
                        found.append(rel)
                        if len(found) >= limit:
                            return sorted(found)
            return sorted(found)

        return await asyncio.to_thread(walk)

    async def search_text(
        self, pattern: str, p: str = ".", *, regex: bool = True, limit: int = 200
    ) -> list[TextMatch]:
        root = self._resolve(p)
        rg = _real_rg()
        if rg is not None:
            return await asyncio.to_thread(self._rg, rg, pattern, root, regex, limit)
        return await asyncio.to_thread(self._py_search, pattern, root, regex, limit)

    def _rg(self, rg: str, pattern: str, root: Path, regex: bool, limit: int) -> list[TextMatch]:
        # -H: a single-file root still prints "path:line:text"
        argv = [rg, "-H", "-n", "--no-heading", "--color", "never", "--max-columns", "300"]
        argv += ["-m", "50", *([] if regex else ["-F"]), "-e", pattern, "--", str(root)]
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)  # noqa: S603
        except subprocess.TimeoutExpired as exc:  # the pattern comes from a model: don't crash
            raise ToolError(f"search timed out after 60 s: {pattern[:200]!r}") from exc
        if out.returncode == 2 and not out.stdout.strip():  # e.g. a bad regex: not "no matches"
            detail = (out.stderr.strip().splitlines() or ["rg failed"])[-1]
            raise ToolError(f"search failed: {detail[:300]}")
        matches: list[TextMatch] = []
        for line in out.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[1].isdigit():
                rel = self._rel(Path(parts[0]))
                if is_secret_path(rel):  # same rule as the Python search: secrets never match
                    continue
                matches.append(TextMatch(rel, int(parts[1]), parts[2]))
                if len(matches) >= limit:
                    break
        return matches

    def _py_search(self, pattern: str, root: Path, regex: bool, limit: int) -> list[TextMatch]:
        try:
            compiled = re.compile(pattern if regex else re.escape(pattern))
        except re.error as exc:  # the pattern comes from a model: report it, don't crash the task
            raise ToolError(f"invalid regex {pattern[:200]!r}: {exc}") from exc
        matches: list[TextMatch] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            for name in sorted(filenames):
                path = Path(dirpath) / name
                rel = self._rel(path)
                if is_secret_path(rel):
                    continue
                if path.is_symlink():  # judge the target: outside the repo, or a secret file
                    target = Path(os.path.realpath(path))
                    if not self.env.boundary.contains(target) or is_secret_path(target):
                        continue
                try:
                    if path.stat().st_size > MAX_READ_BYTES:
                        continue
                    data = path.read_bytes()
                except OSError:
                    continue
                if b"\0" in data[:8192]:
                    continue
                for number, line in enumerate(data.decode(errors="replace").splitlines(), 1):
                    if compiled.search(line):
                        matches.append(TextMatch(rel, number, line[:300]))
                        if len(matches) >= limit:
                            return matches
        return matches

    # ---------------------------------------------------------------- writes

    async def _prepare_write(self, p: str, content: str | None, summary: str) -> tuple[Path, str]:
        path = self._resolve(p, write=True)
        rel = self._rel(path)
        if content is not None and contains_redaction(content):
            raise ToolError(f"{rel}: refusing to write text that contains redaction markers")
        if is_secret_path(rel):
            await self._authorize(
                ActionClass.LOCAL_STATE_CHANGE, f"{summary} (secret-bearing file)", rel
            )
        else:
            await self._authorize(ActionClass.WORKTREE_WRITE, summary, rel)
        self._check_guard(rel)
        if self.checkpointer is not None:
            self.checkpointer.record_original(rel)
        return path, rel

    async def write_file(self, p: str, content: str) -> PatchResult:
        existed = self._resolve(p, write=True).exists()
        path, rel = await self._prepare_write(p, content, f"write {p}")
        before = _sha(path.read_bytes()) if existed else None
        data = content.encode()
        self._write_atomic(path, data)
        if not existed:
            self.created.add(rel)
        self.modified.add(rel)
        self._emit("patch_finished", path=rel, kind="write", before=before, after=_sha(data))
        return PatchResult(rel, True, before, _sha(data))

    async def create_file(self, p: str, content: str) -> PatchResult:
        if self._resolve(p, write=True).exists():
            raise ToolError(f"{p}: already exists (use patch_file)")
        return await self.write_file(p, content)

    async def patch_file(
        self, p: str, patch: Patch, *, expected_sha: str | None = None
    ) -> PatchResult:
        target = self._resolve(p, write=True)
        if isinstance(patch, UnifiedDiff):
            diffs = parse_unified(patch.text)
            if len(diffs) != 1:
                raise ToolError("one file per patch_file call")
            diff = diffs[0]
            if diff.old_path is None and not target.exists():
                content = "\n".join(line for h in diff.hunks for line in h.new) + "\n"
                return await self.create_file(p, content)
        if not target.is_file():
            raise ToolError(f"{p}: does not exist (use create_file)")
        rel = self._rel(target)
        original = target.read_bytes()
        before = _sha(original)
        self._emit("patch_started", path=rel)
        if expected_sha is not None and expected_sha != before:
            return PatchResult(
                rel,
                False,
                before,
                conflict="file changed since it was read; re-read it and regenerate the edit",
            )
        # surrogateescape: bytes that are not UTF-8 (Latin-1 files) come back out unchanged
        text = original.decode("utf-8", errors="surrogateescape")
        try:
            if isinstance(patch, UnifiedDiff):
                applied = apply_hunks(text, parse_unified(patch.text)[0].hunks)
            else:
                applied = apply_search_replace(text, patch)
        except PatchConflict as exc:
            self._emit("patch_finished", path=rel, applied=False, conflict=str(exc))
            return PatchResult(rel, False, before, conflict=str(exc), candidates=exc.candidates)
        new_content = applied.text
        await self._prepare_write(p, self._added_text(text, new_content), f"patch {p}")
        if target.read_bytes() != original:  # edited while the approval prompt was open
            conflict = "file changed while waiting for approval; re-read it and regenerate the edit"
            self._emit("patch_finished", path=rel, applied=False, conflict=conflict)
            return PatchResult(rel, False, before, conflict=conflict)
        data = new_content.encode("utf-8", errors="surrogateescape")
        self._write_atomic(target, data)
        self.modified.add(rel)
        after = _sha(data)
        self._emit(
            "patch_finished",
            path=rel,
            applied=True,
            before=before,
            after=after,
            relocated=applied.relocated,
        )
        return PatchResult(rel, True, before, after, applied.relocated, applied.notes)

    @staticmethod
    def _added_text(old: str, new: str) -> str:
        """Only lines the patch introduces are checked for redaction markers."""
        old_lines = set(old.split("\n"))
        return "\n".join(line for line in new.split("\n") if line not in old_lines)

    async def move_file(self, src: str, dst: str) -> PatchResult:
        source = self._resolve(src, write=True)
        if not source.exists():
            raise ToolError(f"{src}: does not exist")
        dest = self._resolve(dst, write=True)
        if dest.exists():
            raise ToolError(f"{dst}: already exists")
        await self._prepare_write(src, None, f"move {src} -> {dst}")
        await self._prepare_write(dst, None, f"move {src} -> {dst}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        source.replace(dest)
        rel_src, rel_dst = self._rel(source), self._rel(dest)
        self.modified.update({rel_src, rel_dst})
        if rel_src in self.created:
            self.created.discard(rel_src)
            self.created.add(rel_dst)
        self._emit("patch_finished", path=rel_dst, kind="move", source=rel_src)
        return PatchResult(rel_dst, True)

    async def delete_file(self, p: str) -> PatchResult:
        path = self._resolve(p, write=True)
        if not path.is_file():
            raise ToolError(f"{p}: not a file")
        rel = self._rel(path)
        cls = ActionClass.WORKTREE_WRITE if rel in self.created else ActionClass.LOCAL_DESTRUCTIVE
        await self._authorize(cls, f"delete {rel}", rel)
        self._check_guard(rel)
        if self.checkpointer is not None:
            self.checkpointer.record_original(rel)
        before = _sha(path.read_bytes())
        path.unlink()
        self.modified.add(rel)
        self._emit("patch_finished", path=rel, kind="delete", before=before)
        return PatchResult(rel, True, before, None)


def search_replace(*pairs: tuple[str, str]) -> list[SearchReplace]:
    return [SearchReplace(a, b) for a, b in pairs]


def describe(result: PatchResult) -> dict[str, Any]:
    return {k: v for k, v in result.__dict__.items() if v not in (None, [], 0)}
