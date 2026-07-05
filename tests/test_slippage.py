"""Depth-implied slippage decomposition."""

from __future__ import annotations

import time

import pytest

from arb.feeds.book import OrderBook
from arb.models import VenueName
from arb.signals.slippage import executable_vwap, slippage_bps


def make_book() -> OrderBook:
    book = OrderBook(VenueName.BINANCE, "BTC/USDT")
    book.apply_snapshot(
        bids=[("100.0", "1"), ("99.0", "1")],
        asks=[("100.5", "1"), ("101.5", "1")],
        exchange_ts_ms=None,
        mono_ns=time.monotonic_ns(),
        wall_ns=time.time_ns(),
    )
    return book


def test_slippage_buy_side() -> None:
    book = make_book()
    # VWAP for 2 base = 101.0 vs best ask 100.5 -> (101/100.5 - 1) * 1e4
    assert executable_vwap(book, "ask", 2.0) == pytest.approx(101.0)
    assert slippage_bps(book, "ask", 2.0) == pytest.approx((101.0 / 100.5 - 1) * 1e4)


def test_slippage_sell_side() -> None:
    book = make_book()
    # VWAP for 2 base = 99.5 vs best bid 100.0 -> (1 - 99.5/100) * 1e4 = 50bps
    assert executable_vwap(book, "bid", 2.0) == pytest.approx(99.5)
    assert slippage_bps(book, "bid", 2.0) == pytest.approx(50.0)


def test_no_slippage_within_top_level() -> None:
    book = make_book()
    assert slippage_bps(book, "ask", 0.5) == pytest.approx(0.0)
    assert slippage_bps(book, "bid", 1.0) == pytest.approx(0.0)


def test_insufficient_depth_is_none_not_approximation() -> None:
    book = make_book()
    assert executable_vwap(book, "ask", 10.0) is None
    assert slippage_bps(book, "ask", 10.0) is None
