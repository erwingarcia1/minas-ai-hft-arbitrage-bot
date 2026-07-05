"""Adapter sync logic: Binance snapshot+diff algorithm, Bybit sequencing,
OKX seqId chaining and deprecated-checksum handling.

These run the real ``handle_raw`` paths with canned frames; only network I/O
(REST snapshot, websocket sends) is faked.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

import orjson
import pytest

import arb.feeds.binance as binance_mod
from arb.feeds.binance import BinanceFeed
from arb.feeds.book import BookRegistry
from arb.feeds.bybit import BybitFeed
from arb.feeds.okx import OkxFeed
from arb.models import VenueName

from tests.conftest import make_metrics

BTC = "BTC/USDT"


class FakeWs:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


def now_ns() -> tuple[int, int]:
    return time.time_ns(), time.monotonic_ns()


# ---------------------------------------------------------------------------
# Binance
# ---------------------------------------------------------------------------


def binance_frame(U: int, u: int, bids: list, asks: list) -> bytes:
    return orjson.dumps(
        {
            "stream": "btcusdt@depth@100ms",
            "data": {
                "e": "depthUpdate",
                "E": 1_700_000_000_000,
                "s": "BTCUSDT",
                "U": U,
                "u": u,
                "b": bids,
                "a": asks,
            },
        }
    )


def make_binance(
    snapshot: dict[str, Any] | list[Any],
) -> tuple[BinanceFeed, BookRegistry, list]:
    """``snapshot`` may be a single canned REST response or a sequence of
    per-call responses; an Exception instance in the sequence is raised."""
    registry = BookRegistry([VenueName.BINANCE], [BTC])
    updates: list = []
    feed = BinanceFeed([BTC], registry, lambda v, s: updates.append((v, s)), make_metrics())
    responses = snapshot if isinstance(snapshot, list) else [snapshot]
    call_count = 0

    async def fake_fetch(symbol: str) -> dict[str, Any]:
        nonlocal call_count
        response = responses[min(call_count, len(responses) - 1)]
        call_count += 1
        if isinstance(response, Exception):
            raise response
        return response

    feed._fetch_snapshot = fake_fetch  # type: ignore[method-assign]
    return feed, registry, updates


async def test_binance_snapshot_sync_drops_stale_and_applies_overlap() -> None:
    snapshot = {
        "lastUpdateId": 100,
        "bids": [["100.0", "1"]],
        "asks": [["100.5", "1"]],
    }
    feed, registry, updates = make_binance(snapshot)
    book = registry.get(VenueName.BINANCE, BTC)

    wall, mono = now_ns()
    # Pre-sync events buffer: first is entirely <= lastUpdateId (dropped),
    # second overlaps (U=101 <= lastUpdateId+1 <= u=105) and must apply.
    await feed.handle_raw(binance_frame(99, 100, [["99.0", "9"]], []), wall, mono)
    await feed.handle_raw(binance_frame(101, 105, [["100.0", "2"]], []), wall, mono)
    assert not book.synced

    await feed._sync_symbol(BTC)
    assert book.synced
    assert book.best_bid() == (100.0, 2.0)  # overlap event overwrote snapshot level
    assert feed._sync[BTC].last_u == 105
    assert ("99.0" not in {str(p) for p in book.bids})  # dropped event never applied


async def test_binance_applies_chain_and_ignores_duplicates() -> None:
    snapshot = {"lastUpdateId": 100, "bids": [["100.0", "1"]], "asks": [["100.5", "1"]]}
    feed, registry, _ = make_binance(snapshot)
    book = registry.get(VenueName.BINANCE, BTC)
    await feed._sync_symbol(BTC)

    wall, mono = now_ns()
    await feed.handle_raw(binance_frame(101, 102, [["99.5", "3"]], []), wall, mono)
    assert book.bids.get(Decimal("99.5")) is not None
    assert feed._sync[BTC].last_u == 102

    # Stale duplicate: u <= last_u -> ignored entirely.
    await feed.handle_raw(binance_frame(101, 102, [["99.5", "7"]], []), wall, mono)
    assert float(book.bids[Decimal("99.5")]) == 3.0


async def test_binance_gap_forces_resync() -> None:
    snapshot = {"lastUpdateId": 100, "bids": [["100.0", "1"]], "asks": [["100.5", "1"]]}
    feed, registry, _ = make_binance(snapshot)
    book = registry.get(VenueName.BINANCE, BTC)
    await feed._sync_symbol(BTC)
    assert book.synced

    wall, mono = now_ns()
    # Gap: U=120 > last_u+1=101 -> resync starts, book leaves the usable set.
    await feed.handle_raw(binance_frame(120, 121, [["98.0", "1"]], []), wall, mono)
    assert not book.synced
    # The spawned resync task completes against the fake snapshot.
    for _ in range(20):
        await asyncio.sleep(0.01)
        if book.synced:
            break
    assert book.synced
    assert feed._sync[BTC].last_u == 100


async def test_binance_buffer_gap_forces_fresh_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hole INSIDE the buffered stream must refetch, never mark synced —
    ghost levels from inside the hole would otherwise survive in a book the
    detector trusts."""
    monkeypatch.setattr(binance_mod, "_SNAPSHOT_RETRY_S", 0.01)
    snap1 = {"lastUpdateId": 100, "bids": [["100.0", "1"]], "asks": [["100.5", "1"]]}
    snap2 = {"lastUpdateId": 200, "bids": [["200.0", "1"]], "asks": [["200.5", "1"]]}
    feed, registry, _ = make_binance([snap1, snap2])
    book = registry.get(VenueName.BINANCE, BTC)
    resyncs: list[str] = []
    monkeypatch.setattr(feed, "note_resync", lambda sym, reason: resyncs.append(reason))

    wall, mono = now_ns()
    # Buffer: 101..105 chains onto snapshot 1, but 110..112 leaves a hole
    # (106..109 missing, e.g. a frame the error handler dropped).
    await feed.handle_raw(binance_frame(101, 105, [["100.0", "2"]], []), wall, mono)
    await feed.handle_raw(binance_frame(110, 112, [["99.0", "1"]], []), wall, mono)

    await feed._sync_symbol(BTC)
    assert "buffer_gap" in resyncs
    assert book.synced
    # Synced state must come from the SECOND snapshot, not the hole-y replay.
    assert book.best_bid() == (200.0, 1.0)
    assert feed._sync[BTC].last_u == 200


