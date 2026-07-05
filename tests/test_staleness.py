"""Staleness monitor: venue transitions and the per-book degraded backstop."""

from __future__ import annotations

import asyncio
import time

from arb.feeds.book import BookRegistry
from arb.models import VenueName
from arb.risk.staleness import StalenessMonitor

from tests.conftest import fill_book, make_metrics, make_settings

BTC = "BTC/USDT"


async def _run_ticks(monitor: StalenessMonitor, seconds: float) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(monitor.run(stop))
    await asyncio.sleep(seconds)
    stop.set()
    await task


async def test_venue_transition_and_per_book_backstop() -> None:
    settings = make_settings()
    registry = BookRegistry(settings.enabled_venues, settings.symbols)
    unusable_calls: list[VenueName] = []
    degraded_calls: list[str] = []
    monitor = StalenessMonitor(
        registry,
        settings,
        make_metrics(),
        on_unusable=lambda venue, reason: unusable_calls.append(venue),
        on_symbol_degraded=degraded_calls.append,
    )

    binance = registry.get(VenueName.BINANCE, BTC)
    okx = registry.get(VenueName.OKX, BTC)
    fill_book(binance, bid=99.9, ask=100.0)
    fill_book(okx, bid=100.5, ask=100.6)

    await _run_ticks(monitor, 0.25)
    assert monitor.usable[VenueName.BINANCE]
    assert monitor.usable[VenueName.OKX]
    assert unusable_calls == []

    # Age the okx BTC book past staleness: the venue transition must fire AND
    # the per-book backstop must ask for a re-evaluation of the symbol.
    okx.last_local_mono_ns = time.monotonic_ns() - 600_000_000
    await _run_ticks(monitor, 0.25)
    assert not monitor.usable[VenueName.OKX]
    assert VenueName.OKX in unusable_calls
    assert BTC in degraded_calls


async def test_never_updated_books_are_unusable_from_boot() -> None:
    settings = make_settings()
    registry = BookRegistry(settings.enabled_venues, settings.symbols)
    monitor = StalenessMonitor(
        registry, settings, make_metrics(), on_unusable=lambda v, r: None
    )
    await _run_ticks(monitor, 0.15)
    assert not any(monitor.usable.values())
