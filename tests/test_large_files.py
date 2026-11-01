import gzip
import json
import sqlite3
import struct
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

from dfm.config import DFMConfig
from dfm.context.engine import ContextEngine
from dfm.execution import ExecutionEnvironment, ResourceLimits
from dfm.policy import Policy
from dfm.sessions import SessionStore
from dfm.tools.filesystem import FsTools
from dfm.tools.filesystem.kinds import FileKind, classify_file
from tests.helpers import make_env as shared_make_env

GB = 1024**3


def make_env(
    repo: Path, tmp_path: Path, limits: ResourceLimits | None = None
) -> ExecutionEnvironment:
    return shared_make_env(repo, tmp_path, session_id="lf", limits=limits)


def sparse_jsonl(path: Path, size: int) -> Path:
    with path.open("wb") as fh:
        for i in range(500):  # > 8 KB of real lines before the hole (holes read as NUL bytes)
            fh.write(json.dumps({"i": i, "msg": f"event {i}"}).encode() + b"\n")
        fh.truncate(size)  # sparse: no real 2 GB written to disk
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def test_classification_by_magic_and_extension(repo: Path) -> None:
    db = repo / "app.db"
    with sqlite3.connect(db) as conn:
        conn.execute("create table users (id integer primary key, name text)")
        conn.executemany("insert into users (name) values (?)", [("a",), ("b",), ("c",)])
    with zipfile.ZipFile(repo / "bundle.zip", "w") as zf:
        zf.writestr("x.txt", "hi")
    (repo / "logs.gz").write_bytes(gzip.compress(b"x" * 100))
    (repo / "data.parquet").write_bytes(b"PAR1" + b"\x00" * 64 + b"PAR1")
    (repo / "doc.pdf").write_bytes(b"%PDF-1.7\n...")
    (repo / "model.gguf").write_bytes(b"GGUF" + b"\x00" * 32)
    header = json.dumps({"w": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode()
    (repo / "w.safetensors").write_bytes(struct.pack("<Q", len(header)) + header + b"\x00" * 4)
    (repo / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    (repo / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16)
    (repo / "msg.pb").write_bytes(b"\x08\x96\x01\x12\x04\x00abc")
    (repo / "small.txt").write_text("hello\n")
    (repo / "big.txt").write_text("line\n" * 200_000)
    (repo / "server.log").write_text("INFO ok\n" * 100_000)
    expected = {
        "app.db": FileKind.SQLITE,
        "bundle.zip": FileKind.ARCHIVE,
        "logs.gz": FileKind.ARCHIVE,
        "data.parquet": FileKind.PARQUET,
        "doc.pdf": FileKind.PDF,
        "model.gguf": FileKind.MODEL_WEIGHTS,
        "w.safetensors": FileKind.MODEL_WEIGHTS,
        "pic.png": FileKind.MEDIA,
        "clip.mp4": FileKind.MEDIA,
        "msg.pb": FileKind.PROTOBUF,
        "small.txt": FileKind.TEXT,
        "big.txt": FileKind.LARGE_TEXT,
        "server.log": FileKind.LOG,
    }
    for name, kind in expected.items():
        assert classify_file(repo / name).kind is kind, name


async def test_two_gigabyte_jsonl_is_a_stub(repo: Path, tmp_path: Path) -> None:
    sparse_jsonl(repo / "events.jsonl", 2 * GB)
    store = SessionStore(tmp_path / "sessions")
    session = store.create(repo, "read the events")

    def trace(event_type: str, **payload: Any) -> None:
        store.append(session, event_type, **payload)

    fs = FsTools(make_env(repo, tmp_path), Policy(DFMConfig()), on_event=trace)
    started = time.monotonic()
    view = await fs.read_file("events.jsonl")  # the session trace accepts the stub's event
    assert view.stub and view.text.startswith(
        "[events.jsonl: JSONL, 2.0 GB; not read into context]"
    )
    events = [e for e in store.load(session.id).events if e["type"] == "file_read"]
    assert events[-1]["stub"] is True and events[-1]["path"] == "events.jsonl"
    assert "dfm inspect-log events.jsonl" in view.text and '"msg"' not in view.text
    head = await fs.read_lines("events.jsonl", 1, 3)  # an explicit range is streamed
    assert head.text.splitlines()[2] == '{"i": 2, "msg": "event 2"}' and (head.start, head.end) == (
        1,
        3,
    )
    assert time.monotonic() - started < 2  # nothing loaded the 2 GB


async def test_ranges_are_capped_and_limits_configurable(repo: Path, tmp_path: Path) -> None:
    (repo / "app.log").write_text("".join(f"line {n}\n" for n in range(1, 50_001)))
    fs = FsTools(
        make_env(repo, tmp_path, ResourceLimits(read_text_bytes=1024, read_range_lines=100)),
        Policy(DFMConfig()),
    )
    view = await fs.read_lines("app.log", 10, 100_000)
    assert (view.start, view.end) == (10, 109) and view.text.splitlines()[0] == "line 10"
    (repo / "notes.md").write_text("x" * 5000)  # small by default, large under a 1 KB limit
    assert (await fs.read_file("notes.md")).stub


async def test_sqlite_summary_and_archive_stub(repo: Path, tmp_path: Path) -> None:
    with sqlite3.connect(repo / "app.db") as conn:
        conn.execute("create table orders (id integer primary key, total real)")
        conn.executemany("insert into orders (total) values (?)", [(1.0,), (2.5,)])
    with zipfile.ZipFile(repo / "dump.zip", "w") as zf:
        zf.writestr("secret.txt", "do not extract")
    fs = FsTools(make_env(repo, tmp_path), Policy(DFMConfig()))
    db = await fs.read_file("app.db")
    assert db.stub and "table orders: 2 rows" in db.text
    archive = await fs.read_file("dump.zip")
    assert archive.stub and "does not extract" in archive.text
    assert not (repo / "secret.txt").exists()


def test_context_engine_routes_big_files(repo: Path, tmp_path: Path) -> None:
    sparse_jsonl(repo / "events.jsonl", 2 * GB)
    (repo / "server.log").write_text("".join(f"line {n}\n" for n in range(1, 100_001)))
    with sqlite3.connect(repo / "app.db") as conn:
        conn.execute("create table t (x)")
    engine = ContextEngine(make_env(repo, tmp_path), None)
    item, why = engine.read_item("events.jsonl", reason="mentioned", priority=1)
    assert item is None and why is not None and "JSONL" in why
    excerpt, _ = engine.read_item(
        "server.log", reason="failure", priority=1, lines=(50_000, 50_002)
    )
    assert (
        excerpt is not None
        and "line 50000" in excerpt.content
        and "line 1\n" not in excerpt.content
    )
    summary, _ = engine.read_item("app.db", reason="mentioned", priority=1)
    assert summary is not None and "table t: 0 rows" in summary.content
