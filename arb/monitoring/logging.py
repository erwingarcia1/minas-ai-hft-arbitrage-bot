"""Structured JSON logging.

Every record becomes one JSON object per line with timestamp, level, logger,
message, and any ``extra={...}`` fields the call site attached (venue, symbol,
reason, ...). Exceptions serialize their traceback into ``exc_info``.

Failure modes:
- A log field that orjson cannot serialize is stringified rather than raised —
  logging must never take the process down.
- When the terminal dashboard is active, logs route to a file (stdout belongs
  to the dashboard); otherwise they go to stderr.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import orjson

_STD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
    | {"message", "asctime", "taskName"}
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc_info"] = self.formatException(record.exc_info)
        try:
            return orjson.dumps(payload).decode()
        except TypeError:
            return orjson.dumps(
                {k: str(v) for k, v in payload.items()}, option=orjson.OPT_NON_STR_KEYS
            ).decode()


def setup_logging(level: str, log_dir: Path | None) -> None:
    """Configure root logging. ``log_dir`` set -> file handler (dashboard owns
    the terminal); None -> stderr."""
    handler: logging.Handler
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        handler = logging.FileHandler(log_dir / f"arb-{day}.log", encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # Third-party debug noise stays out of the research-grade log stream.
    logging.getLogger("websockets").setLevel(logging.INFO)
    logging.getLogger("aiohttp").setLevel(logging.INFO)
