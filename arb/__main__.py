"""Entry point: ``python -m arb <observe|replay|report|instruments> ...``.

Phase 1 modes (all read-only, public data only):
- ``observe``      live books + detector + research/recording logs
- ``replay``       deterministic re-run of a recorded session (Mode 2)
- ``report``       ranked viability report over research logs (§48.10)
- ``instruments``  refresh venue precision metadata from public endpoints

There is no ``--live`` flag because there is nothing it could enable: the
execution package is empty by design. When Phase 3 introduces it, live
trading will require BOTH ``--live`` AND ``ARB_CONFIRM_LIVE_TRADING`` in the
environment.

Failure modes:
- Boot errors (bad config, bad instruments file, metrics port taken) are
  fatal before any connection is opened.
- SIGINT/SIGTERM trigger a graceful stop: feeds close, open theoretical
  opportunities are flushed with reason "shutdown", the research log and the
  book-event recording close (recording ends with a ``recording_end`` stamp).
- The staleness monitor dying takes the process down (running blind is worse
  than not running); a single venue's feed dying does not.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
import time
from pathlib import Path

import orjson

from arb import __version__
from arb.accounting.opportunities import OpportunityLog
from arb.config.loader import ConfigError, load_settings
from arb.feeds.base import WsFeed
from arb.feeds.binance import BinanceFeed
from arb.feeds.bitget import BitgetFeed
from arb.feeds.book import BookRegistry
from arb.feeds.bybit import BybitFeed
from arb.feeds.okx import OkxFeed
from arb.instruments.registry import InstrumentRegistry, InstrumentsError
from arb.models import Settings, VenueName
from arb.monitoring.dashboard import run_dashboard
from arb.monitoring.logging import setup_logging
from arb.monitoring.metrics import Metrics
from arb.recording.recorder import BookEventRecorder
from arb.risk.staleness import StalenessMonitor
from arb.signals.spread import SpreadDetector

logger = logging.getLogger("arb.main")

FEED_CLASSES: dict[VenueName, type[WsFeed]] = {
    VenueName.BINANCE: BinanceFeed,
    VenueName.BYBIT: BybitFeed,
    VenueName.OKX: OkxFeed,
    VenueName.BITGET: BitgetFeed,
}

_BANNER = f"""
=============================================================================
 arb v{__version__} — PHASE 1: MARKET DATA + SPREAD OBSERVER
 This build contains NO order-placement code. Nothing here can trade.
 Public market-data endpoints only; no API keys are read.
