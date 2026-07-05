"""Depth-implied slippage: executable VWAP versus top-of-book.

The spread detector prices every clip with ``OrderBook.vwap_for_qty`` (walk
the book), so slippage is *built into* the edge rather than added as a fudge
factor. This module provides the explicit decomposition for logging/analysis.

Failure modes:
- ``executable_vwap`` returns None when the book cannot fill the quantity;
  callers must skip the clip (and close any open opportunity on it).
"""

from __future__ import annotations

from typing import Literal

from arb.feeds.book import OrderBook

BPS = 1e4


def executable_vwap(book: OrderBook, side: Literal["bid", "ask"], qty: float) -> float | None:
    """Thin, intention-revealing wrapper over the book's depth walk."""
    return book.vwap_for_qty(side, qty)


def slippage_bps(book: OrderBook, side: Literal["bid", "ask"], qty: float) -> float | None:
    """How much worse the executable VWAP is than top-of-book, in bps.

    Positive means the clip walks the book. None when depth is insufficient
    or the book is empty.
    """
    vwap = book.vwap_for_qty(side, qty)
    if vwap is None:
        return None
    top = book.best_ask() if side == "ask" else book.best_bid()
    if top is None:
        return None
    top_price = top[0]
    if side == "ask":
        return (vwap / top_price - 1.0) * BPS
    return (1.0 - vwap / top_price) * BPS
