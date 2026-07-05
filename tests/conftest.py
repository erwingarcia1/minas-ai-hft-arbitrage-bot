"""Shared test fixtures/helpers."""

from __future__ import annotations

import time

from arb.feeds.book import OrderBook
from arb.models import Settings
from arb.monitoring.metrics import Metrics


def make_settings(
    min_net_edge_bps: float = 3.0,
    clip_sizes_usdt: list[float] | None = None,
    staleness_ms: int = 500,
) -> Settings:
    return Settings.model_validate(
        {
            "symbols": ["BTC/USDT"],
            "signals": {
                "min_net_edge_bps": min_net_edge_bps,
                "clip_sizes_usdt": clip_sizes_usdt or [1000],
            },
            "risk": {
                "staleness_ms": staleness_ms,
                "max_position_usdt": 5000,
                "max_daily_loss_usdt": 200,
            },
            "venues": {
                "binance": {"enabled": True, "fees": {"maker_bps": 10, "taker_bps": 10}},
                "okx": {"enabled": True, "fees": {"maker_bps": 8, "taker_bps": 10}},
                "bybit": {"enabled": False, "fees": {"maker_bps": 10, "taker_bps": 10}},
                "bitget": {"enabled": False, "fees": {"maker_bps": 10, "taker_bps": 10}},
            },
            "monitoring": {
                "metrics_port": 9109,
                "dashboard": False,
                "spread_sample_interval_s": 30,
            },
            "data_dir": "data",
        }
    )


def fill_book(book: OrderBook, bid: float, ask: float, size: float = 100.0) -> None:
    """Fresh, synced, deep book with one level each side."""
    book.apply_snapshot(
        bids=[(str(bid), str(size))],
        asks=[(str(ask), str(size))],
        exchange_ts_ms=None,
        mono_ns=time.monotonic_ns(),
        wall_ns=time.time_ns(),
    )
    book.synced = True


class ListSink:
    """Collects research records for assertions."""

    def __init__(self) -> None:
        self.records: list[dict] = []

    def write(self, record: dict) -> None:
        self.records.append(record)

    def of_type(self, record_type: str) -> list[dict]:
        return [r for r in self.records if r["type"] == record_type]


def make_metrics() -> Metrics:
    return Metrics()  # isolated CollectorRegistry per instance
