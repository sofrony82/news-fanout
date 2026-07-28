"""Structured logging setup.

Replaces the `logging_helpers.configure_logging` helper that this service used
when it lived inside the parent uv workspace (`packages/logging-helpers`). That
package is not published, so the service carries its own stdlib-only version.
"""

import json
import logging
import logging.config
import os
import sys
from datetime import datetime, timezone
from typing import Any

_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    """One JSON object per line — what Cloud Logging picks up from stdout."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, default=str)


def configure_logging(level: int | str | None = None, json_output: bool | None = None) -> None:
    """Install a single stdout handler on the root logger.

    Level comes from the `level` argument, else `LOG_LEVEL`, else INFO.
    Format is JSON unless `LOG_FORMAT=text` (or `json_output=False`) is set.
    """
    resolved_level = level if level is not None else os.getenv("LOG_LEVEL", "INFO")
    if json_output is None:
        json_output = os.getenv("LOG_FORMAT", "json").lower() != "text"

    formatter: logging.Formatter
    if json_output:
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(resolved_level)

    # uvicorn installs its own handlers; drain them onto ours so output stays uniform.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    logging.getLogger("sqlalchemy.engine.Engine").propagate = True
