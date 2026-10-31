"""JSON Schemas in the exact shape `fm schema object` emits, from pydantic models.

Shape (verified against the installed `fm`, golden files in tests/fixtures/fm_schema):
object → {type, title, properties, x-order, required, additionalProperties: false};
nested models → `$defs` + `{"$ref": "#/$defs/Name"}`; arrays → {type: array, items};
optional (`X | None`) → present in properties, absent from `required`;
`Literal[...]` / `Enum` → {type: string, enum: [...]} (fm honors enums).
The same schema drives MLX structured prompting, so it stays plain JSON Schema.
"""

import types
from enum import Enum
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel

_SCALARS: dict[Any, str] = {str: "string", int: "integer", float: "number", bool: "boolean"}


def fm_schema(model: type[BaseModel]) -> dict[str, Any]:
    defs: dict[str, dict[str, Any]] = {}
    root = _object_schema(model, defs)
    if defs:
        root["$defs"] = defs
    return root


def _object_schema(model: type[BaseModel], defs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, info in model.model_fields.items():
        schema, optional = _type_schema(info.annotation, defs)
        if info.description:
            schema = {**schema, "description": info.description}
        properties[name] = schema
        if not optional:
            required.append(name)
    return {
        "type": "object",
        "title": model.__name__,
        "properties": properties,
        "x-order": list(properties),
        "required": required,
        "additionalProperties": False,
    }


def _type_schema(annotation: Any, defs: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], bool]:
    if annotation in _SCALARS:
        return {"type": _SCALARS[annotation]}, False
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, types.UnionType):
        rest = [a for a in args if a is not type(None)]
        if len(rest) == 1 and len(args) == 2:
            inner, _ = _type_schema(rest[0], defs)
            return inner, True
        return {"anyOf": [_type_schema(a, defs)[0] for a in rest]}, type(None) in args
    if origin is Literal:
        kind = (
            "integer"
            if all(isinstance(a, int) and not isinstance(a, bool) for a in args)
            else "string"
        )
        return {"type": kind, "enum": list(args)}, False
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return {"type": "string", "enum": [m.value for m in annotation]}, False
    if origin in (list, tuple, set, frozenset):
        item = args[0] if args else str
        inner, _ = _type_schema(item, defs)
        return {"type": "array", "items": inner}, False
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        name = annotation.__name__
        if name not in defs:
            defs[name] = {}  # placeholder guards recursion
            defs[name] = _object_schema(annotation, defs)
        return {"$ref": f"#/$defs/{name}"}, False
    raise TypeError(f"no fm schema mapping for {annotation!r}")
