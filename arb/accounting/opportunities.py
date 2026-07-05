"""JSONL research log: opportunity records + spread samples.

One file per UTC day (``opportunities-YYYYMMDD.jsonl``), rotated on write.
Writes are synchronous on the event loop: records are <1KB, arrive at low
rate (opportunity opens/closes + one sample sweep every ~30s), and land in
the OS page cache — microseconds, not a latency hazard at Phase 1 rates.
If Phase 2 telemetry rates grow, this becomes a queue + writer task.

Failure modes:
- A write failure (disk full, permissions) is logged with context and counted
  in ``arb_record_write_errors_total``; the process keeps running — losing a
  research record must not take down market-data collection. The error
  counter makes silent data loss visible on the dashboard/alerts.
- Records are flushed per write, so a crash loses at most the in-flight line.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

import orjson

from arb.monitoring.metrics import Metrics

logger = logging.getLogger("arb.accounting.opportunities")


class OpportunityLog:
    def __init__(self, data_dir: Path, metrics: Metrics) -> None:
        self.data_dir = data_dir
        self.metrics = metrics
        self._fh: IO[bytes] | None = None
        self._day: str | None = None
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _handle_for_today(self) -> IO[bytes]:
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        if self._fh is None or day != self._day:
            if self._fh is not None:
                self._fh.close()
            path = self.data_dir / f"opportunities-{day}.jsonl"
            self._fh = path.open("ab")
            self._day = day
            logger.info("research log -> %s", path)
        return self._fh

    def write(self, record: dict) -> None:
        try:
            fh = self._handle_for_today()
            fh.write(orjson.dumps(record))
            fh.write(b"\n")
            fh.flush()
            self.metrics.records_written.inc()
        except Exception:
            logger.exception("failed to write research record", extra={"record_type": record.get("type")})
            self.metrics.record_write_errors.inc()

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                logger.exception("failed to close research log")
            self._fh = None
