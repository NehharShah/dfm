"""Shared types for sibling-project integrations."""

from dataclasses import dataclass


@dataclass(frozen=True)
class IntegrationHealth:
    name: str
    available: bool
    version: str | None
    detail: str
