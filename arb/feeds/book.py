"""Local L2 order-book engine shared by all venue adapters.

Prices and sizes are stored as ``Decimal`` keyed exactly as the exchange sent
them: OKX/Bitget CRC32 checksums are computed over the *literal* price/size
strings (trailing zeros included), and ``Decimal`` round-trips those strings
faithfully where ``float`` formatting would not.

Signal math reads float views (``iter_bids``/``iter_asks``/``vwap_for_qty``);
float64 precision is far beyond bps-level spread math.

Recording tap: every state change flows through ``apply_snapshot`` /
``apply_deltas`` / ``clear`` / the ``synced`` property, and each fires the
optional ``tap`` callback with the literal levels and all three timestamps.
The deterministic market-data journal (arb/recording) and therefore the
replay engine (arb/replay) are built on this seam — replay re-applies tap
events instead of re-running venue protocols.

Failure modes:
- A book is only trustworthy when ``synced`` is True AND ``age_ms()`` is small;
  consumers must check both. ``synced`` is cleared by the owning feed on
  disconnect, sequence gap, or checksum mismatch.
- ``vwap_for_qty`` returns None when the book lacks depth for the clip —
  callers must treat that as "no executable price", never fall back to
  top-of-book.
- Exchanges send plain decimal strings; scientific notation would break
  checksum reconstruction and is asserted against in tests.
- A raising ``tap`` is contained and logged: recording failures must degrade
  recording, never market-data processing.
"""

from __future__ import annotations

import logging
import time
import zlib
from decimal import Decimal
from itertools import islice
from typing import Callable, Iterable, Iterator, Literal

from sortedcontainers import SortedDict

from arb.models import VenueName

logger = logging.getLogger("arb.feeds.book")

RawLevel = tuple[str, str]  # (price, size) exactly as received

# Tap event kinds, emitted in application order:
#   "snapshot"  levels replaced wholesale (book was cleared first)
#   "delta"     incremental level changes applied
#   "clear"     levels dropped (disconnect/resync); an "unsync" follows if the
#               book was synced
#   "sync"      synced False -> True
#   "unsync"    synced True -> False
BookEventKind = Literal["snapshot", "delta", "clear", "sync", "unsync"]
# tap(book, kind, bids, asks, exchange_ts_ms, mono_ns, wall_ns)
BookTap = Callable[
    ["OrderBook", BookEventKind, list[RawLevel], list[RawLevel], "int | None", int, int],
    None,
]

_CHECKSUM_DEPTH = 25  # OKX and Bitget both checksum the top 25 levels


def crc32_signed(payload: str) -> int:
    """CRC32 as a signed int32, matching OKX/Bitget checksum encoding."""
    v = zlib.crc32(payload.encode("ascii"))
    return v - 2**32 if v >= 2**31 else v


def _plain(d: Decimal) -> str:
    """Fixed-point rendering of a Decimal, never scientific notation.

    ``str(Decimal("0.0000001"))`` is ``"1E-7"`` (Decimal switches to
    scientific notation below 1e-6), which would corrupt checksum payloads
    for small-priced assets. ``format(d, "f")`` preserves the exchange's
    literal digits (trailing zeros included) in plain notation.
    """
    return format(d, "f")


