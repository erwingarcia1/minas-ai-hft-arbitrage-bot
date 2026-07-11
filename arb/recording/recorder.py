"""Normalized book-event journal: the input side of the replay engine.

One JSONL file per observer session (``books-<UTC>.jsonl``). Line 1 is a
``recording_header`` carrying the schema version, session start stamps, and
the detector-relevant configuration (symbols, venues, fees, thresholds, and
the instrument metadata that rounds quantities) so a replay can verify
comparability. Then one ``book_event`` line per applied book mutation, sync
transition, or live evaluation point, carrying the *literal* price/size
strings the exchange sent (Decimal-exact round trip) and all three
timestamps (exchange ms, local wall ns, local monotonic ns). ``close()``
appends a ``recording_end`` stamp so replay can flush open opportunities at
the session-end clock.

Evaluation points are journaled explicitly (kind ``eval``) because they are
NOT inferable from mutations: the feeds deliberately do not evaluate state
that failed an integrity check (checksum mismatch, crossed book), so a
replay that evaluated after every mutation would price books live rejected.
The recorder journals ``eval`` exactly where the feeds invoke their
book-update callback.

The recorder attaches to every book via ``OrderBook.tap`` — the single seam
all mutations flow through — so recording completeness is structural, not
per-feed. File order equals application order (single event loop, single
writer), which is the replay order. ``detach()`` exists for process
teardown: feed-cancellation unsyncs are live-shutdown mechanics, not market
data, and journaling them would make replay close still-open opportunities
as ``book_unusable`` instead of reproducing the live ``shutdown`` flush.

Failure modes:
- Writes are buffered (``_FLUSH_EVERY`` events or ``_FLUSH_INTERVAL_NS``,
  whichever first); a crash loses at most one buffer. Research-grade loss
  tolerance, counted, never silent.
- A write failure is logged and counted in ``arb_recording_errors_total``;
  after ``_MAX_CONSECUTIVE_ERRORS`` consecutive failures the recorder
  disables itself (loudly) instead of spamming a dead disk — market-data
  collection continues either way.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

import orjson

from arb.feeds.book import BookEventKind, BookRegistry, OrderBook, RawLevel
from arb.instruments.registry import InstrumentRegistry
from arb.models import Settings, VenueName
from arb.monitoring.metrics import Metrics

logger = logging.getLogger("arb.recording.recorder")

SCHEMA_VERSION = 1

_FLUSH_EVERY = 500  # events
_FLUSH_INTERVAL_NS = 1_000_000_000  # 1s
_MAX_CONSECUTIVE_ERRORS = 10


def _session_stamp(wall_ns: int) -> str:
    dt = datetime.fromtimestamp(wall_ns / 1e9, tz=timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%S.%f") + "Z"


def instruments_snapshot(
    settings: Settings, instruments: InstrumentRegistry | None
) -> dict[str, dict]:
    """The instrument fields the detector's pricing actually consumes, as
    Decimal-exact strings, keyed 'venue:symbol'. Replay compares this against
    its own registry: refreshed metadata changes the money math and must be
    as visible as a changed fee or threshold."""
    if instruments is None:
        return {}
    snapshot: dict[str, dict] = {}
    for venue in settings.enabled_venues:
        for symbol in settings.symbols:
            meta = instruments.get(venue, symbol)
            if meta is None:
                continue
            snapshot[f"{venue.value}:{symbol}"] = {
                "status": meta.status,
                "qty_step": str(meta.qty_step),
                "min_qty": str(meta.min_qty),
                "min_notional": str(meta.min_notional),
                "max_qty": str(meta.max_qty) if meta.max_qty is not None else None,
            }
    return snapshot


def header_record(
    settings: Settings,
    wall_ns: int,
    mono_ns: int,
    instruments: InstrumentRegistry | None = None,
) -> dict:
    """Detector-relevant config snapshot; replay compares it with the active
    config and warns on divergence (divergence is legitimate — threshold
    sensitivity analysis is a stated replay use — but must be visible)."""
    return {
        "type": "recording_header",
        "schema_version": SCHEMA_VERSION,
        "started_at": datetime.fromtimestamp(wall_ns / 1e9, tz=timezone.utc).isoformat(
            timespec="milliseconds"
        ),
        "wall_ns": wall_ns,
        "mono_ns": mono_ns,
        "symbols": list(settings.symbols),
        "venues": [v.value for v in settings.enabled_venues],
        "signals": {
            "min_net_edge_bps": settings.signals.min_net_edge_bps,
            "clip_sizes_usdt": settings.signals.clip_sizes_usdt,
        },
        "staleness_ms": settings.risk.staleness_ms,
        "taker_bps": {v.value: settings.taker_bps(v) for v in settings.enabled_venues},
        "spread_sample_interval_s": settings.monitoring.spread_sample_interval_s,
        "instruments": instruments_snapshot(settings, instruments),
    }


class BookEventRecorder:
    def __init__(
        self,
        directory: Path,
        settings: Settings,
        metrics: Metrics,
        *,
        wall_ns: int,
        mono_ns: int,
        instruments: InstrumentRegistry | None = None,
    ) -> None:
        self.metrics = metrics
        self.enabled = True
        self._buffer: list[bytes] = []
        self._last_flush_mono_ns = mono_ns
        self._consecutive_errors = 0
        self._registry: BookRegistry | None = None
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"books-{_session_stamp(wall_ns)}.jsonl"
        self._fh: IO[bytes] | None = self.path.open("ab")
        self._write(header_record(settings, wall_ns, mono_ns, instruments), mono_ns)
        logger.info("recording book events -> %s", self.path)

    def attach(self, registry: BookRegistry) -> None:
        self._registry = registry
        for book in registry.all_books():
            book.tap = self.on_book_event

    def detach(self, registry: BookRegistry) -> None:
        """Stop journaling BEFORE feed teardown: cancellation-driven unsync
        events are shutdown mechanics, not market data (see module docs)."""
        for book in registry.all_books():
            book.tap = None

    def on_evaluation(self, venue: VenueName, symbol: str) -> None:
        """Journal a live evaluation point (the feeds' book-update callback).
        Stamped from the book's last data-frame stamps — the evaluation
        happens synchronously after the mutation that carries those stamps,
        preserving the one-time-source-per-journal rule."""
        if not self.enabled or self._registry is None:
            return
        book = self._registry.get(venue, symbol)
        record = {
            "type": "book_event",
            "v": venue.value,
            "s": symbol,
            "k": "eval",
            "ets": book.last_exchange_ts_ms,
            "mono_ns": book.last_local_mono_ns,
            "wall_ns": book.last_local_wall_ns,
            "b": [],
            "a": [],
        }
        self._write(record, book.last_local_mono_ns)
        self.metrics.recording_events.labels(venue=venue.value).inc()

    # -- tap callback --------------------------------------------------------

    def on_book_event(
        self,
        book: OrderBook,
        kind: BookEventKind,
        bids: list[RawLevel],
        asks: list[RawLevel],
        exchange_ts_ms: int | None,
        mono_ns: int,
        wall_ns: int,
    ) -> None:
        if not self.enabled:
            return
        record = {
            "type": "book_event",
            "v": book.venue.value,
            "s": book.symbol,
            "k": kind,
            "ets": exchange_ts_ms,
            "mono_ns": mono_ns,
            "wall_ns": wall_ns,
            "b": bids,
            "a": asks,
        }
        self._write(record, mono_ns)
        self.metrics.recording_events.labels(venue=book.venue.value).inc()

    # -- writer ----------------------------------------------------------------

    def _write(self, record: dict, mono_ns: int) -> None:
        try:
            self._buffer.append(orjson.dumps(record))
            if (
                len(self._buffer) >= _FLUSH_EVERY
                or mono_ns - self._last_flush_mono_ns >= _FLUSH_INTERVAL_NS
            ):
                self._flush(mono_ns)
            self._consecutive_errors = 0
        except Exception:
            self._consecutive_errors += 1
            self.metrics.recording_errors.inc()
            logger.exception("failed to journal book event (%s)", self.path)
            if self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                self.enabled = False
                logger.error(
                    "recording DISABLED after %d consecutive write failures; "
                    "market-data collection continues, replay data ends here",
                    self._consecutive_errors,
                )

    def _flush(self, mono_ns: int) -> None:
        if not self._buffer:
            self._last_flush_mono_ns = mono_ns
            return
        if self._fh is None:
            self._buffer.clear()
            return
        self._fh.write(b"\n".join(self._buffer) + b"\n")
        self._fh.flush()
        self._buffer.clear()
        self._last_flush_mono_ns = mono_ns

    def close(self, *, wall_ns: int, mono_ns: int) -> None:
        """Append the session-end stamp and close. Replay uses the stamp to
        close still-open opportunities at the exact same clock as the live
        session's shutdown flush."""
        if self._fh is None:
            return
        try:
            if self.enabled:
                self._buffer.append(
                    orjson.dumps(
                        {"type": "recording_end", "mono_ns": mono_ns, "wall_ns": wall_ns}
                    )
                )
            self._flush(mono_ns)
            self._fh.close()
        except Exception:
            self.metrics.recording_errors.inc()
            logger.exception("failed to close recording %s", self.path)
        finally:
            self._fh = None
