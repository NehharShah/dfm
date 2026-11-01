"""Merge defaults < user config < repo config < CLI flags into one validated DFMConfig.

The repo layer is untrusted (it is a file in someone else's repository). It is an
allowlist: it may tighten `network.*`, move a boolean toward its safe value (turn an
integration or worktree consent off, never on), and lower `limits.*`. Every other key,
including anything under `models`, is refused. Rejected repo keys are skipped with a
warning instead of failing, so a hostile or broken repo config cannot block DFM.
"""

import copy
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ValidationError

from dfm.config.model import DFMConfig
from dfm.config.paths import repo_config_path, user_config_path

Source = Literal["default", "user", "repo", "cli"]

_RULE_RANK = {"allow": 0, "ask": 1, "deny": 2}

# Boolean settings where only one value is the safe one; the repo layer may move
# toward it but never away from it. Off is safe for integrations (less runs, less leaves)
# and for standing worktree consent (ask instead of allow).
_SAFE_BOOL = {
    "security.repo_filesystem_boundary": True,
    "security.allow_production_cluster_writes": False,
    "git.prefer_worktree_for_large_tasks": False,
    "integrations.repocanon.enabled": False,
    "integrations.repokg.enabled": False,
    "integrations.reflex.enabled": False,
    "integrations.reflex.session_export": False,
    "integrations.reflex.use_skills": False,
    "integrations.reflex.use_handbook": False,
}
# Numeric budgets the repo layer may lower, never raise.
_REPO_LOWER_ONLY = ("limits.",)

_REPO_FORBIDDEN_SECTIONS = {"models"}


class ConfigError(ValueError):
    """The user's own config is unreadable or invalid. DFM refuses to guess."""


@dataclass(frozen=True)
class ConfigOverrides:
    offline: bool = False
    local_models: bool = False
    # Dotted keys set last, like CLI flags (DFM's own callers only, e.g. benchmark ablations).
    settings: Mapping[str, Any] = field(default_factory=lambda: {})


@dataclass(frozen=True)
class LoadedConfig:
    config: DFMConfig
    sources: Mapping[str, Source]
    warnings: tuple[str, ...]
    user_path: Path
    repo_path: Path | None


