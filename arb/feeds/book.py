"""Local L2 order-book engine shared by all venue adapters.

Prices and sizes are stored as ``Decimal`` keyed exactly as the exchange sent
them: OKX/Bitget CRC32 checksums are computed over the *literal* price/size
strings (trailing zeros included), and ``Decimal`` round-trips those strings
faithfully where ``float`` formatting would not.

Signal math reads float views (``iter_bids``/``iter_asks``/``vwap_for_qty``);
float64 precision is far beyond bps-level spread math.

Failure modes:
- A book is only trustworthy when ``synced`` is True AND ``age_ms()`` is small;
  consumers must check both. ``synced`` is cleared by the owning feed on
  disconnect, sequence gap, or checksum mismatch.
- ``vwap_for_qty`` returns None when the book lacks depth for the clip —
  callers must treat that as "no executable price", never fall back to
  top-of-book.
- Exchanges send plain decimal strings; scientific notation would break
  checksum reconstruction and is asserted against in tests.
"""

from __future__ import annotations

import time
import zlib
from decimal import Decimal
from itertools import islice
from typing import Iterable, Iterator, Literal

from sortedcontainers import SortedDict

from arb.models import VenueName

RawLevel = tuple[str, str]  # (price, size) exactly as received

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
        "synced",
        "last_local_mono_ns",
        "last_local_wall_ns",
        "last_exchange_ts_ms",
    )

    def __init__(self, venue: VenueName, symbol: str) -> None:
        self.venue = venue
        self.symbol = symbol
        self.bids: SortedDict = SortedDict()  # Decimal price -> Decimal size (asc)
        self.asks: SortedDict = SortedDict()
        self.synced: bool = False
        self.last_local_mono_ns: int = 0
        self.last_local_wall_ns: int = 0
        self.last_exchange_ts_ms: int | None = None

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
        self.bids.clear()
        self.asks.clear()
        self.apply_deltas(bids, asks, exchange_ts_ms=exchange_ts_ms, mono_ns=mono_ns, wall_ns=wall_ns)

    def apply_deltas(
        self,
        bids: Iterable[RawLevel],
        asks: Iterable[RawLevel],
        *,
        exchange_ts_ms: int | None,
        mono_ns: int,
        wall_ns: int,
    ) -> None:
        for price_s, size_s in bids:
            self._set_level(self.bids, price_s, size_s)
        for price_s, size_s in asks:
            self._set_level(self.asks, price_s, size_s)
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
        self.synced = False

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
