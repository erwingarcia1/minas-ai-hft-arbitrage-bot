"""Entry point: ``python -m arb observe --config config/config.yaml``.

Phase 1 exposes exactly one mode — ``observe`` — which maintains books,
detects theoretical opportunities, and records research data. There is no
``--live`` flag because there is nothing it could enable: the execution
package is empty by design. When Phase 3 introduces it, live trading will
require BOTH ``--live`` AND ``ARB_CONFIRM_LIVE_TRADING`` in the environment.

Failure modes:
- Boot errors (bad config, metrics port taken) are fatal before any
  connection is opened.
- SIGINT/SIGTERM trigger a graceful stop: feeds close, open theoretical
  opportunities are flushed with reason "shutdown", the research log closes.
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
from pathlib import Path

from arb import __version__
from arb.accounting.opportunities import OpportunityLog
from arb.config.loader import ConfigError, load_settings
from arb.feeds.base import WsFeed
from arb.feeds.binance import BinanceFeed
from arb.feeds.bitget import BitgetFeed
from arb.feeds.book import BookRegistry
from arb.feeds.bybit import BybitFeed
from arb.feeds.okx import OkxFeed
from arb.models import Settings, VenueName
from arb.monitoring.dashboard import run_dashboard
from arb.monitoring.logging import setup_logging
from arb.monitoring.metrics import Metrics
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


async def _sampler(detector: SpreadDetector, settings: Settings, stop: asyncio.Event) -> None:
    """Periodic unconditional spread samples -> research log. Also acts as a
    lifecycle backstop: it re-evaluates even when no book has ticked."""
    interval = settings.monitoring.spread_sample_interval_s
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass
        if stop.is_set():
            return
        for symbol in settings.symbols:
            detector.evaluate(symbol, emit_sample=True)


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
        },
    )

    registry = BookRegistry(settings.enabled_venues, settings.symbols)
    research_log = OpportunityLog(settings.data_dir, metrics)
    detector = SpreadDetector(registry, settings, research_log, metrics)
    monitor = StalenessMonitor(
        registry,
        settings,
        metrics,
        detector.on_venue_unusable,
        on_symbol_degraded=detector.evaluate,
    )

    # REGION-SPLITTING SEAM (Phase 4): feeds publish via a plain callback here.
    # In the multi-region design, each feed process runs next to its venue
    # (Tokyo/Singapore/HK) and this callback becomes a bus publish; the
    # detector consumes the bus where the execution engine lives.
    feeds = [
        FEED_CLASSES[venue](settings.symbols, registry, detector.on_book_update, metrics)
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
        asyncio.create_task(_sampler(detector, settings, stop), name="spread-sampler"),
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
        all_tasks = feed_tasks + core_tasks + ([dash_task] if dash_task else [])
        for task in all_tasks:
            task.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)
        detector.close_all("shutdown")
        research_log.close()
        logger.info("observer stopped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arb", description=__doc__)
    sub = parser.add_subparsers(dest="command")
    observe = sub.add_parser("observe", help="run the Phase 1 spread observer")
    observe.add_argument(
        "--config", type=Path, default=Path("config/config.yaml"), help="YAML config path"
    )
    observe.add_argument("--no-dashboard", action="store_true", help="disable terminal dashboard")
    observe.add_argument("--log-level", default="INFO", help="root log level")
    args = parser.parse_args(argv)

    if args.command != "observe":
        parser.print_help()
        return 2

    print(_BANNER, file=sys.stderr)
    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1

    runner = asyncio.run
    try:
        import uvloop

        runner = uvloop.run
    except ImportError:
        print("uvloop unavailable; using default event loop", file=sys.stderr)

    try:
        runner(run_observer(settings, use_dashboard=not args.no_dashboard, log_level=args.log_level))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