def load_config(
    repo: Path | None = None,
    overrides: ConfigOverrides | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> LoadedConfig:
    env = os.environ if env is None else env
    overrides = overrides or ConfigOverrides()
    warnings: list[str] = []

    data: dict[str, Any] = DFMConfig().model_dump()
    sources: dict[str, Source] = dict.fromkeys(flatten(data), "default")

    user_path = user_config_path(env)
    user_raw = _read_user(user_path)
    user_raw = _drop_unknown(user_raw, DFMConfig, "", str(user_path), warnings)
    for key, value in flatten(user_raw).items():
        _set(data, key, value)
        sources[key] = "user"
    config = _validate(data, user_path)
    if config.network.remote_inference != "deny":
        warnings.append(
            f"{user_path}: network.remote_inference = "
            f"'{config.network.remote_inference}'. DFM ships no remote model provider, "
            "so this has no effect today."
        )

    repo_path = repo_config_path(repo) if repo is not None else None
    if repo_path is not None:
        data = _apply_repo_layer(data, sources, repo_path, warnings)

    if overrides.offline:
        for key in data["network"]:
            data["network"][key] = "deny"
            sources[f"network.{key}"] = "cli"
    if overrides.local_models:
        data["network"]["remote_inference"] = "deny"
        sources["network.remote_inference"] = "cli"
    for key, value in overrides.settings.items():
        _set(data, key, value)
        sources[key] = "cli"

    return LoadedConfig(
        config=_validate(data, None),
        sources=sources,
        warnings=tuple(warnings),
        user_path=user_path,
        repo_path=repo_path,
    )


def _apply_repo_layer(
    data: dict[str, Any], sources: dict[str, Source], path: Path, warnings: list[str]
) -> dict[str, Any]:
    label = str(path)
    try:
        raw = _read_toml(path)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        warnings.append(f"{label}: ignored, cannot be read ({exc})")
        return data
    raw = _drop_unknown(raw, DFMConfig, "", label, warnings)
    for key, value in flatten(raw).items():
        if key.split(".")[0] in _REPO_FORBIDDEN_SECTIONS:
            warnings.append(f"{label}: '{key}' ignored, a repository cannot choose models")
            continue
        if not _repo_may_set(key):
            warnings.append(f"{label}: '{key}' ignored, a repository cannot set this setting")
            continue
        candidate = copy.deepcopy(data)
        _set(candidate, key, value)
        try:
            DFMConfig.model_validate(candidate)
        except ValidationError:
            warnings.append(f"{label}: '{key}' ignored, invalid value {value!r}")
            continue
        current = _get(data, key)
        if _loosens(key, current, value):
            warnings.append(
                f"{label}: '{key} = {value!r}' ignored, a repository may only tighten "
                f"this setting (effective: {current!r})"
            )
            continue
        data = candidate
        sources[key] = "repo"
    return data


def _repo_may_set(key: str) -> bool:
    """The allowlist: anything not listed here is refused from the repo layer outright."""
    return key.startswith("network.") or key in _SAFE_BOOL or key.startswith(_REPO_LOWER_ONLY)


def _loosens(key: str, current: Any, proposed: Any) -> bool:
    """For an allowlisted key: does `proposed` move away from the safe side of `current`?"""
    if key.startswith("network."):
        return _RULE_RANK[proposed] < _RULE_RANK[current]
    if key in _SAFE_BOOL:
        safe = _SAFE_BOOL[key]
        return current == safe and proposed != safe
    if key.startswith(_REPO_LOWER_ONLY):
        return bool(proposed > current)
    return True


def _read_user(path: Path) -> dict[str, Any]:
    try:
        return _read_toml(path)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: cannot be read ({exc})") from exc


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def validate_user_layer(user: Mapping[str, Any], origin: Path | None = None) -> DFMConfig:
    """The configuration these user settings produce over the defaults; ConfigError if invalid."""
    data: dict[str, Any] = DFMConfig().model_dump()
    for key, value in flatten(user).items():
        _set(data, key, value)
    return _validate(data, origin)


def _validate(data: dict[str, Any], origin: Path | None) -> DFMConfig:
    try:
        return DFMConfig.model_validate(data)
    except ValidationError as exc:
        where = f"{origin}: " if origin else ""
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(f"{where}invalid configuration: {problems}") from exc


def _drop_unknown(
    raw: Mapping[str, Any],
    model: type[BaseModel],
    prefix: str,
    label: str,
    warnings: list[str],
) -> dict[str, Any]:
    known: dict[str, Any] = {}
    for key, value in raw.items():
        dotted = f"{prefix}{key}"
        field = model.model_fields.get(key)
        if field is None:
            warnings.append(f"{label}: unknown key '{dotted}' ignored")
            continue
        section = field.annotation
        if isinstance(section, type) and issubclass(section, BaseModel) and isinstance(value, dict):
            table = cast(dict[str, Any], value)
            known[key] = _drop_unknown(table, section, f"{dotted}.", label, warnings)
        else:
            known[key] = value
    return known


def flatten(tree: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """Nested tables to dotted keys: {"network": {"github": "ask"}} -> {"network.github": "ask"}."""
    out: dict[str, Any] = {}
    for key, value in tree.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten(cast(dict[str, Any], value), f"{dotted}."))
        else:
            out[dotted] = value
    return out


def _get(tree: Mapping[str, Any], dotted: str) -> Any:
    node: Any = tree
    for part in dotted.split("."):
        node = node[part]
    return node


def _set(tree: dict[str, Any], dotted: str, value: Any) -> None:
    *parents, last = dotted.split(".")
    node = tree
    for part in parents:
        node = node.setdefault(part, {})
    node[last] = value
