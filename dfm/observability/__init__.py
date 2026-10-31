"""Structured logs and metrics. Nothing here talks to the network."""

from dfm.observability.logs import configure as configure_logging
from dfm.observability.metrics import load, metric, start, summarize, write

__all__ = ["configure_logging", "load", "metric", "start", "summarize", "write"]
