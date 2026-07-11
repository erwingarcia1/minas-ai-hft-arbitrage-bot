"""Spread detector: opportunity lifecycle, staleness gate, depth handling."""

from __future__ import annotations

import time

import pytest

from arb.feeds.book import BookRegistry
from arb.models import VenueName
from arb.signals.spread import SpreadDetector

from tests.conftest import ListSink, fill_book, make_metrics, make_settings

BTC = "BTC/USDT"


def make_detector(
    settings=None,
) -> tuple[SpreadDetector, BookRegistry, ListSink]:
    settings = settings or make_settings()
    registry = BookRegistry(settings.enabled_venues, settings.symbols)
    sink = ListSink()
    detector = SpreadDetector(registry, settings, sink, make_metrics())
    return detector, registry, sink


def test_opportunity_opens_and_closes_on_edge_decay() -> None:
    detector, registry, sink = make_detector()
    binance = registry.get(VenueName.BINANCE, BTC)
    okx = registry.get(VenueName.OKX, BTC)

    # Buy binance @100.0, sell okx @100.5 -> ~50bps gross, ~30bps net of 10+10.
    fill_book(binance, bid=99.9, ask=100.0)
    fill_book(okx, bid=100.5, ask=100.6)
    detector.evaluate(BTC)
    assert detector.open_count == 1
    assert sink.of_type("opportunity") == []  # still open, nothing recorded yet

    # Edge decays below fees -> close with a full record.
    fill_book(okx, bid=100.0, ask=100.1)
    detector.evaluate(BTC)
    assert detector.open_count == 0
    records = sink.of_type("opportunity")
    assert len(records) == 1
    rec = records[0]
    assert rec["buy_venue"] == "binance"
    assert rec["sell_venue"] == "okx"
    assert rec["close_reason"] == "edge_below_threshold"
    assert rec["net_bps_open"] == pytest.approx(29.87, abs=0.1)
    assert rec["gross_bps_open"] == pytest.approx(50.0, abs=0.1)
    assert rec["max_net_bps"] >= rec["net_bps_open"] - 1e-9
    assert rec["duration_ms"] >= 0
    # Profit on the EXECUTABLE notional: qty at open VWAPs, fees explicit.
    fee = 10.0 / 1e4
    assert rec["est_profit_usdt_open"] == pytest.approx(
        rec["qty_base"]
        * (rec["sell_vwap_open"] * (1 - fee) - rec["buy_vwap_open"] * (1 + fee))
    )


def test_no_opportunity_below_threshold() -> None:
    detector, registry, sink = make_detector()
    # ~22bps gross -> ~2bps net at 10+10 fees: below the 3bps threshold.
    fill_book(registry.get(VenueName.BINANCE, BTC), bid=99.9, ask=100.0)
    fill_book(registry.get(VenueName.OKX, BTC), bid=100.22, ask=100.32)
    detector.evaluate(BTC)
    assert detector.open_count == 0
    assert sink.records == []


def test_stale_book_blocks_and_closes() -> None:
    detector, registry, sink = make_detector()
    binance = registry.get(VenueName.BINANCE, BTC)
    okx = registry.get(VenueName.OKX, BTC)
    fill_book(binance, bid=99.9, ask=100.0)
    fill_book(okx, bid=100.5, ask=100.6)
    detector.evaluate(BTC)
    assert detector.open_count == 1

    # Age the okx book beyond staleness_ms (500ms): edge may still "exist"
    # numerically, but the data cannot be trusted -> close, don't reopen.
    okx.last_local_mono_ns = time.monotonic_ns() - 600_000_000
    detector.evaluate(BTC)
    assert detector.open_count == 0
    assert sink.of_type("opportunity")[0]["close_reason"] == "book_unusable"

    detector.evaluate(BTC)
    assert detector.open_count == 0  # stays closed while stale


