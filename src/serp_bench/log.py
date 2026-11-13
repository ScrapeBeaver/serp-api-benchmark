"""Logging: what a run did, line by line, as text for people or JSON for machines.

Nothing secret is ever logged. Log lines are built from tasks, outcomes, timings and the
check trail only: never from request headers, URLs with keys, or response bodies.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from typing import IO, Any

LOGGER = logging.getLogger("serp_bench")


class _Text(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = dt.datetime.fromtimestamp(record.created, dt.UTC).strftime("%H:%M:%S")
        return f"{stamp} {record.levelname:<7} {record.getMessage()}"


class _Json(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = dt.datetime.fromtimestamp(record.created, dt.UTC)
        line: dict[str, Any] = {
            "ts": stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "event": getattr(record, "event", "log"),
            "message": record.getMessage(),
        }
        line.update(getattr(record, "fields", {}))
        return json.dumps(line, ensure_ascii=False, default=str)


def setup(level: str = "INFO", fmt: str = "text", stream: IO[str] | None = None) -> None:
    """Send log lines to `stream` (standard error by default). Reports go to standard output."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(_Json() if fmt == "json" else _Text())
    LOGGER.handlers[:] = [handler]
    LOGGER.setLevel(level.upper())
    LOGGER.propagate = False


def emit(level: int, event: str, message: str, **fields: Any) -> None:
    """One log line: `message` for people, `event` and `fields` for machines."""
    LOGGER.log(level, message, extra={"event": event, "fields": fields})


def info(event: str, message: str, **fields: Any) -> None:
    emit(logging.INFO, event, message, **fields)


def warning(event: str, message: str, **fields: Any) -> None:
    emit(logging.WARNING, event, message, **fields)
