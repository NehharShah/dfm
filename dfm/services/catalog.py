"""Which local services a repository's tests need, and how to recognise them.

Sources, most explicit first: compose files (service image → kind, host port), env
templates (`.env.example`, `.env.test`, `.env.sample`; never `.env`, which holds real
secrets), and Reflex practice hints ("start Postgres before integration tests").
Only loopback hosts are managed; a non-local URL is reported and left alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import yaml

from dfm.policy.network import is_loopback

DEFAULT_PORTS = {
    "postgres": 5432,
    "redis": 6379,
    "clickhouse": 8123,
    "kafka": 9092,
    "nats": 4222,
    "mysql": 3306,
    "mongo": 27017,
    "anvil": 8545,
    "starknet-devnet": 5050,
}
_IMAGES = (
    (r"postgres|postgis|timescale", "postgres"),
    (r"redis|valkey", "redis"),
    (r"clickhouse", "clickhouse"),
    (r"kafka|redpanda", "kafka"),
    (r"(^|/)nats", "nats"),
    (r"mysql|mariadb", "mysql"),
    (r"(^|/)mongo", "mongo"),
)
_SCHEMES = {
    "postgres": "postgres",
    "postgresql": "postgres",
    "redis": "redis",
    "rediss": "redis",
    "clickhouse": "clickhouse",
    "nats": "nats",
    "mysql": "mysql",
    "mongodb": "mongo",
    "kafka": "kafka",
}
COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")
ENV_TEMPLATES = (".env.example", ".env.sample", ".env.test", ".env.template", ".env.ci")


@dataclass(frozen=True)
class ServiceSpec:
    kind: str  # postgres | redis | ... | compose (unknown image)
    name: str
    port: int
    host: str = "127.0.0.1"
    compose_file: str | None = None
    compose_service: str | None = None
    source: str = ""  # where it was detected


@dataclass
class Detection:
    services: list[ServiceSpec] = field(default_factory=lambda: [])
    skipped: list[str] = field(default_factory=lambda: [])  # e.g. non-local hosts


def _kind_of_image(image: str) -> str:
    for pattern, kind in _IMAGES:
        if re.search(pattern, image):
            return kind
    return "compose"


def _host_port(raw: Any, default: int) -> int:
    """`"5433:5432"`, `"127.0.0.1:5433:5432"`, `5432`, or the long syntax → host port."""
    if isinstance(raw, dict):
        entry = cast(dict[str, Any], raw)
        return int(entry.get("published") or entry.get("target") or default)
    text = str(raw).split("/")[0]
    parts = text.split(":")
    try:
        return int(parts[-2]) if len(parts) >= 2 else int(parts[0])
    except ValueError:
        return default


def from_compose(repo: Path) -> list[ServiceSpec]:
    out: list[ServiceSpec] = []
    for name in COMPOSE_FILES:
        path = repo / name
        if not path.is_file():
            continue
        try:
            doc = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError):
            continue
        document = cast(dict[str, Any], doc) if isinstance(doc, dict) else {}
        services = cast(dict[str, Any], document.get("services") or {})
        for service, raw in services.items():
            if not isinstance(raw, dict):
                continue
            spec = cast(dict[str, Any], raw)
            kind = _kind_of_image(str(spec.get("image", "")))
            ports = cast(list[Any], spec.get("ports") or [])
            default = DEFAULT_PORTS.get(kind, 0)
            port = _host_port(ports[0], default) if ports else default
            if port:
                out.append(
                    ServiceSpec(
                        kind, service, port, compose_file=name, compose_service=service, source=name
                    )
                )
        break  # the first compose file wins, as with `docker compose`
    return out


def from_env_templates(repo: Path) -> Detection:
    found = Detection()
    for name in ENV_TEMPLATES:
        try:
            text = (repo / name).read_text()
        except OSError:
            continue
        for match in re.finditer(
            r"^\s*([A-Z0-9_]+)\s*=\s*['\"]?([a-z][a-z0-9+]*://[^\s'\"]+)", text, re.M
        ):
            url = urlparse(match.group(2))
            kind = _SCHEMES.get(url.scheme.split("+")[0])
            if kind is None:
                continue
            host = url.hostname or "localhost"
            if not is_loopback(host):
                found.skipped.append(
                    f"{match.group(1)} in {name} points at {host}: not local, not managed"
                )
                continue
            found.services.append(
                ServiceSpec(
                    kind, kind, url.port or DEFAULT_PORTS[kind], source=f"{name}:{match.group(1)}"
                )
            )
    return found


def from_hints(hints: list[str]) -> list[ServiceSpec]:
    """Reflex practices such as "start Postgres before integration tests"."""
    out: list[ServiceSpec] = []
    for hint in hints:
        lowered = hint.lower()
        if not re.search(r"\b(start|run|needs?|requires?|launch|up)\b", lowered):
            continue
        for kind in DEFAULT_PORTS:
            if re.search(rf"\b{re.escape(kind)}(ql)?\b", lowered):
                out.append(ServiceSpec(kind, kind, DEFAULT_PORTS[kind], source="reflex practice"))
    return out


def detect(repo: Path, hints: list[str] | None = None) -> Detection:
    """Services the repository's tests appear to need, de-duplicated by (kind, port)."""
    env = from_env_templates(repo)
    found = Detection(skipped=env.skipped)
    seen: set[tuple[str, int]] = set()
    for spec in [*from_compose(repo), *env.services, *from_hints(hints or [])]:
        key = (spec.kind, spec.port)
        if key not in seen:
            seen.add(key)
            found.services.append(spec)
    return found