def test_unsynced_book_blocks() -> None:
    detector, registry, sink = make_detector()
    binance = registry.get(VenueName.BINANCE, BTC)
    okx = registry.get(VenueName.OKX, BTC)
    fill_book(binance, bid=99.9, ask=100.0)
    fill_book(okx, bid=100.5, ask=100.6)
    okx.synced = False  # e.g. mid-resync after a sequence gap
    detector.evaluate(BTC)
    assert detector.open_count == 0
    assert sink.records == []


def test_venue_unusable_force_closes() -> None:
    detector, registry, sink = make_detector()
    fill_book(registry.get(VenueName.BINANCE, BTC), bid=99.9, ask=100.0)
    fill_book(registry.get(VenueName.OKX, BTC), bid=100.5, ask=100.6)
    detector.evaluate(BTC)
    assert detector.open_count == 1

    detector.on_venue_unusable(VenueName.OKX, "venue_unusable")
    assert detector.open_count == 0
    assert sink.of_type("opportunity")[0]["close_reason"] == "venue_unusable"


def test_insufficient_depth_closes() -> None:
    detector, registry, sink = make_detector()
    binance = registry.get(VenueName.BINANCE, BTC)
    okx = registry.get(VenueName.OKX, BTC)
    fill_book(binance, bid=99.9, ask=100.0)
    fill_book(okx, bid=100.5, ask=100.6)
    detector.evaluate(BTC)
    assert detector.open_count == 1

    # Clip needs ~10 base; shrink binance's ask side to a sliver. The VWAP
    # must become None (no executable price) and the opportunity must close.
    fill_book(binance, bid=99.9, ask=100.0, size=0.001)
    detector.evaluate(BTC)
    assert detector.open_count == 0
    assert sink.of_type("opportunity")[0]["close_reason"] == "insufficient_depth"


def test_close_all_on_shutdown() -> None:
    detector, registry, sink = make_detector()
    fill_book(registry.get(VenueName.BINANCE, BTC), bid=99.9, ask=100.0)
    fill_book(registry.get(VenueName.OKX, BTC), bid=100.5, ask=100.6)
    detector.evaluate(BTC)
    assert detector.open_count == 1
    detector.close_all("shutdown")
    assert detector.open_count == 0
    assert sink.of_type("opportunity")[0]["close_reason"] == "shutdown"


def test_sample_emission_covers_both_directions() -> None:
    detector, registry, sink = make_detector()
    fill_book(registry.get(VenueName.BINANCE, BTC), bid=99.9, ask=100.0)
    fill_book(registry.get(VenueName.OKX, BTC), bid=100.5, ask=100.6)
    detector.evaluate(BTC, emit_sample=True)
    samples = sink.of_type("spread_sample")
    directions = {(s["buy_venue"], s["sell_venue"]) for s in samples}
    assert directions == {("binance", "okx"), ("okx", "binance")}
    # Samples record negative edges too — that's the point of the dataset.
    assert any(s["net_bps"] < 0 for s in samples)
    assert any(s["net_bps"] > 0 for s in samples)


def test_slippage_is_priced_into_edge() -> None:
    # Same top-of-book, but the sell side is layered so the clip walks down
    # the book: net edge must be strictly worse than a top-of-book-only view.
    settings = make_settings(clip_sizes_usdt=[2000])
    detector, registry, sink = make_detector(settings)
    binance = registry.get(VenueName.BINANCE, BTC)
    okx = registry.get(VenueName.OKX, BTC)
    fill_book(binance, bid=99.9, ask=100.0, size=1000)
    okx.apply_snapshot(
        bids=[("100.5", "10"), ("100.2", "1000")],  # only 10 base at the touch
        asks=[("100.6", "1000")],
        exchange_ts_ms=None,
        mono_ns=time.monotonic_ns(),
        wall_ns=time.time_ns(),
    )
    okx.synced = True
    detector.evaluate(BTC, emit_sample=True)
    sample = next(
        s
        for s in sink.of_type("spread_sample")
        if s["buy_venue"] == "binance" and s["sell_venue"] == "okx"
    )
    # qty ~= 2000/100.25 ~= 19.95 base; only 10 fill at 100.5, rest at 100.2.
    assert sample["sell_vwap"] < 100.5
    assert sample["sell_vwap"] > 100.2
