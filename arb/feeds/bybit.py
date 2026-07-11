"""Bybit v5 spot order-book feed (``orderbook.50.<SYMBOL>``).

Protocol: after subscribing, Bybit pushes a ``snapshot`` message, then
``delta`` messages carrying an update id ``u``. Sync rules implemented here:

- ``type == "snapshot"``: rebuild the book, ``last_u = u``.
- ``type == "delta"`` with ``u == 1``: per Bybit docs this is a service
  restart and the message is a snapshot — rebuild.
- ``u == last_u + 1``: apply.
- ``u <= last_u``: stale duplicate, drop.
- otherwise: sequence gap -> resubscribe the topic (Bybit re-sends a
  snapshot on subscribe).

Heartbeat: app-level ``{"op":"ping"}`` every 20s (Bybit disconnects quiet
clients); the reply and all data frames feed the idle watchdog.

Failure modes:
- No checksum exists on this venue, so sequence continuity is the only
  integrity check; any ambiguity is resolved toward resync (safe: the book
  is merely invisible to signals while resyncing).
- A locally-crossed book after apply is treated as corruption -> resync.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

import orjson
from pydantic import BaseModel, ConfigDict
from websockets.asyncio.client import ClientConnection

from arb.feeds.base import WsFeed
from arb.models import VenueName

_WS_URL = "wss://stream.bybit.com/v5/public/spot"
_DEPTH = 50
_MAX_SUB_ARGS = 10  # Bybit v5 spot: at most 10 args per subscribe request


class BookData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    s: str  # venue symbol
    b: list[tuple[str, str]]
    a: list[tuple[str, str]]
    u: int  # update id
    seq: int | None = None


class BookMsg(BaseModel):
    model_config = ConfigDict(extra="ignore")

    topic: str
    type: Literal["snapshot", "delta"]
    ts: int  # gateway timestamp (ms)
    cts: int | None = None  # matching-engine timestamp (ms)
    data: BookData


def venue_symbol(symbol: str) -> str:
    return symbol.replace("/", "")


class BybitFeed(WsFeed):
    venue: ClassVar[VenueName] = VenueName.BYBIT
    HEARTBEAT_INTERVAL_S: ClassVar[float | None] = 20.0
    IDLE_TIMEOUT_S: ClassVar[float] = 45.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._last_u: dict[str, int] = {}
        self._by_topic: dict[str, str] = {
            f"orderbook.{_DEPTH}.{venue_symbol(s)}": s for s in self.symbols
        }

    def ws_url(self) -> str:
        return _WS_URL

    def heartbeat_payload(self) -> str:
        return '{"op":"ping"}'

    async def on_connected(self, ws: ClientConnection) -> None:
        self._last_u.clear()
        topics = list(self._by_topic)
        # Bybit spot rejects subscribe requests with more than 10 args — and a
        # rejected request delivers NO topics, silently darkening the venue.
        for i in range(0, len(topics), _MAX_SUB_ARGS):
            await ws.send(
                orjson.dumps(
                    {"op": "subscribe", "args": topics[i : i + _MAX_SUB_ARGS]}
                ).decode()
            )

    def on_disconnected(self) -> None:
        super().on_disconnected()
        self._last_u.clear()

    async def handle_raw(self, raw: str | bytes, wall_ns: int, mono_ns: int) -> None:
        obj = orjson.loads(raw)
        topic = obj.get("topic")
        if topic is None:
            # Operational frames: subscribe acks, pong. Failure acks matter.
            if obj.get("success") is False:
                self.log.error(
                    "bybit op failed: %s", obj, extra={"venue": self.venue.value}
                )
            return
        symbol = self._by_topic.get(topic)
        if symbol is None:
            return
        msg = BookMsg.model_validate(obj)
        book = self.registry.get(self.venue, symbol)
        data = msg.data
        exchange_ts = msg.cts or msg.ts

        is_snapshot = msg.type == "snapshot" or data.u == 1  # u==1: service restart snapshot
        if is_snapshot:
            book.apply_snapshot(
                data.b, data.a, exchange_ts_ms=exchange_ts, mono_ns=mono_ns, wall_ns=wall_ns
            )
            self._last_u[symbol] = data.u
        else:
            last_u = self._last_u.get(symbol)
            if last_u is None:
                # Delta before any snapshot (e.g. right after resubscribe raced
                # an in-flight delta): wait for the snapshot.
                return
            if data.u <= last_u:
                return
            if data.u != last_u + 1:
                self.note_resync(symbol, "sequence_gap")
                await self._resubscribe(symbol)
                return
            book.apply_deltas(
                data.b, data.a, exchange_ts_ms=exchange_ts, mono_ns=mono_ns, wall_ns=wall_ns
            )
            self._last_u[symbol] = data.u

        # Integrity is verified BEFORE the book is marked synced: a locally-
        # crossed book must never be journaled as trusted.
        if book.crossed():
            self.note_resync(symbol, "crossed_book")
            await self._resubscribe(symbol)
            return

        if is_snapshot:
            book.synced = True
            self.clear_resync_deadline(symbol)
        self.record_feed_latency(exchange_ts, wall_ns)
        self.on_update(self.venue, symbol)

    async def _resubscribe(self, symbol: str) -> None:
        """Drop and re-request one topic; Bybit responds with a fresh snapshot.
        The base-class resync deadline recycles the whole connection if that
        snapshot never arrives (rejected op, lost frame): a symbol must not
        stay silently dark on an otherwise-healthy socket."""
        book = self.registry.get(self.venue, symbol)
        book.synced = False
        self._last_u.pop(symbol, None)
        if self._ws is None:
            return  # disconnected; reconnect path will resubscribe everything
        self.arm_resync_deadline(symbol)
        topic = f"orderbook.{_DEPTH}.{venue_symbol(symbol)}"
        await self._ws.send(orjson.dumps({"op": "unsubscribe", "args": [topic]}).decode())
        await self._ws.send(orjson.dumps({"op": "subscribe", "args": [topic]}).decode())
