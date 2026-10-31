"""What a file is before anything reads it into a model.

Huge JSONL, large logs, SQLite, Parquet, archives, protobuf blobs, model weights,
media and PDFs never go into a prompt: readers get a short stub that says what the
file is and how to inspect it (`dfm inspect-log`, a SQLite/Parquet summary). Large
text can still be read by explicit line range, streamed, never loaded whole.
Archives are described, never extracted.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import itertools
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class FileKind(StrEnum):
    TEXT = "text"
    LARGE_TEXT = "large text"
    JSONL = "JSONL"
    LOG = "log"
    SQLITE = "SQLite database"
    PARQUET = "Parquet file"
    ARCHIVE = "archive"
    PROTOBUF = "protobuf blob"
    MODEL_WEIGHTS = "model weights"
    MEDIA = "media"
    PDF = "PDF"
    BINARY = "binary"


STREAMABLE = {FileKind.LARGE_TEXT, FileKind.JSONL, FileKind.LOG}

_MAGIC: tuple[tuple[bytes, int, FileKind], ...] = (
    (b"SQLite format 3\x00", 0, FileKind.SQLITE),
    (b"PAR1", 0, FileKind.PARQUET),
    (b"%PDF", 0, FileKind.PDF),
    (b"PK\x03\x04", 0, FileKind.ARCHIVE),
    (b"\x1f\x8b", 0, FileKind.ARCHIVE),  # gzip
    (b"BZh", 0, FileKind.ARCHIVE),
    (b"\xfd7zXZ\x00", 0, FileKind.ARCHIVE),
    (b"\x28\xb5\x2f\xfd", 0, FileKind.ARCHIVE),  # zstd
    (b"7z\xbc\xaf\x27\x1c", 0, FileKind.ARCHIVE),
    (b"ustar", 257, FileKind.ARCHIVE),
    (b"GGUF", 0, FileKind.MODEL_WEIGHTS),
    (b"\x93NUMPY", 0, FileKind.MODEL_WEIGHTS),
    (b"\x89PNG", 0, FileKind.MEDIA),
    (b"\xff\xd8\xff", 0, FileKind.MEDIA),
    (b"GIF8", 0, FileKind.MEDIA),
    (b"ftyp", 4, FileKind.MEDIA),  # mp4 / mov / heic
    (b"RIFF", 0, FileKind.MEDIA),  # wav / webp / avi
    (b"ID3", 0, FileKind.MEDIA),
    (b"OggS", 0, FileKind.MEDIA),
)
_EXTENSIONS = {
    FileKind.MODEL_WEIGHTS: {
        ".safetensors",
        ".gguf",
        ".pt",
        ".pth",
        ".ckpt",
        ".onnx",
        ".npy",
        ".npz",
        ".h5",
        ".mlmodel",
        ".tflite",
    },
    FileKind.PROTOBUF: {".pb", ".binpb", ".protobin"},
    FileKind.MEDIA: {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".heic",
        ".mp3",
        ".wav",
        ".flac",
        ".mp4",
        ".mov",
        ".mkv",
        ".avi",
        ".ogg",
    },
    FileKind.ARCHIVE: {
        ".zip",
        ".tar",
        ".gz",
        ".tgz",
        ".bz2",
        ".xz",
        ".zst",
        ".7z",
        ".rar",
        ".jar",
        ".whl",
    },
    FileKind.PDF: {".pdf"},
    FileKind.SQLITE: {".sqlite", ".sqlite3", ".db"},
    FileKind.PARQUET: {".parquet"},
}


@dataclass(frozen=True)
class FileInfo:
    kind: FileKind
    size: int

    @property
    def readable(self) -> bool:
        return self.kind is FileKind.TEXT


def _human(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} bytes"


def classify_file(path: Path, *, text_limit: int = 256 * 1024) -> FileInfo:
    size = path.stat().st_size
    with path.open("rb") as fh:
        head = fh.read(8192)
    for magic, offset, kind in _MAGIC:
        if head[offset : offset + len(magic)] == magic:
            return FileInfo(kind, size)
    suffix = path.suffix.lower()
    for kind, suffixes in _EXTENSIONS.items():
        if suffix in suffixes and (kind is not FileKind.SQLITE or b"\x00" in head):
            return FileInfo(kind, size)
    if b"\x00" in head:
        if suffix == ".safetensors" or head[8:9] == b"{":  # safetensors: u64 header length + JSON
            return FileInfo(FileKind.MODEL_WEIGHTS, size)
        return FileInfo(FileKind.BINARY, size)
    if size <= text_limit:
        return FileInfo(FileKind.TEXT, size)
    if suffix in {".jsonl", ".ndjson"} or (head.lstrip()[:1] == b"{" and b"}\n{" in head):
        return FileInfo(FileKind.JSONL, size)
    if suffix in {".log", ".out", ".err"} or "log" in path.name.lower():
        return FileInfo(FileKind.LOG, size)
    return FileInfo(FileKind.LARGE_TEXT, size)


def _sqlite_summary(path: Path) -> list[str]:
    lines: list[str] = []
    uri = f"file:{path}?mode=ro&immutable=1"
    try:
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
            tables = db.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            for name, sql in tables[:30]:
                quoted = (
                    '"' + str(name).replace('"', '""') + '"'
                )  # an identifier from the db itself
                query = f"SELECT count(*) FROM {quoted}"  # noqa: S608
                rows = db.execute(query).fetchone()[0]
                lines.append(f"- table {name}: {rows} rows; {' '.join(str(sql).split())[:200]}")
    except sqlite3.Error as exc:
        lines.append(f"(could not open read-only: {exc})")
    return lines


def _parquet_summary(path: Path) -> list[str]:
    if importlib.util.find_spec("pyarrow") is None:
        return ["(install pyarrow for a schema and row-count summary)"]
    pq: Any = importlib.import_module("pyarrow.parquet")
    meta: Any = pq.ParquetFile(str(path)).metadata
    return [
        f"- {meta.num_rows} rows, {meta.num_columns} columns",
        f"- schema: {str(meta.schema)[:500]}",
    ]


def stub(rel: str, info: FileInfo, path: Path) -> str:
    """What a model sees instead of the file's content."""
    head = f"[{rel}: {info.kind.value}, {_human(info.size)}; not read into context]"
    if info.kind in {FileKind.JSONL, FileKind.LOG}:
        hint = f"Inspect it with `dfm inspect-log {rel}`, or read a line range (start/end)."
        return f"{head}\n{hint}"
    if info.kind is FileKind.LARGE_TEXT:
        return f"{head}\nRead a line range (start/end) or search it for what you need."
    if info.kind is FileKind.SQLITE:
        return "\n".join([head, "Summary (read-only):", *_sqlite_summary(path)])
    if info.kind is FileKind.PARQUET:
        return "\n".join([head, "Summary:", *_parquet_summary(path)])
    if info.kind is FileKind.ARCHIVE:
        return f"{head}\nList it with `tar -tf` / `unzip -l`; DFM does not extract archives into the repository."
    return head


def read_range(path: Path, start: int, end: int, max_lines: int) -> tuple[str, int, int]:
    """Lines `start..end` (1-based, inclusive) streamed from a large text file; (text, start, end)."""
    start = max(1, start)
    end = min(end, start + max_lines - 1)
    with path.open("rb") as fh:
        chunk = list(itertools.islice(fh, start - 1, end))
    text = b"".join(chunk).decode(errors="replace").rstrip("\n")
    return text, start, start + len(chunk) - 1