=============================================================================
"""


async def _sampler(
    detector: SpreadDetector,
    settings: Settings,
    stop: asyncio.Event,
    anchor_mono_ns: int,
) -> None:
    """Periodic unconditional spread samples -> research log. Also acts as a
    lifecycle backstop: it re-evaluates even when no book has ticked.

    Sweeps are scheduled on an ABSOLUTE deadline grid anchored at the session
    start (the same anchor the recording header stamps), not by re-arming a
    relative timer after each sweep: a relative timer accumulates unbounded
    drift (interval + evaluation cost + scheduling lateness per iteration),
    which the replay engine — which sweeps on the exact grid — could not
    reproduce. Residual live-vs-replay skew is per-sweep wakeup lateness
    only; it does not accumulate."""
    interval_ns = int(settings.monitoring.spread_sample_interval_s * 1e9)
    k = 1
    while not stop.is_set():
        deadline = anchor_mono_ns + k * interval_ns
        delay_s = max(0.0, (deadline - time.monotonic_ns()) / 1e9)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay_s)
        except TimeoutError:
            pass
        if stop.is_set():
            return
        for symbol in settings.symbols:
            detector.evaluate(symbol, emit_sample=True)
        # Next deadline strictly after "now": a long stall skips missed
        # boundaries rather than firing a burst of catch-up sweeps.
        now = time.monotonic_ns()
        while anchor_mono_ns + k * interval_ns <= now:
            k += 1


def load_instruments(settings: Settings) -> InstrumentRegistry:
    """Instrument metadata is part of correctness (rounding, minimums): a
    configured-but-unloadable file is fatal at boot, same as a bad config.
    ``instruments_file: null`` disables rounding explicitly and loudly."""
    if settings.instruments_file is None:
        logger.warning(
            "instruments_file is null: clip quantities will NOT be rounded to venue rules"
        )
        return InstrumentRegistry.empty()
    return InstrumentRegistry.load(settings.instruments_file)


async def run_observer(settings: Settings, use_dashboard: bool, log_level: str) -> None:
    use_dashboard = use_dashboard and sys.stdout.isatty()
    setup_logging(log_level, settings.monitoring.log_dir if use_dashboard else None)

    metrics = Metrics()
    metrics.start_server(settings.monitoring.metrics_port)
    logger.info(
        "observer starting",
        extra={
            "version": __version__,
            "venues": [v.value for v in settings.enabled_venues],
            "symbols": settings.symbols,
            "metrics_port": settings.monitoring.metrics_port,
            "recording": settings.recording.enabled,
        },
    )

    instruments = load_instruments(settings)
    registry = BookRegistry(settings.enabled_venues, settings.symbols)
    # One anchor for the session: recording header stamp AND sampler grid,
    # so replay reconstructs the sweep schedule from the header exactly.
    anchor_wall_ns = time.time_ns()
    anchor_mono_ns = time.monotonic_ns()
    recorder: BookEventRecorder | None = None
    if settings.recording.enabled:
        recorder = BookEventRecorder(
            settings.recording.dir,
            settings,
            metrics,
            wall_ns=anchor_wall_ns,
            mono_ns=anchor_mono_ns,
            instruments=instruments,
        )
        recorder.attach(registry)
    research_log = OpportunityLog(settings.data_dir, metrics)
    detector = SpreadDetector(registry, settings, research_log, metrics, instruments=instruments)
    monitor = StalenessMonitor(
        registry,
        settings,
        metrics,
        detector.on_venue_unusable,
        on_symbol_degraded=detector.evaluate,
    )

    # Feed callback: journal the evaluation point FIRST (replay re-evaluates
    # exactly at journaled eval events — see arb/recording), then evaluate.
    def on_book_update(venue: VenueName, symbol: str) -> None:
        if recorder is not None:
            recorder.on_evaluation(venue, symbol)
        detector.on_book_update(venue, symbol)

    # REGION-SPLITTING SEAM (Phase 4): feeds publish via a plain callback here.
    # In the multi-region design, each feed process runs next to its venue
    # (Tokyo/Singapore/HK) and this callback becomes a bus publish; the
    # detector consumes the bus where the execution engine lives.
    feeds = [
        FEED_CLASSES[venue](settings.symbols, registry, on_book_update, metrics)
        for venue in settings.enabled_venues
    ]

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    feed_tasks = [
        asyncio.create_task(feed.run(stop), name=f"feed-{feed.venue.value}") for feed in feeds
    ]
    core_tasks = [
        asyncio.create_task(monitor.run(stop), name="staleness-monitor"),
        asyncio.create_task(
            _sampler(detector, settings, stop, anchor_mono_ns), name="spread-sampler"
        ),
    ]
    dash_task = (
        asyncio.create_task(
            run_dashboard(registry, detector, monitor, feeds, stop), name="dashboard"
        )
        if use_dashboard
        else None
    )

    # A feed dying (a bug — run() swallows connection errors) degrades to one
    # venue offline: its books go permanently unusable, which the staleness
    # monitor makes loudly visible. The other venues keep collecting data.
    def _log_feed_death(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "feed task %s died: %r — venue offline until restart",
                task.get_name(),
                exc,
                exc_info=exc,
            )

    for task in feed_tasks:
        task.add_done_callback(_log_feed_death)

    try:
        # The core tasks ARE load-bearing: the staleness monitor dying means
        # running blind, so it stops everything.
        done, _ = await asyncio.wait(core_tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            exc = task.exception()
            if exc is not None:
                logger.error(
                    "task %s died: %r — shutting down", task.get_name(), exc,
                    exc_info=exc,
                )
                raise exc
    finally:
        stop.set()
        if recorder is not None:
            # Detach BEFORE cancelling feeds: task-cancellation unsyncs are
            # shutdown mechanics, not market data. No await sits between
            # detach and the cancellations, so no frame can slip through
            # unjournaled on this single-threaded loop.
            recorder.detach(registry)
        all_tasks = feed_tasks + core_tasks + ([dash_task] if dash_task else [])
        for task in all_tasks:
            task.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)
        detector.close_all("shutdown")
        research_log.close()
        if recorder is not None:
            # After close_all: replay flushes leftovers at the recording_end
            # stamp with reason "shutdown", matching live to within the
            # scheduling microseconds between close_all's clock reads and
            # this stamp (documented in docs/MARKET_REPLAY.md).
            recorder.close(wall_ns=time.time_ns(), mono_ns=time.monotonic_ns())
        logger.info("observer stopped")


class _FileSink:
    """JSONL sink for replay output ('-' -> stdout). Opens with truncation:
    re-running a replay to the same path must REPLACE the records — appending
    would silently break the byte-identical determinism-verification workflow
    (two replays, one diff/hash)."""

    def __init__(self, path: Path | None) -> None:
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = sys.stdout.buffer if path is None else path.open("wb")
        self._is_stdout = path is None

    def write(self, record: dict) -> None:
        self._fh.write(orjson.dumps(record))
        self._fh.write(b"\n")

    def close(self) -> None:
        if self._is_stdout:
            self._fh.flush()
        else:
            self._fh.close()


def _cmd_observe(args: argparse.Namespace, settings: Settings) -> int:
    print(_BANNER, file=sys.stderr)
    runner = asyncio.run
    try:
        import uvloop

        runner = uvloop.run
    except ImportError:
        print("uvloop unavailable; using default event loop", file=sys.stderr)
    try:
        runner(
            run_observer(settings, use_dashboard=not args.no_dashboard, log_level=args.log_level)
        )
    except KeyboardInterrupt:
        pass
    return 0


def _cmd_replay(args: argparse.Namespace, settings: Settings) -> int:
    from arb.replay.engine import ReplayError, run_replay

    setup_logging(args.log_level, None)
    inputs = [Path(p) for p in args.input]
    missing = [p for p in inputs if not p.is_file()]
    if missing:
        print(f"FATAL: recording file(s) not found: {missing}", file=sys.stderr)
        return 1
    instruments = load_instruments(settings)
    if args.output == "-":
        out_path = None
    elif args.output is not None:
        out_path = Path(args.output)
    else:
        from datetime import datetime, timezone

        # Microsecond stamp: two replays within the same second must never
        # silently share (and previously append to) one output file.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f") + "Z"
        out_path = settings.data_dir / "replays" / f"replay-{stamp}.jsonl"
    try:
        sink = _FileSink(out_path)
    except OSError as exc:
        print(f"FATAL: cannot open output {out_path}: {exc}", file=sys.stderr)
        return 1
    try:
        stats = run_replay(settings, inputs, sink, instruments=instruments)
    except ReplayError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    finally:
        sink.close()
    print(
        f"replay complete: {stats.files} file(s), {stats.headers} session(s), "
        f"{stats.events_applied} events applied ({stats.events_skipped} skipped), "
        f"{stats.samples_swept} sample sweeps, {stats.opportunities} opportunities, "
        f"{stats.records_written} records"
        + (f" -> {out_path}" if out_path is not None else ""),
        file=sys.stderr,
    )
    for warning in stats.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    return 0


def _cmd_report(args: argparse.Namespace, settings: Settings) -> int:
    from arb.report.viability import build_report, load_records, render_table

    if args.input:
        paths = [Path(p) for p in args.input]
    else:
        paths = sorted(settings.data_dir.glob("opportunities-*.jsonl"))
    missing = [p for p in paths if not p.is_file()]
    if missing:
        print(f"FATAL: record file(s) not found: {missing}", file=sys.stderr)
        return 1
    report = build_report(load_records(paths), min_duration_ms=args.min_duration_ms)
    sys.stdout.write(render_table(report))
    if args.json is not None:
        Path(args.json).write_bytes(orjson.dumps(report.to_dict(), option=orjson.OPT_INDENT_2))
        print(f"json report -> {args.json}", file=sys.stderr)
    return 0


def _cmd_instruments(args: argparse.Namespace, settings: Settings) -> int:
    from arb.instruments.fetch import FetchError, fetch_all, write_instruments_yaml

    if args.action != "fetch":
        print("usage: arb instruments fetch [--out PATH]", file=sys.stderr)
        return 2
    setup_logging(args.log_level, None)
    try:
        metas = asyncio.run(fetch_all(settings.enabled_venues, settings.symbols))
    except FetchError as exc:
        print(
            f"FATAL: {exc}\n(note: some environments block exchange egress; "
            f"run this from an operator machine)",
            file=sys.stderr,
        )
        return 1
    out = Path(args.out)
    write_instruments_yaml(out, metas)
    print(f"wrote {len(metas)} instruments -> {out}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arb", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    observe = sub.add_parser("observe", help="run the Phase 1 spread observer")
    observe.add_argument("--no-dashboard", action="store_true", help="disable terminal dashboard")

    replay = sub.add_parser("replay", help="deterministically re-run a recorded session")
    replay.add_argument(
        "--input", nargs="+", required=True, help="recording file(s) in session order"
    )
    replay.add_argument(
        "--output",
        default=None,
        help="records output path, '-' for stdout (default: <data_dir>/replays/replay-<utc>.jsonl)",
    )

    report = sub.add_parser("report", help="ranked viability report from research logs")
    report.add_argument(
        "--input", nargs="*", default=None, help="record file(s); default: <data_dir>/opportunities-*.jsonl"
    )
    report.add_argument(
        "--min-duration-ms",
        type=float,
        default=250.0,
        help="persistence floor: opportunities shorter than this are unexecutable fiction "
        "for a non-colocated system (default 250)",
    )
    report.add_argument("--json", default=None, help="also write the report as JSON to this path")

    instruments = sub.add_parser("instruments", help="instrument metadata utilities")
    instruments.add_argument("action", choices=["fetch"], help="fetch: refresh from public endpoints")
    instruments.add_argument(
        "--out", default="config/instruments.yaml", help="output YAML path"
    )

    for p in (observe, replay, report, instruments):
        p.add_argument(
            "--config", type=Path, default=Path("config/config.yaml"), help="YAML config path"
        )
        p.add_argument("--log-level", default="INFO", help="root log level")

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2

    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1

    try:
        if args.command == "observe":
            return _cmd_observe(args, settings)
        if args.command == "replay":
            return _cmd_replay(args, settings)
        if args.command == "report":
            return _cmd_report(args, settings)
        if args.command == "instruments":
            return _cmd_instruments(args, settings)
    except InstrumentsError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
