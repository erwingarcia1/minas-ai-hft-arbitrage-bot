"""Cross-venue executable-spread detector and opportunity lifecycle.

For every symbol, ordered venue pair (buy on A, sell on B) and configured clip
size, the detector prices BOTH legs by walking the live books (slippage is
therefore inherent in the edge) and nets out both taker fees. The candidate
quantity is rounded DOWN to a step executable on both venues and checked
against both venues' minimums (instrument metadata) before pricing — fees AND
rounding are part of every evaluation, per CLAUDE.md §48.6. An opportunity
"opens" when net edge >= threshold and both books are synced and fresh; it
"closes" when the edge decays, depth vanishes, or either book becomes
unusable — and the full open->close record is written to the research log.

Time comes exclusively from the injected ``Clock`` (default: SystemClock).
The replay engine substitutes a ReplayClock driven by recorded stamps, which
is what makes replayed detection deterministic (§35).

Failure modes:
- Books that are unsynced or older than ``risk.staleness_ms`` are excluded
  from evaluation; opportunities touching them are force-closed with reason
  ``book_unusable`` / ``venue_unusable`` rather than left dangling.
- Insufficient depth for a clip closes (reason ``insufficient_depth``) —
  a thin book must not keep an opportunity nominally open. A clip that
  violates a venue's minimum quantity/notional after rounding is equally
  unexecutable and takes the same path.
- A suspended instrument excludes its (venue, symbol) from evaluation
  entirely; missing instrument metadata falls back to unrounded pricing
  with a WARNING at construction (visible, deliberate degradation).
- On shutdown, ``close_all("shutdown")`` flushes every open opportunity so
  the JSONL log never ends with phantom open intervals.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from itertools import permutations
from typing import Protocol

from arb.feeds.book import BookRegistry, OrderBook
from arb.instruments.registry import InstrumentRegistry, round_qty_for_pair
from arb.models import OpportunityRecord, Settings, SpreadSample, VenueName
from arb.monitoring.metrics import Metrics
from arb.replay.clock import Clock, SystemClock
from arb.signals.fees import gross_edge_bps, net_edge_bps

logger = logging.getLogger("arb.signals.spread")

# (symbol, buy venue, sell venue, clip usdt)
OppKey = tuple[str, VenueName, VenueName, float]


class RecordSink(Protocol):
    def write(self, record: dict) -> None: ...


@dataclass(slots=True)
class EvalResult:
    """Latest pricing of one pair/direction/clip (for dashboard + samples)."""

    symbol: str
    buy_venue: VenueName
    sell_venue: VenueName
    clip_usdt: float
    qty_base: float
    buy_vwap: float
    sell_vwap: float
    gross_bps: float
    net_bps: float
    mono_ns: int


@dataclass(slots=True)
class _OpenOpp:
    opened_wall: datetime
    opened_mono_ns: int
    gross_bps_open: float
    net_bps_open: float
    buy_vwap_open: float
    sell_vwap_open: float
    qty_base: float
    max_net_bps: float
    sum_net_bps: float
    samples: int


def _utc_iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


class SpreadDetector:
    def __init__(
        self,
        registry: BookRegistry,
        settings: Settings,
        sink: RecordSink,
        metrics: Metrics,
        *,
        clock: Clock | None = None,
        instruments: InstrumentRegistry | None = None,
    ) -> None:
        self.registry = registry
        self.settings = settings
        self.sink = sink
        self.metrics = metrics
        self.clock: Clock = clock if clock is not None else SystemClock()
        self.instruments = instruments if instruments is not None else InstrumentRegistry.empty()
        self.threshold_bps = settings.signals.min_net_edge_bps
        self.staleness_ms = float(settings.risk.staleness_ms)
        self.clips = settings.signals.clip_sizes_usdt
        self._open: dict[OppKey, _OpenOpp] = {}
        self.last_evals: dict[OppKey, EvalResult] = {}
        self.recent_closed: deque[OpportunityRecord] = deque(maxlen=10)
        # Precomputed metadata per (venue, symbol): suspended instruments are
        # excluded from evaluation entirely; missing metadata degrades to
        # unrounded pricing, loudly and only once.
        self._suspended: set[tuple[VenueName, str]] = set()
        for venue in settings.enabled_venues:
            for symbol in settings.symbols:
                meta = self.instruments.get(venue, symbol)
                if meta is None:
                    if len(self.instruments) > 0:
                        logger.warning(
                            "no instrument metadata for %s %s: clip quantities "
                            "will not be rounded to venue rules",
                            venue.value,
                            symbol,
                            extra={"venue": venue.value, "symbol": symbol},
                        )
                elif meta.status != "trading":
                    logger.warning(
                        "instrument %s %s status=%s: excluded from evaluation",
                        venue.value,
                        symbol,
                        meta.status,
                        extra={"venue": venue.value, "symbol": symbol},
                    )
                    self._suspended.add((venue, symbol))

    # -- entry points ---------------------------------------------------------

    def on_book_update(self, venue: VenueName, symbol: str) -> None:
        """Feed callback: re-evaluate the updated symbol across all pairs."""
        self.evaluate(symbol)

    def on_venue_unusable(self, venue: VenueName, reason: str = "venue_unusable") -> None:
        """Called by the staleness monitor / feeds when a venue degrades:
        every open opportunity touching the venue closes immediately."""
        for key in [k for k in self._open if k[1] == venue or k[2] == venue]:
            self._close(key, reason)

    def close_all(self, reason: str) -> None:
        for key in list(self._open):
            self._close(key, reason)

    @property
    def open_count(self) -> int:
        return len(self._open)

    # -- evaluation -------------------------------------------------------------

    def _usable(self, book: OrderBook, now_mono_ns: int) -> bool:
        return book.synced and book.age_ms(now_mono_ns) <= self.staleness_ms

    def evaluate(self, symbol: str, emit_sample: bool = False) -> None:
        now_mono_ns = self.clock.mono_ns()
        books = {
            b.venue: b
            for b in self.registry.books_for_symbol(symbol)
            if b.venue in self.settings.enabled_venues
            and (b.venue, symbol) not in self._suspended
        }
        usable = {v: b for v, b in books.items() if self._usable(b, now_mono_ns)}

        # Close anything whose legs are no longer both usable, and drop its
        # last pricing — the dashboard/samplers must never present a spread
        # computed from data that is now stale or unsynced as current.
        for key in [
            k
            for k in self._open
            if k[0] == symbol and (k[1] not in usable or k[2] not in usable)
        ]:
            self._close(key, "book_unusable")
        for key in [
            k
            for k in self.last_evals
            if k[0] == symbol and (k[1] not in usable or k[2] not in usable)
        ]:
            self.last_evals.pop(key)

        for buy_venue, sell_venue in permutations(usable, 2):
            buy_book = usable[buy_venue]
            sell_book = usable[sell_venue]
            for clip in self.clips:
                key: OppKey = (symbol, buy_venue, sell_venue, clip)
                result, reject_reason = self._price(buy_book, sell_book, clip, now_mono_ns)
                if result is None:
                    self.last_evals.pop(key, None)
                    if key in self._open:
                        self._close(key, reject_reason or "unpriceable")
                    continue
                self.last_evals[key] = result
                self._observe_metrics(result)
                if emit_sample:
                    self._emit_sample(result, buy_book, sell_book, now_mono_ns)
                if result.net_bps >= self.threshold_bps:
                    self._open_or_extend(key, result)
                elif key in self._open:
                    self._close(key, "edge_below_threshold")

    def _price(
        self,
        buy_book: OrderBook,
        sell_book: OrderBook,
        clip_usdt: float,
        now_mono_ns: int,
    ) -> tuple[EvalResult | None, str | None]:
        """Returns (result, None) when priceable, else (None, reject_reason).
        The reason distinguishes true depth exhaustion from venue-rule
        violations in the research log — mislabeled close causes would poison
        the dataset's rejected-opportunity statistics (§37)."""
        buy_ask = buy_book.best_ask()
        sell_bid = sell_book.best_bid()
        if buy_ask is None or sell_bid is None:
            return None, "insufficient_depth"
        # Clip is quote notional; convert once to base qty at the pair's
        # cross-venue mid so both legs price the SAME base amount.
        ref_price = (buy_ask[0] + sell_bid[0]) / 2.0
        if ref_price <= 0:
            return None, "insufficient_depth"
        buy_meta = self.instruments.get(buy_book.venue, buy_book.symbol)
        sell_meta = self.instruments.get(sell_book.venue, sell_book.symbol)
        # Round DOWN to a quantity executable on both venues; a clip whose
        # rounded quantity violates either venue's minimum quantity is
        # unexecutable — same outcome as missing depth, distinct label.
        qty = round_qty_for_pair(clip_usdt / ref_price, buy_meta, sell_meta)
        if qty is None:
            return None, "below_venue_minimums"
        buy_vwap = buy_book.vwap_for_qty("ask", qty)
        sell_vwap = sell_book.vwap_for_qty("bid", qty)
        if buy_vwap is None or sell_vwap is None:
            return None, "insufficient_depth"
        # Minimum notionals need executable prices, so they are checked last.
        if buy_meta is not None and qty * buy_vwap < float(buy_meta.min_notional):
            return None, "below_venue_minimums"
        if sell_meta is not None and qty * sell_vwap < float(sell_meta.min_notional):
            return None, "below_venue_minimums"
        gross = gross_edge_bps(buy_vwap, sell_vwap)
        net = net_edge_bps(
            buy_vwap,
            sell_vwap,
            self.settings.taker_bps(buy_book.venue),
            self.settings.taker_bps(sell_book.venue),
        )
        return (
            EvalResult(
                symbol=buy_book.symbol,
                buy_venue=buy_book.venue,
                sell_venue=sell_book.venue,
                clip_usdt=clip_usdt,
                qty_base=qty,
                buy_vwap=buy_vwap,
                sell_vwap=sell_vwap,
                gross_bps=gross,
                net_bps=net,
                mono_ns=now_mono_ns,
            ),
            None,
        )

    # -- opportunity lifecycle ----------------------------------------------------

    def _open_or_extend(self, key: OppKey, result: EvalResult) -> None:
        opp = self._open.get(key)
        if opp is None:
            self._open[key] = _OpenOpp(
                opened_wall=self.clock.now_utc(),
                opened_mono_ns=result.mono_ns,
                gross_bps_open=result.gross_bps,
                net_bps_open=result.net_bps,
                buy_vwap_open=result.buy_vwap,
                sell_vwap_open=result.sell_vwap,
                qty_base=result.qty_base,
                max_net_bps=result.net_bps,
                sum_net_bps=result.net_bps,
                samples=1,
            )
            logger.info(
                "opportunity OPEN %s buy=%s sell=%s clip=%.0f net=%.2fbps gross=%.2fbps",
                key[0],
                key[1].value,
                key[2].value,
                key[3],
                result.net_bps,
                result.gross_bps,
                extra={"symbol": key[0]},
            )
        else:
            opp.max_net_bps = max(opp.max_net_bps, result.net_bps)
            opp.sum_net_bps += result.net_bps
            opp.samples += 1

    def _close(self, key: OppKey, reason: str) -> None:
        opp = self._open.pop(key)
        symbol, buy_venue, sell_venue, clip = key
        closed_wall = self.clock.now_utc()
        duration_ms = (self.clock.mono_ns() - opp.opened_mono_ns) / 1e6
        record = OpportunityRecord(
            symbol=symbol,
            buy_venue=buy_venue,
            sell_venue=sell_venue,
            clip_usdt=clip,
            qty_base=opp.qty_base,
            opened_at=_utc_iso(opp.opened_wall),
            closed_at=_utc_iso(closed_wall),
            duration_ms=duration_ms,
            gross_bps_open=opp.gross_bps_open,
            net_bps_open=opp.net_bps_open,
            max_net_bps=opp.max_net_bps,
            mean_net_bps=opp.sum_net_bps / opp.samples,
            samples=opp.samples,
            buy_vwap_open=opp.buy_vwap_open,
            sell_vwap_open=opp.sell_vwap_open,
            # Profit on the EXECUTABLE notional (rounded qty at open VWAPs),
            # not the configured clip: rounding/max-qty can shrink the
            # executable quantity well below clip/ref, and a clip-based
            # figure would overstate every capturable-profit aggregate built
            # on it. Fee-explicit: proceeds*(1-f_sell) - cost*(1+f_buy).
            est_profit_usdt_open=opp.qty_base
            * (
                opp.sell_vwap_open * (1.0 - self.settings.taker_bps(sell_venue) / 1e4)
                - opp.buy_vwap_open * (1.0 + self.settings.taker_bps(buy_venue) / 1e4)
            ),
            threshold_bps=self.threshold_bps,
            close_reason=reason,
        )
        self.sink.write(record.model_dump(mode="json"))
        self.recent_closed.append(record)
        self.metrics.opportunities.labels(
            symbol=symbol, buy_venue=buy_venue.value, sell_venue=sell_venue.value
        ).inc()
        self.metrics.opportunity_duration_ms.observe(duration_ms)
        logger.info(
            "opportunity CLOSE %s buy=%s sell=%s clip=%.0f dur=%.0fms max_net=%.2fbps reason=%s",
            symbol,
            buy_venue.value,
            sell_venue.value,
            clip,
            duration_ms,
            opp.max_net_bps,
            reason,
            extra={"symbol": symbol},
        )

    # -- research output -----------------------------------------------------------

    def _emit_sample(
        self,
        result: EvalResult,
        buy_book: OrderBook,
        sell_book: OrderBook,
        now_mono_ns: int,
    ) -> None:
        sample = SpreadSample(
            ts=_utc_iso(self.clock.now_utc()),
            symbol=result.symbol,
            buy_venue=result.buy_venue,
            sell_venue=result.sell_venue,
            clip_usdt=result.clip_usdt,
            qty_base=result.qty_base,
            buy_vwap=result.buy_vwap,
            sell_vwap=result.sell_vwap,
            gross_bps=result.gross_bps,
            net_bps=result.net_bps,
            buy_book_age_ms=buy_book.age_ms(now_mono_ns),
            sell_book_age_ms=sell_book.age_ms(now_mono_ns),
        )
        self.sink.write(sample.model_dump(mode="json"))

    def _observe_metrics(self, result: EvalResult) -> None:
        labels = {
            "symbol": result.symbol,
            "buy_venue": result.buy_venue.value,
            "sell_venue": result.sell_venue.value,
        }
        self.metrics.spread_gross_bps.labels(**labels).observe(result.gross_bps)
        self.metrics.spread_net_bps.labels(**labels).observe(result.net_bps)
