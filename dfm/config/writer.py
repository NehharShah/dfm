"""Write single values into the user's config file (used by `dfm setup` / `dfm models use`).

Reads the existing TOML, sets one dotted key, writes it back atomically and keeps
the previous file as `config.toml.bak`. Comments in the old file are not
preserved (the backup keeps them). Only the user config is ever written; repo
configs are the developer's files.
"""

from __future__ import annotations

import json
import shutil
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from dfm.config.loader import load_config, validate_user_layer
from dfm.config.paths import user_config_path


def _value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(_value(x) for x in cast(list[Any], v)) + "]"
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def dumps(data: Mapping[str, Any]) -> str:
    lines: list[str] = []

    def emit(table: Mapping[str, Any], prefix: str) -> None:
        scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
        tables: dict[str, Mapping[str, Any]] = {
            k: cast(Mapping[str, Any], v) for k, v in table.items() if isinstance(v, dict)
        }
        if scalars and prefix:
            lines.append(f"[{prefix}]")
        for key, value in scalars.items():
            lines.append(f"{key} = {_value(value)}")
        if scalars:
            lines.append("")
        for key, value in tables.items():
            emit(value, f"{prefix}.{key}" if prefix else key)

    emit(data, "")
    return "\n".join(lines).rstrip() + "\n"


def set_user_value(dotted: str, value: Any, env: Mapping[str, str] | None = None) -> Path:
    path = user_config_path(env)
    data: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    node = data
    *parents, last = dotted.split(".")
    for part in parents:
        node = cast(dict[str, Any], node.setdefault(part, {}))
    node[last] = value
    text = dumps(data)
    # validated before the file is touched: a bad value must not leave a config that breaks
    # every later command
    validate_user_layer(tomllib.loads(text), path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(".toml.bak"))
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(text)
    tmp.replace(path)
    load_config(None, env=env)  # the full load (layers, warnings) succeeds too
    return path
