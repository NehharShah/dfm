"""RepoCanon provider: repository-operating model (commands, topology, conventions)."""

from dfm.integrations.repocanon.provider import (
    COMMAND_KINDS,
    CommandKind,
    DegradedCanon,
    RepoCanonProvider,
    ScopedCommand,
    boundaries,
    commands,
    degraded_discovery,
    is_degraded,
    package_for,
    repocanon_available,
    scoped_commands,
    summary,
)

__all__ = [
    "COMMAND_KINDS",
    "CommandKind",
    "DegradedCanon",
    "RepoCanonProvider",
    "ScopedCommand",
    "boundaries",
    "commands",
    "degraded_discovery",
    "is_degraded",
    "package_for",
    "repocanon_available",
    "scoped_commands",
    "summary",
]
