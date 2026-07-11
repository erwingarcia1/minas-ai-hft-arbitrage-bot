"""OKX (global) spot order-book feed (``books`` channel, 400 levels).

Protocol: subscribe to ``{"channel":"books","instId":"BTC-USDT"}``; OKX pushes
``action:"snapshot"`` then ``action:"update"`` messages. Every message carries
``seqId``/``prevSeqId``: an update chains iff its ``prevSeqId`` equals the
``seqId`` we last applied (heartbeat pushes repeat prevSeqId == seqId and chain
naturally); anything else — including sequence resets after OKX maintenance,
which OKX resolves by re-sending a snapshot — forces a resubscribe.

CHECKSUM DEPRECATION (OKX changelog 2026-05-21): the ``checksum`` field is
still present on books pushes but is now FIXED TO 0 and must not be used for
integrity verification; ``seqId``/``prevSeqId`` chaining is the prescribed
mechanism. This adapter therefore verifies the CRC32 only when the field is
present AND non-zero (older/regional gateways), and skips it otherwise — a
genuine CRC of 0 has probability 2^-32, so the skip is harmless either way.

Level format is ``[price, size, "0"(deprecated), numOrders]`` — only the first
two fields participate in the book (and checksum, when verified).

Heartbeat: literal text ``ping`` every 25s; OKX replies ``pong`` and drops
clients silent for 30s.

Failure modes:
- Sequence break (or a non-zero checksum mismatch) resyncs via
  unsubscribe/resubscribe; the book is invisible to signals meanwhile. A
  resubscribe that yields no snapshot within the base-class deadline recycles
  the connection, and more than ``_MAX_CONSECUTIVE_RESYNCS`` back-to-back
  resyncs for one symbol also recycles it — sub/unsub spam against OKX's
  per-connection rate limits must never loop unbounded.
- ``event:"error"`` frames are logged at ERROR with the venue's code/message.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

import orjson
from pydantic import BaseModel, ConfigDict
from websockets.asyncio.client import ClientConnection

from arb.feeds.base import WsFeed
from arb.models import VenueName

_WS_URL = "wss://ws.okx.com:8443/ws/v5/public"
_MAX_CONSECUTIVE_RESYNCS = 5


class BookData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    asks: list[list[str]]  # [price, size, "0", numOrders]
    bids: list[list[str]]
    ts: str
    seqId: int
    prevSeqId: int | None = None
    checksum: int | None = None


class BookMsg(BaseModel):
    model_config = ConfigDict(extra="ignore")

    arg: dict[str, str]
    action: Literal["snapshot", "update"]
    data: list[BookData]


def venue_symbol(symbol: str) -> str:
    return symbol.replace("/", "-")


def _levels(raw: list[list[str]]) -> list[tuple[str, str]]:
    return [(lvl[0], lvl[1]) for lvl in raw]


class OkxFeed(WsFeed):
    venue: ClassVar[VenueName] = VenueName.OKX
    HEARTBEAT_INTERVAL_S: ClassVar[float | None] = 25.0
    IDLE_TIMEOUT_S: ClassVar[float] = 45.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._seq: dict[str, int] = {}
        self._consecutive_resyncs: dict[str, int] = {}
        self._by_inst: dict[str, str] = {venue_symbol(s): s for s in self.symbols}

    def ws_url(self) -> str:
        return _WS_URL

    def heartbeat_payload(self) -> str:
        return "ping"

    async def on_connected(self, ws: ClientConnection) -> None:
        self._seq.clear()
        args = [{"channel": "books", "instId": venue_symbol(s)} for s in self.symbols]
        await ws.send(orjson.dumps({"op": "subscribe", "args": args}).decode())

    def on_disconnected(self) -> None:
        super().on_disconnected()
        self._seq.clear()
        self._consecutive_resyncs.clear()

    async def handle_raw(self, raw: str | bytes, wall_ns: int, mono_ns: int) -> None:
        if raw == "pong" or raw == b"pong":
            return
        obj = orjson.loads(raw)
        event = obj.get("event")
        if event is not None:
            if event == "error":
                self.log.error("okx error frame: %s", obj, extra={"venue": self.venue.value})
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
                _levels(data.bids),
                _levels(data.asks),
                exchange_ts_ms=exchange_ts,
                mono_ns=mono_ns,
                wall_ns=wall_ns,
            )
            self._seq[symbol] = data.seqId
        else:
            last_seq = self._seq.get(symbol)
            if last_seq is None:
                return  # update before snapshot (resubscribe race): wait
            if data.prevSeqId != last_seq:
                await self._resync(symbol, "sequence_gap")
                return
            book.apply_deltas(
                _levels(data.bids),
                _levels(data.asks),
                exchange_ts_ms=exchange_ts,
                mono_ns=mono_ns,
                wall_ns=wall_ns,
            )
            self._seq[symbol] = data.seqId

        # Deprecated by OKX on 2026-05-21: the field is now fixed to 0.
        # Verify only a non-zero checksum (older/regional gateways).
        # Integrity is verified BEFORE the book is marked synced: state that
        # has not passed its checksum must never be journaled as trusted.
        if data.checksum and book.checksum() != data.checksum:
            await self._resync(symbol, "checksum_mismatch")
            return

        if action == "snapshot":
            book.synced = True
            self.clear_resync_deadline(symbol)
        if action == "update":
            # Only a clean UPDATE ends a resync streak: the snapshot right
            # after a resubscribe also applies cleanly, and counting it would
            # defeat the storm guard in a snapshot->mismatch->resubscribe loop.
            self._consecutive_resyncs.pop(symbol, None)
        self.record_feed_latency(exchange_ts, wall_ns)
        self.on_update(self.venue, symbol)

    async def _resync(self, symbol: str, reason: str) -> None:
        """Resubscribe one instId, with a storm guard: repeated back-to-back
        resyncs mean the in-band path is not converging (e.g. a systematically
        wrong integrity signal), so recycle the whole connection instead of
        spamming OKX's per-connection sub/unsub rate limit."""
        self.note_resync(symbol, reason)
        book = self.registry.get(self.venue, symbol)
        book.synced = False
        self._seq.pop(symbol, None)
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
        arg = {"channel": "books", "instId": venue_symbol(symbol)}
        await self._ws.send(orjson.dumps({"op": "unsubscribe", "args": [arg]}).decode())
        await self._ws.send(orjson.dumps({"op": "subscribe", "args": [arg]}).decode())
