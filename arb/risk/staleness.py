"""Per-venue market-data staleness monitor.

Every 100ms it recomputes, per venue: are ALL of that venue's books synced
and younger than ``risk.staleness_ms``? On a usable->unusable transition it
notifies the detector (which force-closes affected opportunities) and logs;
gauges are refreshed on every tick.

KILL-SWITCH SEAM (Phase 2): ``on_unusable`` is the hook where the kill switch
subscribes. In Phase 2 the same transition that today closes theoretical
opportunities will additionally: halt signal->execution flow, cancel all open
orders on every venue, and latch a halted state requiring manual reset. The
monitor is deterministic and runs on the event loop, so halting happens
within one loop cycle of detection — the design requirement.

Failure modes:
- The monitor itself failing is fatal-by-design: its task ending takes the
  whole process down (main gathers it), because running blind is worse than
  not running.
- Books that never received data have age == +inf and are unusable from boot.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from arb.feeds.book import BookRegistry
from arb.models import Settings, VenueName
from arb.monitoring.metrics import Metrics

logger = logging.getLogger("arb.risk.staleness")

_TICK_S = 0.1


class StalenessMonitor:
    def __init__(
        self,
        registry: BookRegistry,
        settings: Settings,
        metrics: Metrics,
        on_unusable: Callable[[VenueName, str], None],
        on_symbol_degraded: Callable[[str], None] | None = None,
    ) -> None:
        self.registry = registry
        self.metrics = metrics
        self.staleness_ms = float(settings.risk.staleness_ms)
        self.venues = settings.enabled_venues
        self.on_unusable = on_unusable
        # Per-BOOK fresh->stale hook (typically detector.evaluate): the venue-
        # level transition alone misses the case where a venue was already
        # unusable via one symbol when another of its books goes stale, which
        # would leave that symbol's opportunities open until the slow sampler.
        self.on_symbol_degraded = on_symbol_degraded
        self.usable: dict[VenueName, bool] = {v: False for v in self.venues}
        self._book_fresh: dict[tuple[VenueName, str], bool] = {}

    def venue_usable(self, venue: VenueName, now_mono_ns: int) -> bool:
        books = self.registry.books_for_venue(venue)
        return all(b.synced and b.age_ms(now_mono_ns) <= self.staleness_ms for b in books)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            now_mono_ns = time.monotonic_ns()
            for venue in self.venues:
                for book in self.registry.books_for_venue(venue):
                    age = book.age_ms(now_mono_ns)
                    self.metrics.book_age_ms.labels(
                        venue=venue.value, symbol=book.symbol
                    ).set(age if age != float("inf") else -1.0)
                    fresh = book.synced and age <= self.staleness_ms
                    key = (venue, book.symbol)
                    if self._book_fresh.get(key, False) and not fresh:
                        # Per-book backstop: re-evaluating the symbol closes
                        # any opportunity leaning on this now-stale book.
                        if self.on_symbol_degraded is not None:
                            self.on_symbol_degraded(book.symbol)
                    self._book_fresh[key] = fresh
                usable_now = self.venue_usable(venue, now_mono_ns)
                self.metrics.venue_usable.labels(venue=venue.value).set(
                    1.0 if usable_now else 0.0
                )
                if self.usable[venue] and not usable_now:
                    logger.warning(
                        "venue %s became UNUSABLE (stale or unsynced books)",
                        venue.value,
                        extra={"venue": venue.value},
                    )
                    self.on_unusable(venue, "venue_unusable")
                elif not self.usable[venue] and usable_now:
                    logger.info(
                        "venue %s became usable", venue.value, extra={"venue": venue.value}
                    )
                self.usable[venue] = usable_now
            try:
                await asyncio.wait_for(stop.wait(), timeout=_TICK_S)
            except TimeoutError:
                pass
