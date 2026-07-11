"""Replay engine: re-run detection over a recorded book-event journal.

Reads ``books-*.jsonl`` files (arb/recording format), re-applies every event
to fresh books, and re-runs the SAME components used live — SpreadDetector
AND StalenessMonitor — under a ReplayClock advanced only by recorded stamps.

Evaluation fidelity: the detector is evaluated ONLY where live evaluated it —
1. at journaled ``eval`` events (the feeds' book-update callback, journaled
   explicitly because feeds deliberately do not evaluate state that failed an
   integrity check; inferring evaluation from mutations would price books
   live rejected as corrupt);
2. at staleness-monitor ticks, reconstructed on the same 100ms grid the live
   monitor runs, seeded from the recording header (the actual
   ``StalenessMonitor.tick`` is reused, so venue-level ``venue_unusable``
   closes and per-book stale closes carry the same reasons as live);
3. at sample-sweep boundaries at the recorded cadence, on the same grid the
   live sampler is anchored to.

Determinism contract (CLAUDE.md §35): the same input files and the same
configuration produce byte-identical records. Replaying with a DIFFERENT
configuration (thresholds, fees, clips, instruments) is the supported
sensitivity-analysis path; the engine warns about every divergence from the
recording header so nobody mistakes a what-if run for a reproduction.

Fidelity notes (documented, deliberate — see docs/MARKET_REPLAY.md §5):
- Live monitor ticks and sample sweeps fire LATE by scheduling delay and
  stamp records with the really-observed clock; replay fires them at the
  exact grid stamps. The skew is bounded per occurrence and does not
  accumulate (both schedules are anchored absolute grids).
- Control-flow sync transitions carry last-data-frame stamps, so the replay
  clock (and therefore the monitor grid) only advances past them when a
  later frame or the end stamp arrives: a freshness transition live
  observed inside a gap with NO interleaving frames can shift to the next
  frame's tick, bounded by the inter-frame gap. Real feeds tick constantly;
  the sparse-stream corner exists mainly in synthetic tests.
- A live process stalled across sweep boundaries skips them; replay fires
  every boundary events span.
- Live shutdown flushes open opportunities microseconds before the
  ``recording_end`` stamp replay flushes them at.

Failure modes:
- Events for venues/symbols outside the active config are skipped and
  counted (replaying a wide recording with a narrow config is legitimate).
- A malformed line raises ``ReplayError`` naming the input file and line; a
  failing OUTPUT sink raises ``ReplayError`` naming the sink — an intact
  journal must never be blamed for an output-disk failure.
- Recordings that end without a ``recording_end`` stamp (crash) still flush
  open opportunities at the last event's clock, reason ``shutdown``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import orjson

from arb.feeds.book import BookRegistry
from arb.instruments.registry import InstrumentRegistry
from arb.models import Settings, VenueName
from arb.monitoring.metrics import Metrics
from arb.recording.recorder import instruments_snapshot
from arb.replay.clock import ReplayClock
from arb.risk.staleness import TICK_S, StalenessMonitor
from arb.signals.spread import RecordSink, SpreadDetector

logger = logging.getLogger("arb.replay.engine")

_TICK_NS = int(TICK_S * 1e9)


class ReplayError(RuntimeError):
    """Raised when a journal file is unreadable/corrupt or the output sink
    fails; the message states which."""


@dataclass
class ReplayStats:
    files: int = 0
    headers: int = 0
    events_applied: int = 0
    events_skipped: int = 0
    evals: int = 0
    monitor_ticks: int = 0
    samples_swept: int = 0
    records_written: int = 0
    opportunities: int = 0
    warnings: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        logger.warning("%s", message)


class _CountingSink:
    def __init__(self, inner: RecordSink, stats: ReplayStats) -> None:
        self._inner = inner
        self._stats = stats

    def write(self, record: dict) -> None:
        try:
            self._inner.write(record)
        except Exception as exc:
            # Output-side failure: never let this be misattributed to the
            # input journal by the per-line handler in run_replay.
            raise ReplayError(f"output sink failed: {exc!r}") from exc
        self._stats.records_written += 1
        if record.get("type") == "opportunity":
            self._stats.opportunities += 1


def _compare_header(
    header: dict,
    settings: Settings,
    instruments: InstrumentRegistry | None,
    stats: ReplayStats,
) -> None:
    """Divergence between the recording's config and the active config is a
    supported use (threshold/fee/instrument sensitivity), but must be
    visible — every detector-relevant input is compared."""
    if set(header.get("symbols", [])) != set(settings.symbols):
        stats.warn(
            f"replaying with symbols {sorted(settings.symbols)} "
            f"(recorded {sorted(header.get('symbols', []))}) — events outside the "
            f"active config are skipped"
        )
    active_venues = sorted(v.value for v in settings.enabled_venues)
    if set(header.get("venues", [])) != set(active_venues):
        stats.warn(
            f"replaying with venues {active_venues} (recorded {sorted(header.get('venues', []))})"
        )
    sig = header.get("signals", {})
    if sig.get("min_net_edge_bps") != settings.signals.min_net_edge_bps:
        stats.warn(
            f"replaying with min_net_edge_bps={settings.signals.min_net_edge_bps} "
            f"(recorded {sig.get('min_net_edge_bps')}) — sensitivity run, not a reproduction"
        )
    if sig.get("clip_sizes_usdt") != settings.signals.clip_sizes_usdt:
        stats.warn(
            f"replaying with clips {settings.signals.clip_sizes_usdt} "
            f"(recorded {sig.get('clip_sizes_usdt')})"
        )
    if header.get("staleness_ms") != settings.risk.staleness_ms:
        stats.warn(
            f"replaying with staleness_ms={settings.risk.staleness_ms} "
            f"(recorded {header.get('staleness_ms')})"
        )
    recorded_fees = header.get("taker_bps", {})
    active_fees = {v.value: settings.taker_bps(v) for v in settings.enabled_venues}
    if recorded_fees != active_fees:
        stats.warn(f"replaying with taker fees {active_fees} (recorded {recorded_fees})")
    interval = header.get("spread_sample_interval_s")
    if interval != settings.monitoring.spread_sample_interval_s:
        stats.warn(
            f"replaying with spread_sample_interval_s="
            f"{settings.monitoring.spread_sample_interval_s} (recorded {interval})"
        )
    recorded_instruments = header.get("instruments", {})
    active_instruments = instruments_snapshot(settings, instruments)
    if recorded_instruments != active_instruments:
        stats.warn(
            "replaying with DIFFERENT instrument metadata than recorded "
            "(tick/step/minimum rules change the money math) — sensitivity "
            f"run, not a reproduction; recorded keys: {sorted(recorded_instruments)}, "
            f"active keys: {sorted(active_instruments)}"
        )


class _Replayer:
    def __init__(
        self,
        settings: Settings,
        sink: RecordSink,
        instruments: InstrumentRegistry | None,
        stats: ReplayStats,
    ) -> None:
        self.settings = settings
        self.instruments = instruments
        self.stats = stats
        self.clock = ReplayClock()
        self.registry = BookRegistry(settings.enabled_venues, settings.symbols)
        # Metrics are throwaway (private CollectorRegistry): replay reports
        # through its returned stats and the record sink, not Prometheus.
        metrics = Metrics()
        self.detector = SpreadDetector(
            self.registry,
            settings,
            _CountingSink(sink, stats),
            metrics,
            clock=self.clock,
            instruments=instruments,
        )
        # The REAL staleness monitor, ticked on the recorded grid: venue
        # transitions and per-book stale closes carry live semantics.
        self.monitor = StalenessMonitor(
            self.registry,
            settings,
            metrics,
            self.detector.on_venue_unusable,
            on_symbol_degraded=self.detector.evaluate,
        )
        self._venues = {v.value: v for v in settings.enabled_venues}
        self._symbols = set(settings.symbols)
        self._interval_ns = int(settings.monitoring.spread_sample_interval_s * 1e9)
        self._next_sample_mono: int | None = None
        self._next_tick_mono: int | None = None
        self._saw_header = False

    # -- periodic reconstruction (monitor grid + sample sweeps) --------------

    def _advance_to(self, mono_target: int) -> None:
        delta = mono_target - self.clock.mono_ns()
        if delta > 0:
            self.clock.advance(mono_target, self.clock.wall_ns() + delta)

    def _run_periodic(self, upto_mono_ns: int) -> None:
        """Fire every due monitor tick and sample sweep at or before
        ``upto_mono_ns``, in chronological order. On an exact tie the sample
        sweep fires first ("sample" < "tick" in the tuple compare) —
        arbitrary but FIXED; live ordering at coincident deadlines is
        scheduler-dependent, and determinism needs one documented convention."""
        while True:
            candidates: list[tuple[int, str]] = []
            if self._next_tick_mono is not None and self._next_tick_mono <= upto_mono_ns:
                candidates.append((self._next_tick_mono, "tick"))
            if self._next_sample_mono is not None and self._next_sample_mono <= upto_mono_ns:
                candidates.append((self._next_sample_mono, "sample"))
            if not candidates:
                return
            due, kind = min(candidates)
            self._advance_to(due)
            if kind == "tick":
                self.monitor.tick(due)
                self.stats.monitor_ticks += 1
                self._next_tick_mono = due + _TICK_NS
            else:
                for symbol in self.settings.symbols:
                    self.detector.evaluate(symbol, emit_sample=True)
                self.stats.samples_swept += 1
                self._next_sample_mono = due + self._interval_ns

    def _seed_grids(self, mono: int) -> None:
        self._next_sample_mono = mono + self._interval_ns
        self._next_tick_mono = mono + _TICK_NS

    # -- event handling ------------------------------------------------------

    def on_header(self, obj: dict) -> None:
        self.stats.headers += 1
        if self._saw_header:
            # Session boundary: the live equivalent is a process restart —
            # open opportunities were flushed by that session's shutdown; a
            # crash-truncated recording flushes here instead.
            self.detector.close_all("shutdown")
            for book in self.registry.all_books():
                book.clear()
        self._saw_header = True
        _compare_header(obj, self.settings, self.instruments, self.stats)
        mono, wall = int(obj["mono_ns"]), int(obj["wall_ns"])
        self.clock.reset(mono, wall)
        self._seed_grids(mono)

    def on_book_event(self, obj: dict) -> None:
        venue = self._venues.get(obj["v"])
        symbol = obj["s"]
        if venue is None or symbol not in self._symbols:
            self.stats.events_skipped += 1
            return
        mono, wall = int(obj["mono_ns"]), int(obj["wall_ns"])
        if self._next_sample_mono is None:  # headerless (hand-built) stream
            self._seed_grids(mono)
        self._run_periodic(mono)
        self.clock.advance(mono, wall)
        book = self.registry.get(venue, symbol)
        kind = obj["k"]
        if kind == "snapshot":
            book.apply_snapshot(obj["b"], obj["a"], exchange_ts_ms=obj.get("ets"), mono_ns=mono, wall_ns=wall)
        elif kind == "delta":
            book.apply_deltas(obj["b"], obj["a"], exchange_ts_ms=obj.get("ets"), mono_ns=mono, wall_ns=wall)
        elif kind == "clear":
            book.clear()
        elif kind == "sync":
            book.synced = True
        elif kind == "unsync":
            book.synced = False
        elif kind == "eval":
            # A journaled live evaluation point: the ONLY place replay opens
            # opportunities (monitor/sweep evaluations at grid points can
            # also open, exactly as live's monitor-triggered evaluations can).
            self.detector.evaluate(symbol)
            self.stats.evals += 1
        else:
            raise ReplayError(f"unknown book_event kind {kind!r}")
        self.stats.events_applied += 1

    def on_end(self, obj: dict) -> None:
        mono, wall = int(obj["mono_ns"]), int(obj["wall_ns"])
        self._run_periodic(mono)
        self.clock.advance(mono, wall)
        self.detector.close_all("shutdown")
        self._next_sample_mono = None
        self._next_tick_mono = None

    def finish(self) -> None:
        # Crash-truncated recording (no recording_end): flush at last clock.
        self.detector.close_all("shutdown")


def run_replay(
    settings: Settings,
    inputs: list[Path],
    sink: RecordSink,
    *,
    instruments: InstrumentRegistry | None = None,
) -> ReplayStats:
    stats = ReplayStats()
    replayer = _Replayer(settings, sink, instruments, stats)
    for path in inputs:
        stats.files += 1
        try:
            fh = path.open("rb")
        except OSError as exc:
            raise ReplayError(f"cannot read {path}: {exc}") from exc
        with fh:  # stream: journals grow to GBs, never load them whole
            for lineno, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    obj = orjson.loads(line)
                    kind = obj["type"]
                    if kind == "book_event":
                        replayer.on_book_event(obj)
                    elif kind == "recording_header":
                        replayer.on_header(obj)
                    elif kind == "recording_end":
                        replayer.on_end(obj)
                    else:
                        stats.events_skipped += 1
                except ReplayError:
                    raise
                except Exception as exc:
                    raise ReplayError(
                        f"{path}:{lineno}: malformed journal line: {exc!r}"
                    ) from exc
    replayer.finish()
    return stats
