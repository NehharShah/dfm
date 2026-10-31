"""Where every piece of model context came from.

A `ContextItem` is created only through `ContextItem.make`, which redacts the
content (CONTEXT profile) and hashes the redacted text, so nothing unredacted
can reach a model by this path. Traces store `provenance()` (no content).
Historical evidence (Reflex) is rendered with an explicit label: it is never
presented as current repository truth.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from dfm.secrets import redact_text
from dfm.security.injection import untrusted_block

Source = Literal[
    "user", "repo", "repocanon", "repokg", "reflex", "git", "lsp", "compiler", "test", "github"
]

# Context priority: 1 = most important.
PRIORITY: dict[str, int] = {
    "user_reference": 1,
    "failure": 2,
    "diff": 3,
    "repokg_module": 4,
    "repocanon": 5,
    "reflex": 6,
    "lsp": 7,
    "test": 8,
    "neighbor": 9,
    "docs": 10,
}

_TRUST: dict[str, str] = {
    "user": "user",
    "repocanon": "deterministic",
    "repokg": "deterministic",
    "compiler": "tool-output",
    "test": "tool-output",
    "lsp": "tool-output",
    "git": "tool-output",
    "reflex": "historical",
}


@dataclass(frozen=True)
class ContextItem:
    source: Source
    content: str  # redacted
    reason: str
    content_hash: str
    path: str | None = None
    line_range: tuple[int, int] | None = None
    confidence: float = 1.0
    priority: int = 10
    historical: bool = False
    retrieved_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )

    @staticmethod
    def make(
        source: Source,
        content: str,
        reason: str,
        *,
        path: str | None = None,
        line_range: tuple[int, int] | None = None,
        confidence: float = 1.0,
        priority: int = 10,
    ) -> ContextItem:
        clean = redact_text(content)
        return ContextItem(
            source=source,
            content=clean,
            reason=reason,
            content_hash=hashlib.sha256(clean.encode()).hexdigest(),
            path=path,
            line_range=line_range,
            confidence=confidence,
            priority=priority,
            historical=source == "reflex",
        )

    @property
    def location(self) -> str:
        if self.path is None:
            return self.source
        if self.line_range is None:
            return self.path
        return f"{self.path}:{self.line_range[0]}-{self.line_range[1]}"

    @property
    def est_tokens(self) -> int:
        return max(1, len(self.content) // 4)

    def provenance(self) -> dict[str, Any]:
        """Everything except the content: what the trace and `dfm context` show."""
        return {
            "source": self.source,
            "path": self.path,
            "line_range": list(self.line_range) if self.line_range else None,
            "reason": self.reason,
            "retrieved_at": self.retrieved_at,
            "content_hash": self.content_hash,
            "confidence": self.confidence,
            "priority": self.priority,
            "historical": self.historical,
            "chars": len(self.content),
        }

    @staticmethod
    def from_provenance(data: dict[str, Any], content: str = "") -> ContextItem:
        line_range = data.get("line_range")
        return ContextItem(
            source=data["source"],
            content=content,
            reason=data["reason"],
            content_hash=data["content_hash"],
            path=data.get("path"),
            line_range=(int(line_range[0]), int(line_range[1])) if line_range else None,
            confidence=float(data.get("confidence", 1.0)),
            priority=int(data.get("priority", 10)),
            historical=bool(data.get("historical", False)),
            retrieved_at=data.get("retrieved_at", ""),
        )

    def render(self) -> str:
        note = (
            "historical evidence from past sessions, not current repository state"
            if self.historical
            else None
        )
        return untrusted_block(
            self.location, self.content, trust=_TRUST.get(self.source, "untrusted"), note=note
        )
