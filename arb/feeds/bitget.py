"""Bitget v2 spot order-book feed (``books`` channel).

Protocol: subscribe with ``{"instType":"SPOT","channel":"books","instId":...}``;
Bitget pushes ``action:"snapshot"`` then ``action:"update"``. Messages carry a
signed-int32 CRC32 ``checksum`` over the top 25 interleaved ``bid:sz:ask:sz``
levels (same construction as OKX). Bitget's v2 books channel does not expose a
documented per-update sequence chain, so the checksum is the primary integrity
mechanism here — it is verified on EVERY message that carries one, and any
mismatch forces an unsubscribe/resubscribe (Bitget re-sends a snapshot).

Heartbeat: literal text ``ping`` every 30s; Bitget replies ``pong`` and
disconnects clients silent for ~2 minutes.

Failure modes:
- Checksum mismatch -> resubscribe; book invisible to signals meanwhile. A
  resubscribe that yields no snapshot within the base-class deadline recycles
  the connection (a rejected subscribe must not leave the symbol silently
  dark), and more than ``_MAX_CONSECUTIVE_RESYNCS`` back-to-back resyncs for
  one symbol also recycles it rather than spamming sub/unsub requests.
- An update arriving before any snapshot (resubscribe race) is dropped.
- A locally-crossed book after apply is treated as corruption -> resubscribe.
- ``event:"error"`` frames are logged at ERROR with Bitget's code/message.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

import orjson
from pydantic import BaseModel, ConfigDict
from websockets.asyncio.client import ClientConnection

from arb.feeds.base import WsFeed
from arb.models import VenueName

_WS_URL = "wss://ws.bitget.com/v2/ws/public"
_MAX_CONSECUTIVE_RESYNCS = 5


class BookData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    asks: list[tuple[str, str]]
    bids: list[tuple[str, str]]
    ts: str
    checksum: int | None = None


class BookMsg(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: Literal["snapshot", "update"]
    arg: dict[str, str]
    data: list[BookData]


def venue_symbol(symbol: str) -> str:
    return symbol.replace("/", "")


class BitgetFeed(WsFeed):
    venue: ClassVar[VenueName] = VenueName.BITGET
    HEARTBEAT_INTERVAL_S: ClassVar[float | None] = 30.0
    IDLE_TIMEOUT_S: ClassVar[float] = 60.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._have_snapshot: set[str] = set()
        self._consecutive_resyncs: dict[str, int] = {}
        self._by_inst: dict[str, str] = {venue_symbol(s): s for s in self.symbols}

    def ws_url(self) -> str:
        return _WS_URL

    def heartbeat_payload(self) -> str:
        return "ping"

    async def on_connected(self, ws: ClientConnection) -> None:
        self._have_snapshot.clear()
        args = [
            {"instType": "SPOT", "channel": "books", "instId": venue_symbol(s)}
            for s in self.symbols
        ]
        await ws.send(orjson.dumps({"op": "subscribe", "args": args}).decode())

    def on_disconnected(self) -> None:
        super().on_disconnected()
        self._have_snapshot.clear()
        self._consecutive_resyncs.clear()

    async def handle_raw(self, raw: str | bytes, wall_ns: int, mono_ns: int) -> None:
        if raw == "pong" or raw == b"pong":
            return
        obj = orjson.loads(raw)
        event = obj.get("event")
        if event is not None:
            if event == "error":
                self.log.error("bitget error frame: %s", obj, extra={"venue": self.venue.value})
            return
        if obj.get("arg", {}).get("channel") != "books":
            return
        msg = BookMsg.model_validate(obj)
        symbol = self._by_inst.get(msg.arg.get("instId", ""))
        if symbol is None:
            return
        for data in msg.data:
            await self._handle_entry(msg.action, symbol, data, wall_ns, mono_ns)

    async def _handle_entry(
        self, action: str, symbol: str, data: BookData, wall_ns: int, mono_ns: int
    ) -> None:
        book = self.registry.get(self.venue, symbol)
        exchange_ts = int(data.ts)

        if action == "snapshot":
            book.apply_snapshot(
                data.bids, data.asks, exchange_ts_ms=exchange_ts, mono_ns=mono_ns, wall_ns=wall_ns
            )
            self._have_snapshot.add(symbol)
        else:
            if symbol not in self._have_snapshot:
                return  # update before snapshot (resubscribe race): wait
            book.apply_deltas(
                data.bids, data.asks, exchange_ts_ms=exchange_ts, mono_ns=mono_ns, wall_ns=wall_ns
            )

        # Integrity is verified BEFORE the book is marked synced: state that
        # has not passed its checksum must never be journaled as trusted.
        if data.checksum is not None and book.checksum() != data.checksum:
            await self._resync(symbol, "checksum_mismatch")
            return

        if book.crossed():
            await self._resync(symbol, "crossed_book")
            return

        if action == "snapshot":
            book.synced = True
            self.clear_resync_deadline(symbol)
        if action == "update":
            # Only a clean UPDATE ends a resync streak (the snapshot after a
            # resubscribe also applies cleanly; see the storm guard below).
            self._consecutive_resyncs.pop(symbol, None)
        self.record_feed_latency(exchange_ts, wall_ns)
        self.on_update(self.venue, symbol)

    async def _resync(self, symbol: str, reason: str) -> None:
        """Resubscribe one instId, with the same storm guard as OKX: if the
        in-band resync loop is not converging, recycle the connection instead
        of spamming Bitget's per-connection request limits."""
        self.note_resync(symbol, reason)
        book = self.registry.get(self.venue, symbol)
        book.synced = False
        self._have_snapshot.discard(symbol)
        if self._ws is None:
            return
        streak = self._consecutive_resyncs.get(symbol, 0) + 1
        self._consecutive_resyncs[symbol] = streak
        if streak > _MAX_CONSECUTIVE_RESYNCS:
            self.log.warning(
                "%d consecutive resyncs for %s; recycling connection",
                streak,
                symbol,
                extra={"venue": self.venue.value, "symbol": symbol},
            )
            await self._ws.close(code=1000, reason="resync storm")
            return
        self.arm_resync_deadline(symbol)
        arg = {"instType": "SPOT", "channel": "books", "instId": venue_symbol(symbol)}
        await self._ws.send(orjson.dumps({"op": "unsubscribe", "args": [arg]}).decode())
        await self._ws.send(orjson.dumps({"op": "subscribe", "args": [arg]}).decode())
