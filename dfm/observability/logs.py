"""Structured, redacted, rotated JSON logs under `<data>/logs/`."""

from __future__ import annotations

import contextlib
import json
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from dfm.secrets import redact_deep, redact_text

LOG_NAME = "dfm.jsonl"
_STANDARD = set(vars(logging.makeLogRecord({})))


class _DfmJsonHandler(RotatingFileHandler):
    """DFM's own handler (so `configure` can find it again), writing user-only log files."""

    def _open(self):  # type: ignore[override]  # the stdlib signature is untyped
        stream = super()._open()
        with contextlib.suppress(OSError):
            Path(self.baseFilename).chmod(0o600)  # may hold redacted repository text
        return stream


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extra = {
            k: v for k, v in vars(record).items() if k not in _STANDARD and not k.startswith("_")
        }
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": redact_text(record.getMessage()),
            **redact_deep(extra),
        }
        if record.exc_info:
            payload["error"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure(
    logs_dir: Path, *, level: int = logging.INFO, max_bytes: int = 5 * 1024 * 1024, backups: int = 3
) -> Path:
    """Attach the rotating JSON handler to the `dfm` logger (idempotent)."""
    logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = logs_dir / LOG_NAME
    logger = logging.getLogger("dfm")
    for handler in list(logger.handlers):
        if isinstance(handler, _DfmJsonHandler):
            if Path(handler.baseFilename) == path:
                return path
            logger.removeHandler(handler)
            handler.close()
    handler = _DfmJsonHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return path
