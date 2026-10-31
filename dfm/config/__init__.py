"""Configuration loading and validation."""

from dfm.config.loader import ConfigError, ConfigOverrides, LoadedConfig, Source, load_config
from dfm.config.model import DFMConfig
from dfm.config.paths import StateDirs, discover_repo_root, state_dirs

__all__ = [
    "ConfigError",
    "ConfigOverrides",
    "DFMConfig",
    "LoadedConfig",
    "Source",
    "StateDirs",
    "discover_repo_root",
    "load_config",
    "state_dirs",
]
