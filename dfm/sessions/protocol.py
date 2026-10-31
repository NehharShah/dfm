"""DFM session trace protocol, version 1 (ADR 0006).

One JSONL file per session: a header record, then one event per line. This is
the contract Reflex's DFM adapter reads; `docs/protocol/dfm-session-v1.schema.json`
is the same contract as JSON Schema.

Optional header fields: `dfm_pid` (the DFM process, so a live session is never resumed twice),
`project_path_hash` (sha256 of the repository path, first 12 hex chars:
the key Reflex uses to group sessions by project; the path itself is never stored).

Compatibility: adding event types or optional fields keeps schema_version 1
(readers ignore what they don't know); removing or renaming fields bumps it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

HEADER_FIELDS: dict[str, type | tuple[type, ...]] = {
    "schema_version": int,
    "session_id": str,
    "source": str,
    "dfm_version": str,
    "repo_hash": str,
    "repo_name": str,
    "started_at": str,
    "mode": str,
    "request": str,
}

# Required payload fields per event type (beyond type, seq, at).
EVENT_FIELDS: dict[str, dict[str, type | tuple[type, ...]]] = {
    "user_request": {"text": str},
    "route_decision": {"route": str, "source": str},
    "repocanon_loaded": {},
    "repokg_loaded": {},
    "reflex_loaded": {},
    "context_selected": {"items": list},
    "file_read": {"path": str},
    "symbol_lookup": {"symbol": str},
    "command_started": {"argv": list},
    "command_finished": {"argv": list},
    "model_started": {"model": str},
    "model_finished": {"model": str},
    "patch_started": {"path": str},
    "patch_finished": {"path": str},
    "test_started": {"argv": list},
    "test_finished": {"ok": bool},
    "failure": {"error": str},
    "correction": {"text": str},
    "recovery": {"failed": str, "fallback": str, "reason": str},
    "verification": {"check": str, "ok": bool},
    "task_completed": {"status": str},
    "policy_decision": {"decision": str, "action_class": str},
    "plan": {"plan": dict},
    "checkpoint": {"stage": str},
    "note": {},
}
STATUSES = {"completed", "failed", "aborted", "interrupted", "running"}
FORBIDDEN_KEYS = {"content", "file_content", "body", "before", "after"}  # file bodies never stored


@dataclass(frozen=True)
class ProtocolError:
    line: int
    message: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.message}"


def _check_fields(
    record: dict[str, Any], fields: dict[str, type | tuple[type, ...]], line: int
) -> list[ProtocolError]:
    errors: list[ProtocolError] = []
    for key, kind in fields.items():
        if key not in record:
            errors.append(ProtocolError(line, f"missing {key!r}"))
        elif not isinstance(record[key], kind):
            errors.append(ProtocolError(line, f"{key!r} should be {kind}"))
    return errors


def validate_records(records: list[dict[str, Any]]) -> list[ProtocolError]:
    if not records:
        return [ProtocolError(0, "empty trace")]
    header = records[0]
    errors = _check_fields(header, HEADER_FIELDS, 1)
    if header.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            ProtocolError(
                1, f"schema_version {header.get('schema_version')} is not {SCHEMA_VERSION}"
            )
        )
    if header.get("source") != "dfm":
        errors.append(ProtocolError(1, "source must be 'dfm'"))
    last_seq = 0
    for index, event in enumerate(records[1:], start=2):
        kind = event.get("type")
        if not isinstance(event.get("seq"), int) or event["seq"] <= last_seq:
            errors.append(ProtocolError(index, "seq must be an increasing integer"))
        else:
            last_seq = event["seq"]
        if not isinstance(event.get("at"), str):
            errors.append(ProtocolError(index, "missing 'at' timestamp"))
        if kind not in EVENT_FIELDS:
            continue  # unknown types are allowed (forward compatible); readers skip them
        errors += _check_fields(event, EVENT_FIELDS[kind], index)
        if kind == "task_completed" and event.get("status") not in STATUSES:
            errors.append(ProtocolError(index, f"unknown status {event.get('status')!r}"))
        leaked = FORBIDDEN_KEYS & set(event)
        if leaked:
            errors.append(
                ProtocolError(index, f"file bodies are not allowed in traces: {sorted(leaked)}")
            )
    return errors


def validate_file(path: Path) -> list[ProtocolError]:
    records: list[dict[str, Any]] = []
    lines = path.read_text().splitlines()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if number == len(lines):
                break  # a torn last line after a crash is tolerated
            return [ProtocolError(number, "invalid JSON")]
    return validate_records(records)


def json_schema() -> dict[str, Any]:
    """The same rules as JSON Schema (draft 2020-12), for other implementations (Reflex)."""

    def jtype(kind: type | tuple[type, ...]) -> str:
        return {str: "string", int: "integer", bool: "boolean", list: "array", dict: "object"}[
            kind if isinstance(kind, type) else kind[0]
        ]

    events: list[dict[str, Any]] = []
    for name, fields in EVENT_FIELDS.items():
        events.append(
            {
                "type": "object",
                "properties": {
                    "type": {"const": name},
                    **{k: {"type": jtype(v)} for k, v in fields.items()},
                },
                "required": ["type", "seq", "at", *fields],
            }
        )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://github.com/NehharShah/dfm/docs/protocol/dfm-session-v1.schema.json",
        "title": "DFM session trace v1 (one JSON object per line)",
        "$defs": {
            "header": {
                "type": "object",
                "properties": {k: {"type": jtype(v)} for k, v in HEADER_FIELDS.items()}
                | {"schema_version": {"const": SCHEMA_VERSION}, "source": {"const": "dfm"}},
                "required": list(HEADER_FIELDS),
            },
            "event": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "seq": {"type": "integer", "minimum": 1},
                    "at": {"type": "string"},
                },
                "required": ["type", "seq", "at"],
                "not": {"anyOf": [{"required": [k]} for k in sorted(FORBIDDEN_KEYS)]},
                "oneOf": [*events, {"properties": {"type": {"not": {"enum": list(EVENT_FIELDS)}}}}],
            },
        },
        "oneOf": [{"$ref": "#/$defs/header"}, {"$ref": "#/$defs/event"}],
    }