class OrderBook:
    """One venue's L2 book for one symbol. Not thread-safe by design: all
    mutation happens on the single asyncio event loop."""

    __slots__ = (
        "venue",
        "symbol",
        "bids",
        "asks",
        "_synced",
        "last_local_mono_ns",
        "last_local_wall_ns",
        "last_exchange_ts_ms",
        "tap",
    )

    def __init__(self, venue: VenueName, symbol: str) -> None:
        self.venue = venue
        self.symbol = symbol
        self.bids: SortedDict = SortedDict()  # Decimal price -> Decimal size (asc)
        self.asks: SortedDict = SortedDict()
        self._synced: bool = False
        self.last_local_mono_ns: int = 0
        self.last_local_wall_ns: int = 0
        self.last_exchange_ts_ms: int | None = None
        self.tap: BookTap | None = None

    # -- sync state ----------------------------------------------------------

    @property
    def synced(self) -> bool:
        return self._synced

    @synced.setter
    def synced(self, value: bool) -> None:
        if value == self._synced:
            return
        self._synced = value
        # Sync transitions happen in feed control flow, not on a data frame,
        # so they have no stamps of their own: they are journaled at the
        # book's last data-frame stamps. Every recorded timestamp therefore
        # originates from feed data — one time source per journal — and the
        # replay clock is never advanced by control-flow events. (File order
        # still records the true application order.)
        self._emit(
            "sync" if value else "unsync",
            [],
            [],
            None,
            self.last_local_mono_ns,
            self.last_local_wall_ns,
        )

    # -- mutation ----------------------------------------------------------

    def apply_snapshot(
        self,
        bids: Iterable[RawLevel],
        asks: Iterable[RawLevel],
        *,
        exchange_ts_ms: int | None,
        mono_ns: int,
        wall_ns: int,
    ) -> None:
        bids = list(bids)
        asks = list(asks)
        self.bids.clear()
        self.asks.clear()
        self._apply_levels(bids, asks)
        self._stamp(exchange_ts_ms, mono_ns, wall_ns)
        self._emit("snapshot", bids, asks, exchange_ts_ms, mono_ns, wall_ns)

    def apply_deltas(
        self,
        bids: Iterable[RawLevel],
        asks: Iterable[RawLevel],
        *,
        exchange_ts_ms: int | None,
        mono_ns: int,
        wall_ns: int,
    ) -> None:
        bids = list(bids)
        asks = list(asks)
        self._apply_levels(bids, asks)
        self._stamp(exchange_ts_ms, mono_ns, wall_ns)
        self._emit("delta", bids, asks, exchange_ts_ms, mono_ns, wall_ns)

    def _apply_levels(self, bids: list[RawLevel], asks: list[RawLevel]) -> None:
        for price_s, size_s in bids:
            self._set_level(self.bids, price_s, size_s)
        for price_s, size_s in asks:
            self._set_level(self.asks, price_s, size_s)

    def _stamp(self, exchange_ts_ms: int | None, mono_ns: int, wall_ns: int) -> None:
        self.last_local_mono_ns = mono_ns
        self.last_local_wall_ns = wall_ns
        if exchange_ts_ms is not None:
            self.last_exchange_ts_ms = exchange_ts_ms

    @staticmethod
    def _set_level(side: SortedDict, price_s: str, size_s: str) -> None:
        price = Decimal(price_s)
        if Decimal(size_s) == 0:
            side.pop(price, None)
        else:
            # Store the Decimal parsed from the literal string so that
            # str(size) reproduces the exchange's formatting for checksums.
            side[price] = Decimal(size_s)

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()
        # Control-flow event: journaled at last data-frame stamps (see the
        # synced setter for why).
        self._emit("clear", [], [], None, self.last_local_mono_ns, self.last_local_wall_ns)
        self.synced = False  # emits "unsync" if the book was synced

    def _emit(
        self,
        kind: BookEventKind,
        bids: list[RawLevel],
        asks: list[RawLevel],
        exchange_ts_ms: int | None,
        mono_ns: int,
        wall_ns: int,
    ) -> None:
        if self.tap is None:
            return
        try:
            self.tap(self, kind, bids, asks, exchange_ts_ms, mono_ns, wall_ns)
        except Exception:
            # Recording must never take down market-data processing; the
            # recorder counts its own failures, this guard is the backstop.
            logger.exception(
                "book tap failed",
                extra={"venue": self.venue.value, "symbol": self.symbol, "kind": kind},
            )

    # -- views -------------------------------------------------------------

    def age_ms(self, now_mono_ns: int | None = None) -> float:
        if self.last_local_mono_ns == 0:
            return float("inf")
        now = time.monotonic_ns() if now_mono_ns is None else now_mono_ns
        return (now - self.last_local_mono_ns) / 1e6

    def best_bid(self) -> tuple[float, float] | None:
        if not self.bids:
            return None
        p, s = self.bids.peekitem(-1)
        return float(p), float(s)

    def best_ask(self) -> tuple[float, float] | None:
        if not self.asks:
            return None
        p, s = self.asks.peekitem(0)
        return float(p), float(s)

    def mid(self) -> float | None:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        return (bb[0] + ba[0]) / 2.0

    def crossed(self) -> bool:
        """A locally-crossed book (bid >= ask) means our sync is corrupt."""
        bb, ba = self.best_bid(), self.best_ask()
        return bb is not None and ba is not None and bb[0] >= ba[0]

    def iter_bids(self) -> Iterator[tuple[float, float]]:
        """Best (highest) bid first, as floats."""
        for price in reversed(self.bids):
            yield float(price), float(self.bids[price])

    def iter_asks(self) -> Iterator[tuple[float, float]]:
        """Best (lowest) ask first, as floats."""
        for price in self.asks:
            yield float(price), float(self.asks[price])

    def depth(self) -> tuple[int, int]:
        return len(self.bids), len(self.asks)

    def vwap_for_qty(self, side: Literal["bid", "ask"], qty: float) -> float | None:
        """Executable VWAP for taking ``qty`` (base units) from one side.

        side="ask": price paid when BUYING qty (walk asks up).
        side="bid": price received when SELLING qty (walk bids down).
        Returns None when the book cannot fill the full qty — callers must
        treat the clip as unexecutable, not approximate it.
        """
        if qty <= 0:
            raise ValueError("qty must be positive")
        levels = self.iter_asks() if side == "ask" else self.iter_bids()
        remaining = qty
        cost = 0.0
        for price, size in levels:
            take = size if size < remaining else remaining
            cost += take * price
            remaining -= take
            if remaining <= qty * 1e-12:  # float-dust tolerance
                return cost / qty
        return None

    # -- checksums (OKX / Bitget) -------------------------------------------

    def checksum_payload(self, depth: int = _CHECKSUM_DEPTH) -> str:
        """Interleaved ``bidP:bidSz:askP:askSz`` string over the top ``depth``
        levels, using the exchange's literal number formatting. When one side
        has fewer levels, the remaining side's pairs continue uninterleaved —
        this matches both OKX and Bitget checksum specs."""
        bid_prices = list(islice(reversed(self.bids), depth))
        ask_prices = list(islice(iter(self.asks), depth))
        parts: list[str] = []
        for i in range(max(len(bid_prices), len(ask_prices))):
            if i < len(bid_prices):
                p = bid_prices[i]
                parts.append(_plain(p))
                parts.append(_plain(self.bids[p]))
            if i < len(ask_prices):
                p = ask_prices[i]
                parts.append(_plain(p))
                parts.append(_plain(self.asks[p]))
        return ":".join(parts)

    def checksum(self) -> int:
        return crc32_signed(self.checksum_payload())


class BookRegistry:
    """All (venue, symbol) books. Created once at boot; feeds mutate their own
    venue's books, the detector reads all of them (same event loop)."""

    def __init__(self, venues: Iterable[VenueName], symbols: Iterable[str]) -> None:
        self._books: dict[tuple[VenueName, str], OrderBook] = {
            (v, s): OrderBook(v, s) for v in venues for s in symbols
        }
        self.venues = sorted({v for v, _ in self._books}, key=lambda v: v.value)
        self.symbols = sorted({s for _, s in self._books})

    def get(self, venue: VenueName, symbol: str) -> OrderBook:
        return self._books[(venue, symbol)]

    def books_for_symbol(self, symbol: str) -> list[OrderBook]:
        return [b for (v, s), b in self._books.items() if s == symbol]

    def books_for_venue(self, venue: VenueName) -> list[OrderBook]:
        return [b for (v, s), b in self._books.items() if v == venue]

    def all_books(self) -> list[OrderBook]:
        return list(self._books.values())
