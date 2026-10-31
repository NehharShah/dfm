"""Per-language toolchain adapters."""

from pathlib import Path
from typing import Any

from dfm.tools.languages.base import (
    LanguageAdapter,
    LspServerSpec,
    ToolchainCommands,
    ToolCommand,
)
from dfm.tools.languages.cairo import CairoAdapter
from dfm.tools.languages.go import GoAdapter
from dfm.tools.languages.python import PythonAdapter
from dfm.tools.languages.rust import RustAdapter
from dfm.tools.languages.solidity import SolidityAdapter
from dfm.tools.languages.swift import SwiftAdapter
from dfm.tools.languages.typescript import TypeScriptAdapter

ADAPTERS: list[LanguageAdapter] = [
    PythonAdapter(),
    RustAdapter(),
    GoAdapter(),
    SwiftAdapter(),
    SolidityAdapter(),
    CairoAdapter(),  # both before TypeScript: Hardhat/Starknet repos also have package.json
    TypeScriptAdapter(),
]


def select_adapter(repo: Path, canon: Any) -> LanguageAdapter | None:
    """A root manifest (Cargo.toml, go.mod, ...) beats a language RepoCanon merely saw files of."""
    for adapter in ADAPTERS:
        if any((repo / m).exists() for m in adapter.manifests):
            return adapter
    return next((a for a in ADAPTERS if a.detect(repo, canon)), None)


__all__ = [
    "ADAPTERS",
    "CairoAdapter",
    "GoAdapter",
    "LanguageAdapter",
    "LspServerSpec",
    "PythonAdapter",
    "RustAdapter",
    "SolidityAdapter",
    "SwiftAdapter",
    "ToolCommand",
    "ToolchainCommands",
    "TypeScriptAdapter",
    "select_adapter",
]
