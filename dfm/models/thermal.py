"""macOS thermal pressure, readable without privileges and from inside DFM's sandbox.

Local generation keeps the GPU at full power; on a laptop that becomes heat. DFM paces itself by
the system's own thermal pressure level (the one Activity Monitor and Low Power Mode react to):
0 nominal, 1 moderate, 2 heavy, 3 trapping, 4 sleeping. Elsewhere, or if the API is missing,
the level reads as nominal and nothing is paced.
"""

from __future__ import annotations

import ctypes
import functools
import sys
from typing import Literal

LEVELS = ("nominal", "moderate", "heavy", "trapping", "sleeping")
ThermalLimit = Literal["off", "moderate", "heavy"]
_KEY = b"com.apple.system.thermalpressurelevel"


class ThermalMonitor:
    def __init__(self) -> None:
        self._lib: ctypes.CDLL | None = None
        self._token = ctypes.c_int(-1)
        if sys.platform != "darwin":
            return
        try:
            lib = ctypes.CDLL("/usr/lib/libSystem.dylib")
            if lib.notify_register_check(_KEY, ctypes.byref(self._token)) == 0:
                self._lib = lib
        except OSError:
            self._lib = None

    def level(self) -> int:
        if self._lib is None:
            return 0
        state = ctypes.c_uint64()
        if self._lib.notify_get_state(self._token, ctypes.byref(state)) != 0:
            return 0
        return min(int(state.value), len(LEVELS) - 1)


@functools.cache
def monitor() -> ThermalMonitor:
    """One monitor per process: each registers a notify token that lives as long as it does."""
    return ThermalMonitor()


def limit_level(limit: ThermalLimit | str | None) -> int | None:
    """The pressure level at which pacing starts, or None when pacing is off."""
    return LEVELS.index(limit) if limit in ("moderate", "heavy") else None


def pause_for(level: int, limit: int | None, busy_s: float) -> float:
    """Seconds to rest after `busy_s` of generation: half duty at the limit, less above it."""
    if limit is None or level < limit:
        return 0.0
    return busy_s * (level - limit + 1)
