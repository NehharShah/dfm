"""Record every network connection DFM (and every process it starts) opens during a test.

Two layers: a Python audit hook sees `socket.connect` / `getaddrinfo` inside this process,
and a sampler polls `lsof` for internet sockets of this process and all its descendants
(the MLX worker, `fm`, test commands). Afterwards `remote()` lists any non-loopback peer.
"""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import sys
import threading
import time
from types import TracebackType
from typing import Any

_ENDPOINT = re.compile(r"->\[?([0-9a-fA-F:.]+?)\]?:(\d+)")


def _descendants(pid: int) -> list[int]:
    out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, check=False)
    children = [int(p) for p in out.stdout.split()]
    return children + [d for c in children for d in _descendants(c)]


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return host in {"localhost", ""}


class NetworkMonitor:
    def __init__(self, interval: float = 0.2) -> None:
        self.interval = interval
        self.connections: set[tuple[str, str]] = set()  # (source, "host:port")
        self.lookups: set[str] = set()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def _hook(self, event: str, args: tuple[Any, ...]) -> None:
        if self._stop.is_set():
            return
        if event == "socket.connect" and len(args) > 1 and isinstance(args[1], tuple) and args[1]:
            self.connections.add(
                ("python", f"{args[1][0]}:{args[1][1] if len(args[1]) > 1 else ''}")
            )
        elif event == "socket.getaddrinfo" and args and isinstance(args[0], str):
            self.lookups.add(args[0])

    def _sample(self) -> None:
        me = os.getpid()
        while not self._stop.is_set():
            pids = [me, *_descendants(me)]
            out = subprocess.run(
                ["lsof", "-nP", "-a", "-i", "-p", ",".join(map(str, pids))],
                capture_output=True,
                text=True,
                check=False,
            )
            for line in out.stdout.splitlines()[1:]:
                for host, port in _ENDPOINT.findall(line):
                    self.connections.add((line.split()[0], f"{host}:{port}"))
            time.sleep(self.interval)

    def __enter__(self) -> NetworkMonitor:
        sys.addaudithook(self._hook)  # audit hooks cannot be removed; _stop makes it inert
        self._thread.start()
        return self

    def __exit__(self, *exc: type[BaseException] | BaseException | TracebackType | None) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def remote(self) -> list[tuple[str, str]]:
        """Connections to anything but loopback."""
        return sorted(c for c in self.connections if not _is_loopback(c[1].rsplit(":", 1)[0]))

    def remote_lookups(self) -> list[str]:
        return sorted(h for h in self.lookups if not _is_loopback(h) and h not in {"localhost"})
