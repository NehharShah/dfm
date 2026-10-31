"""Durable record of what a DFM session actually did.

One JSONL file per session in `<data>/sessions/<id>.jsonl`: a header record, then
one event per line. Everything is redacted (TRACE profile) before it is written,
file bodies are never stored (only hashes and sizes), and a crash mid-write
leaves a readable file (a torn last line is ignored on load).

This is the format Reflex ingests (ADR 0006; additions are backwards compatible within
schema_version 1).
"""

import hashlib
import json
import os
import secrets
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from dfm import __version__
from dfm.policy import Policy, PolicyVerdict, ProposedAction
from dfm.secrets import Profile, redact_deep, redact_text
from dfm.sessions.protocol import EVENT_FIELDS, SCHEMA_VERSION

SessionStatus = Literal["running", "completed", "failed", "aborted", "interrupted"]

EVENT_TYPES = frozenset(EVENT_FIELDS)

# Payload keys that would carry file bodies; replaced by a hash and size.
_BODY_KEYS = frozenset({"content", "file_content", "body", "before", "after"})


def new_session_id() -> str:
    """Sortable: UTC timestamp plus randomness."""
    return f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{secrets.token_hex(3)}"


def repo_hash(repo: Path) -> str:
    return hashlib.sha256(str(Path(os.path.realpath(repo))).encode()).hexdigest()[:16]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _strip_bodies(payload: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key in _BODY_KEYS and isinstance(value, str | bytes):
            data = value.encode() if isinstance(value, str) else value
            out[f"{key}_sha256"] = hashlib.sha256(data).hexdigest()
            out[f"{key}_bytes"] = len(data)
        else:
            out[key] = value
    return out


@dataclass
class Session:
    id: str
    path: Path
    repo: Path
    seq: int = 0


@dataclass(frozen=True)
class SessionRecord:
    header: dict[str, Any]
    events: list[dict[str, Any]]
    torn_tail: bool = False

    @property
    def status(self) -> SessionStatus:
        for event in reversed(self.events):
            if event["type"] == "task_completed":
                return cast(SessionStatus, event.get("status", "completed"))
        return "running"


@dataclass(frozen=True)
class SessionSummary:
    id: str
    repo_name: str
    started_at: str
    request: str
    status: str
    mode: str = ""
    updated_at: float = field(default=0.0)


def _exclude_file(repo: Path) -> Path | None:
    """The info/exclude Git reads for `repo`: in a linked worktree or a submodule `.git` is a
    file pointing elsewhere, so ask Git where it is."""
    if not (repo / ".git").exists():
        return None
    try:
        out = subprocess.run(  # noqa: S603
            ["git", "-C", str(repo), "rev-parse", "--git-path", "info/exclude"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        out = None
    if out is not None and out.returncode == 0 and out.stdout.strip():
        path = Path(out.stdout.strip())
        return path if path.is_absolute() else repo / path
    return repo / ".git" / "info" / "exclude" if (repo / ".git").is_dir() else None


def ensure_repo_state_dir(repo: Path) -> Path:
    """Create `<repo>/.dfm/` and keep it out of Git via `.git/info/exclude` (never .gitignore)."""
    state = repo / ".dfm"
    state.mkdir(exist_ok=True)
    exclude = _exclude_file(repo)
    if exclude is not None:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        current = exclude.read_text() if exclude.exists() else ""
        if not any(
            line.strip() in {".dfm", ".dfm/", "/.dfm", "/.dfm/"} for line in current.splitlines()
        ):
            sep = "" if current.endswith("\n") or not current else "\n"
            exclude.write_text(f"{current}{sep}/.dfm/\n")
    return state


class SessionStore:
    def __init__(self, sessions_dir: Path) -> None:
        self.dir = sessions_dir

    def _path(self, session_id: str) -> Path:
        if not session_id.replace("-", "").replace("T", "").isalnum():
            raise ValueError(f"invalid session id {session_id!r}")
        return self.dir / f"{session_id}.jsonl"

    def create(self, repo: Path, request: str, *, mode: str = "chat") -> Session:
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        session = Session(new_session_id(), Path(), Path(os.path.realpath(repo)))
        session.path = self._path(session.id)
        clean_request = redact_text(request, Profile.TRACE)
        started_at = _now()
        header: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "session_id": session.id,
            "source": "dfm",
            "dfm_version": __version__,
            "repo_hash": repo_hash(session.repo),
            # Reflex groups sessions by sha256(project path)[:12]; same algorithm, no path stored.
            "project_path_hash": hashlib.sha256(str(session.repo).encode()).hexdigest()[:12],
            "repo_name": session.repo.name,
            "started_at": started_at,
            "mode": mode,
            "request": clean_request,
            "dfm_pid": os.getpid(),  # resume refuses sessions whose process is still alive
        }
        self._write(session.path, header, durable=True)
        self._index_update(
            session.repo,
            session.id,
            {
                "request": clean_request[:200],
                "started_at": started_at,
                "mode": mode,
                "status": "running",
            },
        )
        return session

    def append(
        self, session: Session, event_type: str, *, durable: bool = False, **payload: Any
    ) -> dict[str, Any]:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown session event type {event_type!r}")
        missing = [k for k in EVENT_FIELDS[event_type] if k not in payload]
        if missing:
            raise ValueError(
                f"{event_type} event is missing {missing} (see dfm/sessions/protocol.py)"
            )
        session.seq += 1
        event = {
            "type": event_type,
            "seq": session.seq,
            "at": _now(),
            **redact_deep(_strip_bodies(payload), Profile.TRACE),
        }
        self._write(session.path, event, durable=durable or event_type == "task_completed")
        if event_type == "task_completed":
            self._index_update(
                session.repo, session.id, {"status": payload.get("status", "completed")}
            )
        return event

    def complete(self, session: Session, status: SessionStatus, summary: str = "") -> None:
        self.append(session, "task_completed", status=status, summary=summary)

    def open(self, session_id: str, repo: Path) -> Session:
        """Reopen a session for appending (resume). The trace stores no repo path, only its hash."""
        record = self.load(session_id)
        real = Path(os.path.realpath(repo))
        if record.header.get("repo_hash") != repo_hash(real):
            raise ValueError(f"session {session_id} belongs to a different repository")
        seq = max((int(e.get("seq", 0)) for e in record.events), default=0)
        return Session(session_id, self._path(session_id), real, seq)

    def load(self, session_id: str) -> SessionRecord:
        path = self._path(session_id)
        lines = path.read_text().splitlines()
        records: list[dict[str, Any]] = []
        torn = False
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                if i == len(lines) - 1:
                    torn = True
                    break
                raise
        if not records:
            raise ValueError(f"session {session_id} has no header")
        return SessionRecord(records[0], records[1:], torn)

    def list_sessions(self, repo: Path | None = None) -> list[SessionSummary]:
        if not self.dir.exists():
            return []
        wanted = repo_hash(repo) if repo is not None else None
        out: list[SessionSummary] = []
        for path in sorted(self.dir.glob("*.jsonl"), reverse=True):
            try:
                record = self.load(path.stem)
            except (OSError, ValueError):
                continue
            header = record.header
            if wanted is not None and header.get("repo_hash") != wanted:
                continue
            out.append(
                SessionSummary(
                    id=path.stem,
                    repo_name=str(header.get("repo_name", "")),
                    started_at=str(header.get("started_at", "")),
                    request=str(header.get("request", "")),
                    status=record.status,
                    mode=str(header.get("mode", "")),
                    updated_at=path.stat().st_mtime,
                )
            )
        return out

    def latest(self, repo: Path, *, unfinished: bool = False) -> SessionSummary | None:
        for summary in self.list_sessions(repo):
            if not unfinished or summary.status == "running":
                return summary
        return None

    @staticmethod
    def _write(path: Path, record: Mapping[str, Any], *, durable: bool) -> None:
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode())
            if durable:
                os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _index_update(repo: Path, session_id: str, fields: Mapping[str, Any]) -> None:
        state = ensure_repo_state_dir(repo)
        index_path = state / "sessions.json"
        try:
            index: dict[str, Any] = json.loads(index_path.read_text())
        except (OSError, ValueError):
            index = {}
        entry: dict[str, Any] = index.get(session_id, {})
        entry.update(fields)
        entry["updated_at"] = time.time()
        index[session_id] = entry
        if len(index) > 200:  # keep the per-repo pointer file small
            for old in sorted(index)[:-200]:
                index.pop(old)
        tmp = index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(index, indent=1, sort_keys=True))
        tmp.replace(index_path)


def trace_policy(policy: Policy, store: SessionStore, session: Session) -> None:
    """Record every final policy verdict in the session trace."""

    def listener(action: ProposedAction, verdict: PolicyVerdict) -> None:
        store.append(
            session,
            "policy_decision",
            kind=action.kind,
            action_class=action.action_class.value,
            summary=action.summary,
            proposed_by=action.proposed_by,
            decision=verdict.decision.value,
            reason=verdict.reason,
            approved_by=verdict.approved_by,
            network=verdict.network.value if verdict.network else None,
        )

    policy.subscribe(listener)
