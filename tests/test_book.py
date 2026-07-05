"""Order-book engine: snapshot/delta application, views, VWAP walk, checksums."""

from __future__ import annotations

import time

import pytest

from arb.feeds.book import BookRegistry, OrderBook, crc32_signed
from arb.models import VenueName


def make_book() -> OrderBook:
    return OrderBook(VenueName.OKX, "BTC/USDT")


def stamp() -> dict:
    return {"exchange_ts_ms": 1_700_000_000_000, "mono_ns": time.monotonic_ns(), "wall_ns": time.time_ns()}


def test_snapshot_and_best_levels() -> None:
    book = make_book()
    book.apply_snapshot(
        bids=[("100.0", "1"), ("99.5", "2")],
        asks=[("100.5", "3"), ("101.0", "4")],
        **stamp(),
    )
    assert book.best_bid() == (100.0, 1.0)
    assert book.best_ask() == (100.5, 3.0)
    assert book.mid() == pytest.approx(100.25)
    assert not book.crossed()
    assert book.depth() == (2, 2)


def test_delta_updates_and_zero_removes() -> None:
    book = make_book()
    book.apply_snapshot(bids=[("100.0", "1")], asks=[("100.5", "1")], **stamp())
    book.apply_deltas(bids=[("100.0", "5"), ("99.0", "2")], asks=[("100.5", "0")], **stamp())
    assert book.best_bid() == (100.0, 5.0)
    assert book.best_ask() is None
    assert book.depth() == (2, 0)


def test_snapshot_replaces_prior_state() -> None:
    book = make_book()
    book.apply_snapshot(bids=[("100.0", "1")], asks=[("100.5", "1")], **stamp())
    book.apply_snapshot(bids=[("90.0", "1")], asks=[("91.0", "1")], **stamp())
    assert book.best_bid() == (90.0, 1.0)
    assert book.depth() == (1, 1)


def test_crossed_book_detected() -> None:
    book = make_book()
    book.apply_snapshot(bids=[("101.0", "1")], asks=[("100.5", "1")], **stamp())
    assert book.crossed()


def test_vwap_walks_depth() -> None:
    book = make_book()
    book.apply_snapshot(
        bids=[("100.0", "1"), ("99.0", "1")],
        asks=[("100.5", "1"), ("101.5", "1")],
        **stamp(),
    )
    # Buy 2 base: 1 @ 100.5 + 1 @ 101.5 -> 101.0
    assert book.vwap_for_qty("ask", 2.0) == pytest.approx(101.0)
    # Sell 2 base: 1 @ 100.0 + 1 @ 99.0 -> 99.5
    assert book.vwap_for_qty("bid", 2.0) == pytest.approx(99.5)
    # Partial single level
    assert book.vwap_for_qty("ask", 0.5) == pytest.approx(100.5)


def test_vwap_insufficient_depth_returns_none() -> None:
    book = make_book()
    book.apply_snapshot(bids=[("100.0", "1")], asks=[("100.5", "1")], **stamp())
    assert book.vwap_for_qty("ask", 2.0) is None
    assert book.vwap_for_qty("bid", 2.0) is None


def test_vwap_rejects_nonpositive_qty() -> None:
    book = make_book()
    with pytest.raises(ValueError):
        book.vwap_for_qty("ask", 0.0)


def test_age_infinite_before_first_update() -> None:
    assert make_book().age_ms() == float("inf")


def test_crc32_signed_known_vector() -> None:
    # Precomputed with zlib: crc32(b"foo") = 2356372769 -> signed int32
    assert crc32_signed("foo") == -1938594527


def test_checksum_payload_interleaves_and_preserves_formatting() -> None:
    book = make_book()
    book.apply_snapshot(
        bids=[("25000.5", "1.5"), ("24999", "0.8")],
        asks=[("25001", "2"), ("25002.25", "3")],
        **stamp(),
    )
    assert book.checksum_payload() == "25000.5:1.5:25001:2:24999:0.8:25002.25:3"
    # Precomputed signed CRC32 of the payload above.
    assert book.checksum() == -1615711002


def test_checksum_payload_uneven_sides() -> None:
    book = make_book()
    book.apply_snapshot(
        bids=[("100.0", "1")],
        asks=[("100.5", "2"), ("100.6", "3")],
        **stamp(),
    )
    assert book.checksum_payload() == "100.0:1:100.5:2:100.6:3"
    assert book.checksum() == -1330305621


def test_checksum_preserves_trailing_zeros() -> None:
    book = make_book()
    book.apply_snapshot(bids=[("100.10", "0.00500")], asks=[], **stamp())
    assert book.checksum_payload() == "100.10:0.00500"


def test_checksum_never_uses_scientific_notation() -> None:
    # str(Decimal("0.0000001")) is "1E-7"; the payload must stay fixed-point
    # or every checksum for a sub-1e-6-priced asset falsely mismatches.
    book = make_book()
    book.apply_snapshot(
        bids=[("0.0000000359", "1000")],
        asks=[("0.0000000360", "500.00")],
        **stamp(),
    )
    assert book.checksum_payload() == "0.0000000359:1000:0.0000000360:500.00"


def test_checksum_uses_top_25_only() -> None:
    book = make_book()
    bids = [(f"{1000 - i}", "1") for i in range(30)]
    book.apply_snapshot(bids=bids, asks=[], **stamp())
    payload = book.checksum_payload()
    assert payload.count(":") == 25 * 2 - 1  # 25 price:size pairs
    assert payload.startswith("1000:1:999:1")


def test_registry_lookup() -> None:
    reg = BookRegistry([VenueName.BINANCE, VenueName.OKX], ["BTC/USDT", "ETH/USDT"])
    assert len(reg.all_books()) == 4
    assert reg.get(VenueName.OKX, "BTC/USDT").symbol == "BTC/USDT"
    assert {b.venue for b in reg.books_for_symbol("BTC/USDT")} == {
        VenueName.BINANCE,
        VenueName.OKX,
    }
    assert {b.symbol for b in reg.books_for_venue(VenueName.OKX)} == {
        "BTC/USDT",
        "ETH/USDT",
    }
