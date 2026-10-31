import json
import re
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dfm.cli.app import app
from dfm.tools.logs import Query, inspect, parse_fields, parse_window, tail_lines, template

START = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def jsonl(path: Path, n: int, *, error_every: int = 50) -> Path:
    with path.open("w") as fh:
        for i in range(n):
            ts = (START + timedelta(seconds=i)).isoformat().replace("+00:00", "Z")
            if i % error_every == 0:
                rec = {
                    "ts": ts,
                    "level": "error",
                    "msg": f"timeout talking to db-{i % 3} after {i}ms",
                    "req": f"{i:08x}-aaaa-bbbb-cccc-dddddddddddd",
                }
            else:
                rec = {
                    "ts": ts,
                    "level": "info",
                    "msg": f"handled request {i}",
                    "user": {"id": i % 7},
                }
            fh.write(json.dumps(rec) + "\n")
    return path


TEXT_LOG = """\
2026-10-26T12:00:00Z INFO starting worker 1
2026-10-26T12:00:01Z ERROR connection refused to 10.0.0.5:5432 (attempt 1)
Traceback (most recent call last):
  File "app/db.py", line 10, in connect
    sock.connect(addr)
ConnectionRefusedError: [Errno 61] Connection refused
2026-10-26T12:00:02Z ERROR connection refused to 10.0.0.7:5432 (attempt 2)
Traceback (most recent call last):
  File "app/db.py", line 10, in connect
    sock.connect(addr)
ConnectionRefusedError: [Errno 61] Connection refused
2026-10-26T12:01:00Z WARN slow query took 1234ms
Exception in thread "main" java.lang.IllegalStateException: boom
\tat com.acme.App.run(App.java:42)
\tat com.acme.App.main(App.java:10)
2026-10-26T12:02:00Z ERROR token=ghp_abcdefghijklmnopqrstuvwxyz0123456789 leaked in a log
goroutine 1 [running]:
main.main()
\t/src/main.go:12 +0x1d
2026-10-26T12:03:00Z INFO done
"""


def test_template_masks_variable_parts() -> None:
    a = template("timeout talking to db-1 after 120ms req=0f3e9a2b-aaaa-bbbb-cccc-dddddddddddd")
    b = template("timeout talking to db-2 after 45ms req=11111111-aaaa-bbbb-cccc-dddddddddddd")
    assert a == b == "timeout talking to db-<n> after <n> req=<uuid>"
    assert (
        template('user "alice" from 10.1.2.3:80 at 2026-10-26T12:00:00Z')
        == "user <str> from <ip> at <ts>"
    )


def test_clusters_levels_and_time_range(tmp_path: Path) -> None:
    report = inspect(jsonl(tmp_path / "app.jsonl", 1000))
    assert report.lines == 1000 and report.levels["error"] == 20 and report.levels["info"] == 980
    (cluster,) = report.clusters.values()
    assert cluster.count == 20 and cluster.template == "timeout talking to db-<n> after <n>"
    assert report.first_ts == START and report.last_ts == START + timedelta(seconds=999)


def test_filters_window_head_tail_sample(tmp_path: Path) -> None:
    path = jsonl(tmp_path / "app.jsonl", 600)
    by_field = inspect(path, Query(fields=parse_fields(["level=error"])), head=3)
    assert by_field.matched == 12 and len(by_field.selected) == 3
    nested = inspect(path, Query(fields={"user.id": "3"}))
    assert nested.matched == sum(1 for i in range(600) if i % 50 and i % 7 == 3)
    since, until = parse_window("2026-10-26T12:01:00Z..2026-10-26T12:01:59Z")
    windowed = inspect(path, Query(since=since, until=until))
    assert windowed.matched == 60
    grepped = inspect(path, Query(grep=re.compile(r"request 59\d")), keep_tail=2)
    assert [n for n, _ in grepped.tail] == [599, 600]  # request i is on line i + 1
    sampled = inspect(path, sample=25, seed=7)
    assert len(sampled.selected) == 25 and len({n for n, _ in sampled.selected}) == 25
    assert tail_lines(path, 2)[-1].startswith('{"ts": "2026-10-26T12:09:59Z"')


def test_stack_traces_are_grouped(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    path.write_text(TEXT_LOG)
    report = inspect(path)
    counts = sorted((s.count, s.template.split(" | ")[0]) for s in report.stacks.values())
    assert (2, "ConnectionRefusedError: [Errno <n>] Connection refused") in counts
    assert any("IllegalStateException" in t for _, t in counts)
    assert any(t.startswith("goroutine") or "main.go" in t for _, t in counts)
    errors = {c.template: c.count for c in report.clusters.values()}
    assert errors["<ts> ERROR connection refused to <ip> (attempt <n>)"] == 2


def test_output_is_redacted(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    path.write_text(TEXT_LOG)
    text = inspect(path, Query(grep=re.compile("token")), head=5).render()
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in text and "leaked in a log" in text


def test_cli_json(tmp_path: Path) -> None:
    path = jsonl(tmp_path / "app.jsonl", 200)
    result = CliRunner().invoke(
        app, ["inspect-log", str(path), "--json", "--field", "level=error", "--head", "2"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["matched"] == 4 and len(payload["lines_out"]) == 2
    bad = CliRunner().invoke(app, ["inspect-log", str(path), "--field", "nofield"])
    assert bad.exit_code == 2


def test_memory_stays_bounded(tmp_path: Path) -> None:
    path = jsonl(tmp_path / "big.jsonl", 200_000, error_every=10)  # ~25 MB, 20k errors
    tracemalloc.start()
    report = inspect(path)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert report.lines == 200_000 and sum(c.count for c in report.clusters.values()) == 20_000
    assert peak < 10 * 1024 * 1024, peak  # far below the file size: streamed


@pytest.mark.slow
def test_one_gigabyte_jsonl_clustered_in_bounded_memory(tmp_path: Path) -> None:
    path = tmp_path / "huge.jsonl"
    line = (
        json.dumps(
            {
                "ts": "2026-10-26T12:00:00Z",
                "level": "info",
                "msg": "handled request 123456",
                "pad": "x" * 60,
            }
        )
        + "\n"
    )
    err = (
        json.dumps(
            {
                "ts": "2026-10-26T12:00:00Z",
                "level": "error",
                "msg": "timeout after 123ms on shard 7",
            }
        )
        + "\n"
    )
    block = (line * 99 + err).encode()
    with path.open("wb") as fh:
        while fh.tell() < 1024**3:
            fh.write(block * 100)
    size = path.stat().st_size
    tracemalloc.start()
    report = inspect(path)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    path.unlink()
    assert size >= 1024**3 and report.lines > 7_000_000
    assert (
        len(report.clusters) == 1
        and next(iter(report.clusters.values())).count == report.lines // 100
    )
    assert peak < 10 * 1024 * 1024, peak
