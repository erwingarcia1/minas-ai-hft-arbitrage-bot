"""Recording: the book tap and the JSONL journal.

What is being protected: replay fidelity rests on the tap seeing EVERY book
state change, in application order, with the literal level strings. If a
mutation path bypasses the tap, replay silently diverges from live — the
worst possible failure mode for a research system (wrong conclusions, no
error).
"""

from __future__ import annotations

import time

import orjson

from arb.feeds.book import OrderBook
from arb.models import VenueName
from arb.recording.recorder import BookEventRecorder, header_record
from tests.conftest import make_metrics, make_settings


def collect_tap(book: OrderBook) -> list[tuple]:
    events: list[tuple] = []
    book.tap = lambda b, kind, bids, asks, ets, mono, wall: events.append(
        (kind, bids, asks, ets, mono, wall)
    )
    return events


class TestBookTap:
    def test_snapshot_emits_one_event_with_literal_levels(self) -> None:
        book = OrderBook(VenueName.BINANCE, "BTC/USDT")
        events = collect_tap(book)
        book.apply_snapshot(
            bids=[("100.50", "1.000")],
            asks=[("100.60", "2")],
            exchange_ts_ms=1234,
            mono_ns=10,
            wall_ns=20,
        )
        assert events == [
            ("snapshot", [("100.50", "1.000")], [("100.60", "2")], 1234, 10, 20)
        ]

    def test_delta_and_removal_emit(self) -> None:
        book = OrderBook(VenueName.BINANCE, "BTC/USDT")
        book.apply_snapshot(
            bids=[("100", "1")], asks=[("101", "1")], exchange_ts_ms=None, mono_ns=1, wall_ns=1
        )
        events = collect_tap(book)
        book.apply_deltas(
            bids=[("100", "0")], asks=[], exchange_ts_ms=5678, mono_ns=30, wall_ns=40
        )
        assert events == [("delta", [("100", "0")], [], 5678, 30, 40)]
        assert book.best_bid() is None  # the removal was applied, not just recorded

    def test_sync_transitions_emit_once(self) -> None:
        book = OrderBook(VenueName.OKX, "BTC/USDT")
        events = collect_tap(book)
        book.synced = True
        book.synced = True  # no-op: no duplicate event
        book.synced = False
        kinds = [e[0] for e in events]
        assert kinds == ["sync", "unsync"]

    def test_clear_emits_clear_then_unsync(self) -> None:
        book = OrderBook(VenueName.BITGET, "BTC/USDT")
        book.apply_snapshot(
            bids=[("1", "1")], asks=[("2", "1")], exchange_ts_ms=None, mono_ns=1, wall_ns=1
        )
        book.synced = True
        events = collect_tap(book)
        book.clear()
        assert [e[0] for e in events] == ["clear", "unsync"]
        assert book.depth() == (0, 0)

    def test_raising_tap_does_not_break_mutation(self) -> None:
        book = OrderBook(VenueName.BYBIT, "BTC/USDT")

        def bad_tap(*args: object) -> None:
            raise RuntimeError("recorder exploded")

        book.tap = bad_tap
        book.apply_snapshot(
            bids=[("100", "1")], asks=[("101", "1")], exchange_ts_ms=None, mono_ns=1, wall_ns=1
        )
        assert book.best_bid() == (100.0, 1.0)


class TestRecorder:
    def _recorder(self, tmp_path, settings=None):
        settings = settings or make_settings()
        metrics = make_metrics()
        rec = BookEventRecorder(
            tmp_path, settings, metrics, wall_ns=1_700_000_000_000_000_000, mono_ns=1_000
        )
        return rec, metrics

    def test_journal_contains_header_events_and_end(self, tmp_path) -> None:
        rec, _ = self._recorder(tmp_path)
        book = OrderBook(VenueName.BINANCE, "BTC/USDT")
        book.tap = rec.on_book_event
        book.apply_snapshot(
            bids=[("100.0", "1.5")],
            asks=[("100.1", "2.5")],
            exchange_ts_ms=999,
            mono_ns=2_000,
            wall_ns=1_700_000_000_000_000_500,
        )
        book.synced = True
        rec.close(wall_ns=1_700_000_001_000_000_000, mono_ns=3_000)

        lines = [orjson.loads(l) for l in rec.path.read_bytes().splitlines()]
        assert lines[0]["type"] == "recording_header"
        assert lines[0]["symbols"] == ["BTC/USDT"]
        assert lines[0]["taker_bps"] == {"binance": 10.0, "okx": 10.0}
        assert lines[1] == {
            "type": "book_event",
            "v": "binance",
            "s": "BTC/USDT",
            "k": "snapshot",
            "ets": 999,
            "mono_ns": 2_000,
            "wall_ns": 1_700_000_000_000_000_500,
            "b": [["100.0", "1.5"]],
            "a": [["100.1", "2.5"]],
        }
        assert lines[2]["k"] == "sync"
        assert lines[-1] == {
            "type": "recording_end",
            "mono_ns": 3_000,
            "wall_ns": 1_700_000_001_000_000_000,
        }

    def test_header_snapshot_of_config(self) -> None:
        settings = make_settings(min_net_edge_bps=7.5, clip_sizes_usdt=[500, 1500])
        header = header_record(settings, wall_ns=10**18, mono_ns=42)
        assert header["signals"] == {
            "min_net_edge_bps": 7.5,
            "clip_sizes_usdt": [500.0, 1500.0],
        }
        assert header["staleness_ms"] == 500
        assert header["mono_ns"] == 42

    def test_disables_after_consecutive_write_failures(self, tmp_path, monkeypatch) -> None:
        rec, metrics = self._recorder(tmp_path)

        def boom(mono_ns: int) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(rec, "_flush", boom)
        # mono deltas > flush interval force a flush attempt on every write
        for i in range(10):
            rec._write({"type": "x"}, mono_ns=10**10 * (i + 2))
        assert rec.enabled is False

    def test_write_failures_never_raise_into_the_feed(self, tmp_path, monkeypatch) -> None:
        rec, _ = self._recorder(tmp_path)
        monkeypatch.setattr(
            rec, "_flush", lambda mono_ns: (_ for _ in ()).throw(OSError("boom"))
        )
        book = OrderBook(VenueName.OKX, "BTC/USDT")
        book.tap = rec.on_book_event
        # Must not raise even though every flush attempt fails.
        book.apply_snapshot(
            bids=[("1", "1")], asks=[("2", "1")], exchange_ts_ms=None,
            mono_ns=10**11, wall_ns=1,
        )
