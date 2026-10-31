"""Filesystem tools: read, search, patch, write."""

from dfm.tools.filesystem.patching import (
    PatchConflict,
    SearchReplace,
    UnifiedDiff,
    apply_hunks,
    apply_search_replace,
    parse_unified,
)
from dfm.tools.filesystem.tools import (
    FileSlice,
    FsTools,
    PatchResult,
    TextMatch,
    ToolError,
    describe,
    search_replace,
)

__all__ = [
    "FileSlice",
    "FsTools",
    "PatchConflict",
    "PatchResult",
    "SearchReplace",
    "TextMatch",
    "ToolError",
    "UnifiedDiff",
    "apply_hunks",
    "apply_search_replace",
    "describe",
    "parse_unified",
    "search_replace",
]
