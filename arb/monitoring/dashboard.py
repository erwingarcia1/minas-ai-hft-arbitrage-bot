"""Terminal dashboard (rich). Read-only view over books, detector, staleness.

Refreshes 4x/s. Cosmetic by contract: any exception logs and stops the
dashboard while the trading-relevant tasks continue.

Failure modes: none that affect data collection — see module contract above.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Sequence

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from arb.feeds.base import WsFeed
from arb.feeds.book import BookRegistry
from arb.risk.staleness import StalenessMonitor
from arb.signals.spread import SpreadDetector

logger = logging.getLogger("arb.monitoring.dashboard")

_REFRESH_S = 0.25


def _venues_table(registry: BookRegistry, feeds: Sequence[WsFeed]) -> Table:
    state_by_venue = {f.venue: f.state for f in feeds}
    t = Table(title="Venues / books", expand=True)
    for col in ("venue", "symbol", "state", "synced", "age ms", "best bid", "best ask", "levels"):
        t.add_column(col)
    for book in registry.all_books():
        bb, ba = book.best_bid(), book.best_ask()
        age = book.age_ms()
        t.add_row(
            book.venue.value,
            book.symbol,
            state_by_venue.get(book.venue, "?"),
            "[green]yes[/]" if book.synced else "[red]NO[/]",
            f"{age:,.0f}" if age != float("inf") else "-",
            f"{bb[0]:,.2f}" if bb else "-",
            f"{ba[0]:,.2f}" if ba else "-",
            f"{book.depth()[0]}/{book.depth()[1]}",
        )
    return t


def _spreads_table(detector: SpreadDetector) -> Table:
    t = Table(title="Best current net spreads (per symbol, smallest clip)", expand=True)
    for col in ("symbol", "buy@", "sell@", "clip", "gross bps", "net bps"):
        t.add_column(col)
    best: dict[str, list] = {}
    smallest_clip = min(detector.clips)
    for key, ev in detector.last_evals.items():
        if key[3] != smallest_clip:
            continue
        cur = best.get(ev.symbol)
        if cur is None or ev.net_bps > cur.net_bps:
            best[ev.symbol] = ev
    for symbol in sorted(best):
        ev = best[symbol]
        color = "green" if ev.net_bps >= detector.threshold_bps else (
            "yellow" if ev.net_bps > 0 else "red"
        )
        t.add_row(
            symbol,
            ev.buy_venue.value,
            ev.sell_venue.value,
            f"{ev.clip_usdt:,.0f}",
            f"{ev.gross_bps:+.2f}",
            f"[{color}]{ev.net_bps:+.2f}[/]",
        )
    return t


def _opps_table(detector: SpreadDetector) -> Table:
    t = Table(
        title=f"Recent opportunities (open now: {detector.open_count})", expand=True
    )
    for col in ("symbol", "buy@", "sell@", "clip", "net@open", "max net", "dur ms", "reason"):
        t.add_column(col)
    for rec in reversed(detector.recent_closed):
        t.add_row(
            rec.symbol,
            rec.buy_venue.value,
            rec.sell_venue.value,
            f"{rec.clip_usdt:,.0f}",
            f"{rec.net_bps_open:+.2f}",
            f"{rec.max_net_bps:+.2f}",
            f"{rec.duration_ms:,.0f}",
            rec.close_reason,
        )
    return t


async def run_dashboard(
    registry: BookRegistry,
    detector: SpreadDetector,
    monitor: StalenessMonitor,
    feeds: Sequence[WsFeed],
    stop: asyncio.Event,
) -> None:
    console = Console()
    loop = asyncio.get_running_loop()
    try:
        with Live(console=console, refresh_per_second=4, transient=True) as live:
            while not stop.is_set():
                usable = ", ".join(
                    f"[green]{v.value}[/]" if ok else f"[red]{v.value}[/]"
                    for v, ok in monitor.usable.items()
                )
                header = Panel(
                    f"PHASE 1 — OBSERVER MODE (no order code exists in this build) | usable: {usable}",
                    style="bold",
                )
                group = Group(
                    header,
                    _venues_table(registry, feeds),
                    _spreads_table(detector),
                    _opps_table(detector),
                )
                # live.update contends on Rich's render lock, which the
                # refresh thread holds across blocking tty writes. Run it in
                # a worker so a stalled terminal (Ctrl+S, frozen ssh) blocks
                # that worker — never the event loop feeding the books.
                await loop.run_in_executor(None, live.update, group)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=_REFRESH_S)
                except TimeoutError:
                    pass
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("dashboard crashed; continuing without it")