async def test_binance_sync_retries_inside_task_on_malformed_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed REST body (or any unexpected error) must retry within the
    same sync task — the old code path scheduled a no-op resync and left the
    symbol dark."""
    monkeypatch.setattr(binance_mod, "_SNAPSHOT_RETRY_S", 0.01)
    good = {"lastUpdateId": 100, "bids": [["100.0", "1"]], "asks": [["100.5", "1"]]}
    feed, registry, _ = make_binance([{"unexpected": "shape"}, RuntimeError("boom"), good])
    book = registry.get(VenueName.BINANCE, BTC)

    await asyncio.wait_for(feed._sync_symbol(BTC), timeout=2.0)
    assert book.synced
    assert book.best_bid() == (100.0, 1.0)


# ---------------------------------------------------------------------------
# Bybit
# ---------------------------------------------------------------------------


def bybit_frame(msg_type: str, u: int, bids: list, asks: list) -> bytes:
    return orjson.dumps(
        {
            "topic": "orderbook.50.BTCUSDT",
            "type": msg_type,
            "ts": 1_700_000_000_000,
            "cts": 1_700_000_000_000,
            "data": {"s": "BTCUSDT", "b": bids, "a": asks, "u": u, "seq": 1},
        }
    )


def make_bybit() -> tuple[BybitFeed, BookRegistry]:
    registry = BookRegistry([VenueName.BYBIT], [BTC])
    feed = BybitFeed([BTC], registry, lambda v, s: None, make_metrics())
    feed._ws = FakeWs()  # type: ignore[assignment]
    return feed, registry


async def test_bybit_snapshot_then_chained_deltas() -> None:
    feed, registry = make_bybit()
    book = registry.get(VenueName.BYBIT, BTC)
    wall, mono = now_ns()

    await feed.handle_raw(bybit_frame("snapshot", 50, [["100.0", "1"]], [["100.5", "1"]]), wall, mono)
    assert book.synced
    await feed.handle_raw(bybit_frame("delta", 51, [["100.0", "4"]], []), wall, mono)
    assert book.best_bid() == (100.0, 4.0)
    # Stale duplicate dropped.
    await feed.handle_raw(bybit_frame("delta", 51, [["100.0", "9"]], []), wall, mono)
    assert book.best_bid() == (100.0, 4.0)


async def test_bybit_gap_resubscribes() -> None:
    feed, registry = make_bybit()
    book = registry.get(VenueName.BYBIT, BTC)
    wall, mono = now_ns()

    await feed.handle_raw(bybit_frame("snapshot", 50, [["100.0", "1"]], [["100.5", "1"]]), wall, mono)
    await feed.handle_raw(bybit_frame("delta", 53, [["100.0", "9"]], []), wall, mono)  # gap
    assert not book.synced
    sent = feed._ws.sent  # type: ignore[union-attr]
    assert any("unsubscribe" in m for m in sent)
    assert sum("subscribe" in m for m in sent) >= 2  # unsubscribe + subscribe
    # The gapped delta must NOT have been applied.
    assert book.best_bid() == (100.0, 1.0)


async def test_bybit_u1_is_service_restart_snapshot() -> None:
    feed, registry = make_bybit()
    book = registry.get(VenueName.BYBIT, BTC)
    wall, mono = now_ns()

    await feed.handle_raw(bybit_frame("snapshot", 50, [["100.0", "1"]], [["100.5", "1"]]), wall, mono)
    # Service restart: delta with u=1 is a snapshot and must rebuild the book.
    await feed.handle_raw(bybit_frame("delta", 1, [["90.0", "2"]], [["91.0", "2"]]), wall, mono)
    assert book.best_bid() == (90.0, 2.0)
    assert book.depth() == (1, 1)
    assert book.synced


async def test_bybit_delta_before_snapshot_waits() -> None:
    feed, registry = make_bybit()
    book = registry.get(VenueName.BYBIT, BTC)
    wall, mono = now_ns()
    await feed.handle_raw(bybit_frame("delta", 42, [["100.0", "1"]], []), wall, mono)
    assert not book.synced
    assert book.depth() == (0, 0)


async def test_bybit_subscribe_chunks_at_ten_args() -> None:
    # Bybit spot rejects subscribe requests with >10 args; an oversized
    # request silently darkens the whole venue.
    symbols = [f"C{i}/USDT" for i in range(12)]
    registry = BookRegistry([VenueName.BYBIT], symbols)
    feed = BybitFeed(symbols, registry, lambda v, s: None, make_metrics())
    ws = FakeWs()
    await feed.on_connected(ws)  # type: ignore[arg-type]
    subs = [orjson.loads(m) for m in ws.sent if orjson.loads(m)["op"] == "subscribe"]
    assert len(subs) == 2
    assert [len(s["args"]) for s in subs] == [10, 2]


async def test_bybit_resubscribe_arms_deadline_and_snapshot_clears_it() -> None:
    feed, registry = make_bybit()
    wall, mono = now_ns()
    await feed.handle_raw(bybit_frame("snapshot", 50, [["100.0", "1"]], [["100.5", "1"]]), wall, mono)
    await feed.handle_raw(bybit_frame("delta", 53, [["100.0", "9"]], []), wall, mono)  # gap
    assert feed.expired_resync(time.monotonic() + feed.RESYNC_DEADLINE_S + 1) == BTC
    # Fresh snapshot (Bybit's response to the resubscribe) disarms it.
    await feed.handle_raw(bybit_frame("snapshot", 60, [["100.0", "1"]], [["100.5", "1"]]), wall, mono)
    assert feed.expired_resync(time.monotonic() + feed.RESYNC_DEADLINE_S + 1) is None


# ---------------------------------------------------------------------------
# OKX
# ---------------------------------------------------------------------------


def okx_frame(
    action: str,
    seq_id: int,
    prev_seq_id: int,
    bids: list,
    asks: list,
    checksum: int | None,
) -> bytes:
    return orjson.dumps(
        {
            "arg": {"channel": "books", "instId": "BTC-USDT"},
            "action": action,
            "data": [
                {
                    "bids": bids,
                    "asks": asks,
                    "ts": "1700000000000",
                    "seqId": seq_id,
                    "prevSeqId": prev_seq_id,
                    "checksum": checksum,
                }
            ],
        }
    )


def make_okx() -> tuple[OkxFeed, BookRegistry]:
    registry = BookRegistry([VenueName.OKX], [BTC])
    feed = OkxFeed([BTC], registry, lambda v, s: None, make_metrics())
    feed._ws = FakeWs()  # type: ignore[assignment]
    return feed, registry


async def test_okx_snapshot_then_chained_update() -> None:
    feed, registry = make_okx()
    book = registry.get(VenueName.OKX, BTC)
    wall, mono = now_ns()

    # checksum=0: the post-2026-05-21 deprecated value — must NOT be verified.
    await feed.handle_raw(
        okx_frame("snapshot", 10, -1, [["100.0", "1", "0", "1"]], [["100.5", "1", "0", "1"]], 0),
        wall,
        mono,
    )
    assert book.synced
    await feed.handle_raw(
        okx_frame("update", 11, 10, [["100.0", "4", "0", "1"]], [], 0), wall, mono
    )
    assert book.best_bid() == (100.0, 4.0)
    assert book.synced


async def test_okx_sequence_break_resubscribes() -> None:
    feed, registry = make_okx()
    book = registry.get(VenueName.OKX, BTC)
    wall, mono = now_ns()
    await feed.handle_raw(
        okx_frame("snapshot", 10, -1, [["100.0", "1", "0", "1"]], [["100.5", "1", "0", "1"]], 0),
        wall,
        mono,
    )
    # prevSeqId=11 does not chain onto our last applied seqId=10.
    await feed.handle_raw(
        okx_frame("update", 12, 11, [["100.0", "9", "0", "1"]], [], 0), wall, mono
    )
    assert not book.synced
    assert book.best_bid() == (100.0, 1.0)  # broken update not applied
    sent = feed._ws.sent  # type: ignore[union-attr]
    assert any("unsubscribe" in m for m in sent)


async def test_okx_nonzero_checksum_still_verified() -> None:
    feed, registry = make_okx()
    book = registry.get(VenueName.OKX, BTC)
    wall, mono = now_ns()
    # A wrong NON-zero checksum (older gateway) must still trigger a resync.
    await feed.handle_raw(
        okx_frame(
            "snapshot", 10, -1, [["100.0", "1", "0", "1"]], [["100.5", "1", "0", "1"]], 12345
        ),
        wall,
        mono,
    )
    assert not book.synced
    # And a CORRECT non-zero checksum passes.
    good = registry.get(VenueName.OKX, BTC)
    from arb.feeds.book import OrderBook

    scratch = OrderBook(VenueName.OKX, BTC)
    scratch.apply_snapshot(
        bids=[("100.0", "1")], asks=[("100.5", "1")],
        exchange_ts_ms=None, mono_ns=mono, wall_ns=wall,
    )
    await feed.handle_raw(
        okx_frame(
            "snapshot", 11, -1, [["100.0", "1", "0", "1"]], [["100.5", "1", "0", "1"]],
            scratch.checksum(),
        ),
        wall,
        mono,
    )
    assert good.synced


async def test_okx_resync_storm_recycles_connection() -> None:
    feed, registry = make_okx()
    wall, mono = now_ns()
    ws = feed._ws
    # Six consecutive bad-checksum snapshots: the sixth resync must close the
    # socket instead of endlessly spamming unsubscribe/subscribe.
    for seq in range(6):
        await feed.handle_raw(
            okx_frame(
                "snapshot", seq, -1, [["100.0", "1", "0", "1"]], [["100.5", "1", "0", "1"]], 1
            ),
            wall,
            mono,
        )
    assert ws.closed  # type: ignore[union-attr]
