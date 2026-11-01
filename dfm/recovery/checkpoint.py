"""Checkpoints after analysis, plan, edit, test, repair and verification.

A checkpoint is enough to reconstruct a session: loop state, Git HEAD, the
dirty-file fingerprint, every file DFM modified (before/after hashes), the plan
and the pending steps. The pre-edit content of each file DFM modifies is kept in
a content-addressed store so DFM can show or undo its own edits; secret-bearing
files are recorded by hash only.

Layout (inside the repo's excluded `.dfm/` directory):
    .dfm/checkpoints/<session>/<n>.json
    .dfm/checkpoints/<session>/originals.json   path -> sha256 of first-seen content
    .dfm/originals/<sha256>                      content
"""

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from dfm.secrets import is_secret_path
from dfm.sessions import ensure_repo_state_dir
from dfm.tools.git.state import worktree_state


class Stage(StrEnum):
    ANALYSIS = "analysis"
    PLAN = "plan"
    EDIT = "edit"
    TEST = "test"
    REPAIR = "repair"
    VERIFICATION = "verification"


@dataclass(frozen=True)
class FileChange:
    before: str | None  # sha256, None if DFM created the file
    after: str | None  # sha256, None if DFM deleted the file


@dataclass(frozen=True)
class Checkpoint:
    session_id: str
    n: int
    stage: Stage
    created_at: float
    head: str | None
    dirty: dict[str, str | None]
    fingerprint: str
    modified: dict[str, FileChange] = field(default_factory=lambda: {})
    plan: dict[str, Any] | None = None
    pending: list[str] = field(default_factory=lambda: [])
    state: dict[str, Any] = field(default_factory=lambda: {})

    @staticmethod
    def from_json(data: dict[str, Any]) -> "Checkpoint":
        modified = {p: FileChange(**c) for p, c in data.get("modified", {}).items()}
        return Checkpoint(
            session_id=data["session_id"],
            n=int(data["n"]),
            stage=Stage(data["stage"]),
            created_at=float(data["created_at"]),
            head=data.get("head"),
            dirty=dict(data.get("dirty", {})),
            fingerprint=data.get("fingerprint", ""),
            modified=modified,
            plan=data.get("plan"),
            pending=list(data.get("pending", [])),
            state=dict(data.get("state", {})),
        )


@dataclass
class UndoResult:
    restored: list[str] = field(default_factory=lambda: [])
    removed: list[str] = field(default_factory=lambda: [])  # files DFM had created
    skipped: list[str] = field(default_factory=lambda: [])  # "path: why"


@dataclass(frozen=True)
class CheckpointRef:
    n: int
    path: Path


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    tmp.replace(path)


class Checkpointer:
    def __init__(self, repo: Path, session_id: str) -> None:
        self.repo = repo
        self.session_id = session_id
        state = ensure_repo_state_dir(repo)
        self.dir = state / "checkpoints" / session_id
        self.originals_dir = state / "originals"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.originals_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self.dir / "originals.json"

    # ---------------------------------------------------------- originals

    def _originals_index(self) -> dict[str, str | None]:
        try:
            return json.loads(self._index_path.read_text())
        except (OSError, ValueError):
            return {}

    def record_original(self, rel_path: str) -> str | None:
        """Remember a file's content before DFM's first edit to it. Returns its sha256."""
        index = self._originals_index()
        if rel_path in index:
            return index[rel_path]
        target = self.repo / rel_path
        if not target.exists():
            index[rel_path] = None  # DFM creates it
        else:
            data = target.read_bytes()
            digest = _sha(data)
            if not is_secret_path(rel_path):
                blob = self.originals_dir / digest
                if not blob.exists():
                    blob.write_bytes(data)
                    blob.chmod(0o600)
            index[rel_path] = digest
        _atomic_write(self._index_path, json.dumps(index, indent=1, sort_keys=True))
        return index[rel_path]

    def original_content(self, rel_path: str) -> bytes | None:
        """The bytes recorded before DFM's first edit, or None when only the hash was kept or
        the blob no longer matches it. The sandbox denies repository code writes under
        `.dfm/`; this check is the second line, so a blob that was rewritten anyway is never
        restored as "the original"."""
        digest = self._originals_index().get(rel_path)
        if digest is None:
            return None
        blob = self.originals_dir / digest
        if not blob.exists():
            return None
        data = blob.read_bytes()
        return data if _sha(data) == digest else None

    def undo(self, left: dict[str, str | None]) -> "UndoResult":
        """Put every file DFM edited under this id back as it was before DFM's first edit.

        `left` maps each path to the sha256 DFM left it with (None: DFM deleted it). A file
        whose content changed since is someone else's work now and is not touched. Secret files
        were recorded by hash only and cannot be restored.
        """
        result = UndoResult()
        index = self._originals_index()
        repo = self.repo.resolve()
        for rel_path, expected in sorted(left.items()):
            target = (repo / rel_path).resolve()
            if not target.is_relative_to(repo) or rel_path not in index:
                result.skipped.append(f"{rel_path}: not a file DFM edited here")
                continue
            current = _sha(target.read_bytes()) if target.is_file() else None
            if current != expected:
                result.skipped.append(f"{rel_path}: changed since DFM edited it")
                continue
            original = index[rel_path]
            if original is None:  # DFM created it
                if target.is_file():
                    target.unlink()
                    result.removed.append(rel_path)
                continue
            data = self.original_content(rel_path)
            if data is None:
                why = (
                    "only its hash was kept (secret file)"
                    if is_secret_path(rel_path)
                    else "its recorded original is missing or does not match its hash"
                )
                result.skipped.append(f"{rel_path}: {why}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            result.restored.append(rel_path)
        return result

    def modified_files(self) -> dict[str, FileChange]:
        changes: dict[str, FileChange] = {}
        for rel_path, before in self._originals_index().items():
            current = self.repo / rel_path
            after = _sha(current.read_bytes()) if current.is_file() else None
            changes[rel_path] = FileChange(before, after)
        return changes

    # ---------------------------------------------------------- checkpoints

    def _numbers(self) -> list[int]:
        return sorted(int(p.stem) for p in self.dir.glob("*.json") if p.stem.isdigit())

    def save(
        self,
        stage: Stage,
        *,
        plan: dict[str, Any] | None = None,
        pending: list[str] | None = None,
        state: dict[str, Any] | None = None,
    ) -> CheckpointRef:
        wt = worktree_state(self.repo)
        n = (self._numbers() or [0])[-1] + 1
        checkpoint = Checkpoint(
            session_id=self.session_id,
            n=n,
            stage=stage,
            created_at=time.time(),
            head=wt.head,
            dirty=wt.dirty,
            fingerprint=wt.fingerprint,
            modified=self.modified_files(),
            plan=plan,
            pending=pending or [],
            state=state or {},
        )
        path = self.dir / f"{n}.json"
        _atomic_write(path, json.dumps(asdict(checkpoint), indent=1, sort_keys=True))
        return CheckpointRef(n, path)

    def latest(self) -> Checkpoint | None:
        for n in reversed(self._numbers()):
            try:
                return Checkpoint.from_json(json.loads((self.dir / f"{n}.json").read_text()))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return None

    def all(self) -> list[Checkpoint]:
        out: list[Checkpoint] = []
        for n in self._numbers():
            try:
                out.append(Checkpoint.from_json(json.loads((self.dir / f"{n}.json").read_text())))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return out
