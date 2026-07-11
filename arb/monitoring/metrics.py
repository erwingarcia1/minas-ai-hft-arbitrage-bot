"""Prometheus metrics.

All metrics live on an explicit ``CollectorRegistry`` (never the global
default) so tests can instantiate ``Metrics`` freely without duplicated-
timeseries errors.

Failure modes:
- ``start_server`` failing to bind (port in use) raises at boot — fatal by
  design; running without observability is not acceptable for this system.
"""

from __future__ import annotations

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    start_http_server,
)
from prometheus_client.registry import CollectorRegistry

_LATENCY_BUCKETS_MS = (5, 10, 25, 50, 100, 200, 400, 800, 1600, 5000)
_SPREAD_BUCKETS_BPS = (-50, -30, -20, -10, -5, -2, 0, 1, 2, 3, 5, 8, 12, 20, 50)
_DURATION_BUCKETS_MS = (50, 100, 250, 500, 1000, 2500, 5000, 15000, 60000)


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry if registry is not None else CollectorRegistry()
        r = self.registry

        # feeds
        self.ws_messages = Counter(
            "arb_ws_messages_total", "WebSocket frames received", ["venue"], registry=r
        )
        self.ws_reconnects = Counter(
            "arb_ws_reconnects_total", "WebSocket reconnect events", ["venue"], registry=r
        )
        self.message_errors = Counter(
            "arb_message_errors_total", "Frames that failed handling", ["venue"], registry=r
        )
        self.book_resyncs = Counter(
            "arb_book_resyncs_total",
            "Order-book resyncs by cause",
            ["venue", "symbol", "reason"],
            registry=r,
        )
        self.feed_latency_ms = Histogram(
            "arb_feed_latency_ms",
            "Exchange event time to local arrival (ms; includes clock skew)",
            ["venue"],
            buckets=_LATENCY_BUCKETS_MS,
            registry=r,
        )

        # risk / staleness
        self.book_age_ms = Gauge(
            "arb_book_age_ms",
            "Age of last book update (ms; -1 = never updated)",
            ["venue", "symbol"],
            registry=r,
        )
        self.venue_usable = Gauge(
            "arb_venue_usable",
            "1 when all venue books are synced and fresh",
            ["venue"],
            registry=r,
        )

        # signals
        self.spread_gross_bps = Histogram(
            "arb_spread_gross_bps",
            "Gross executable spread per evaluation (bps)",
            ["symbol", "buy_venue", "sell_venue"],
            buckets=_SPREAD_BUCKETS_BPS,
            registry=r,
        )
        self.spread_net_bps = Histogram(
            "arb_spread_net_bps",
            "Net-of-fees executable spread per evaluation (bps)",
            ["symbol", "buy_venue", "sell_venue"],
            buckets=_SPREAD_BUCKETS_BPS,
            registry=r,
        )
        self.opportunities = Counter(
            "arb_opportunities_total",
            "Closed opportunity records",
            ["symbol", "buy_venue", "sell_venue"],
            registry=r,
        )
        self.opportunity_duration_ms = Histogram(
            "arb_opportunity_duration_ms",
            "How long opportunities persisted (ms)",
            buckets=_DURATION_BUCKETS_MS,
            registry=r,
        )

        # accounting
        self.records_written = Counter(
            "arb_records_written_total", "Research records written", registry=r
        )
        self.record_write_errors = Counter(
            "arb_record_write_errors_total", "Research record write failures", registry=r
        )

        # recording (deterministic market-data journal)
        self.recording_events = Counter(
            "arb_recording_events_total",
            "Book events journaled for replay",
            ["venue"],
            registry=r,
        )
        self.recording_errors = Counter(
            "arb_recording_errors_total",
            "Book-event journal write failures",
            registry=r,
        )

    def start_server(self, port: int) -> None:
        """Expose /metrics. Runs in prometheus_client's own daemon thread;
        the scrape path never touches the event loop."""
        start_http_server(port, registry=self.registry)
