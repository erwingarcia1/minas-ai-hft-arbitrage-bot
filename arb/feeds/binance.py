"""Binance (global) spot diff-depth feed.

Protocol: combined stream ``<sym>@depth@100ms`` (diff updates) + REST depth
snapshot, synchronized with Binance's documented algorithm:

1. Buffer diff events from the socket.
2. GET /api/v3/depth (limit=1000) -> ``lastUpdateId``.
3. Drop buffered events with ``u`` <= lastUpdateId.
4. The first surviving event must satisfy ``U`` <= lastUpdateId+1 <= ``u``;
   otherwise the snapshot is stale relative to the buffer — refetch.
5. After sync, each event must chain: ``U`` == previous ``u`` + 1 (we accept
   the documented overlap form ``U <= last_u+1 <= u``); a gap forces resync.

Heartbeat: Binance sends protocol-level ping frames; the websockets library
answers automatically, so no app-level heartbeat is needed. Binance also
closes every connection at 24h — the base-class reconnect loop handles it.

Failure modes:
- REST snapshot failures (network errors, malformed bodies, stale snapshots,
  holes in the buffered stream) all retry INSIDE the sync task with a throttle;
  the task only exits on success or cancellation, and the book stays
  ``synced=False`` (invisible to signals) throughout.
- Buffer overflow (>10k events while unsynced) forces a fresh resync rather
  than unbounded memory growth.
- Sequence gap (live or mid-buffer) -> resync via new snapshot; counted in
  arb_book_resyncs_total.
- DELIBERATE EXCEPTION to the "no environment reads" rule: the REST snapshot
  session uses ``trust_env=True`` so operators behind an HTTP(S) proxy can
  run the observer. aiohttp will then also consult ``~/.netrc`` for
  api.binance.com credentials if such an entry exists — do not keep exchange
  credentials in ``.netrc``. No other code path reads the environment.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, ClassVar

import aiohttp
import orjson
from pydantic import BaseModel, ConfigDict
from websockets.asyncio.client import ClientConnection

from arb.feeds.base import WsFeed
from arb.models import VenueName

_WS_BASE = "wss://stream.binance.com:9443/stream"
_REST_DEPTH_URL = "https://api.binance.com/api/v3/depth"
_SNAPSHOT_LIMIT = 1000
_MAX_BUFFER = 10_000
_SNAPSHOT_RETRY_S = 1.0


class DepthEvent(BaseModel):
    """Payload of a ``depthUpdate`` stream event."""

    model_config = ConfigDict(extra="ignore")

    e: str
    E: int  # event time (ms)
    s: str  # venue symbol, e.g. BTCUSDT
    U: int  # first update id in event
    u: int  # final update id in event
    b: list[tuple[str, str]]
    a: list[tuple[str, str]]


class _SymbolSync:
    """Per-symbol sync state for the snapshot+diff algorithm."""

    __slots__ = ("synced", "last_u", "buffer", "task")

    def __init__(self) -> None:
        self.synced: bool = False
        self.last_u: int = 0
        self.buffer: list[DepthEvent] = []
        self.task: asyncio.Task[None] | None = None


def venue_symbol(symbol: str) -> str:
    return symbol.replace("/", "")


class BinanceFeed(WsFeed):
    venue: ClassVar[VenueName] = VenueName.BINANCE
    HEARTBEAT_INTERVAL_S: ClassVar[float | None] = None  # protocol pings only
    IDLE_TIMEOUT_S: ClassVar[float] = 30.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._sync: dict[str, _SymbolSync] = {s: _SymbolSync() for s in self.symbols}
        self._by_venue_symbol: dict[str, str] = {venue_symbol(s): s for s in self.symbols}

    def ws_url(self) -> str:
        streams = "/".join(f"{venue_symbol(s).lower()}@depth@100ms" for s in self.symbols)
        return f"{_WS_BASE}?streams={streams}"

    async def on_connected(self, ws: ClientConnection) -> None:
        # Subscription is embedded in the combined-stream URL; just (re)start
        # the snapshot sync for every symbol.
        for symbol in self.symbols:
            self._start_resync(symbol, reason="connect")

    def on_disconnected(self) -> None:
        super().on_disconnected()
        for state in self._sync.values():
            state.synced = False
            state.buffer.clear()
            if state.task is not None:
                state.task.cancel()
                state.task = None

    async def handle_raw(self, raw: str | bytes, wall_ns: int, mono_ns: int) -> None:
        obj = orjson.loads(raw)
        data = obj.get("data")
        if not isinstance(data, dict) or data.get("e") != "depthUpdate":
            # Combined streams carry only subscribed events; anything else
            # (e.g. subscription responses if ever used) is logged and skipped.
            self.log.debug("ignoring non-depth frame", extra={"venue": self.venue.value})
            return
        event = DepthEvent.model_validate(data)
        symbol = self._by_venue_symbol.get(event.s)
        if symbol is None:
            self.log.warning(
                "depth event for unknown symbol %s", event.s, extra={"venue": self.venue.value}
            )
            return

        state = self._sync[symbol]
        if not state.synced:
            state.buffer.append(event)
            if len(state.buffer) > _MAX_BUFFER:
                self.note_resync(symbol, "buffer_overflow")
                self._start_resync(symbol, reason="buffer_overflow")
            return

        book = self.registry.get(self.venue, symbol)
        if event.u <= state.last_u:
            return  # stale duplicate
        if event.U <= state.last_u + 1:
            book.apply_deltas(
                event.b, event.a, exchange_ts_ms=event.E, mono_ns=mono_ns, wall_ns=wall_ns
            )
            state.last_u = event.u
            self.record_feed_latency(event.E, wall_ns)
            self.on_update(self.venue, symbol)  # base class shields this call
        else:
            self.note_resync(symbol, "sequence_gap")
            self._start_resync(symbol, reason="sequence_gap")

    # -- snapshot sync ---------------------------------------------------------

    def _start_resync(self, symbol: str, *, reason: str) -> None:
        state = self._sync[symbol]
        state.synced = False
        state.buffer.clear()
        book = self.registry.get(self.venue, symbol)
        book.synced = False
        if state.task is not None and not state.task.done():
            return  # a sync is already in flight; buffered events feed into it
        state.task = asyncio.create_task(
            self._sync_symbol(symbol), name=f"binance-sync-{symbol}"
        )

    async def _sync_symbol(self, symbol: str) -> None:
        """Snapshot+replay sync loop. This task NEVER exits on failure — every
        error path (fetch failure, malformed snapshot, stale snapshot, gap in
        the buffered stream) sleeps and retries inside the same task, so the
        not-done guard in ``_start_resync`` is always accurate. The only exits
        are success or cancellation."""
        state = self._sync[symbol]
        book = self.registry.get(self.venue, symbol)
        while True:
            try:
                snapshot = await self._fetch_snapshot(symbol)
                last_id = int(snapshot["lastUpdateId"])
                pending = [ev for ev in state.buffer if ev.u > last_id]
                if pending and pending[0].U > last_id + 1:
                    # Snapshot predates the buffered stream — events between
                    # snapshot and buffer head were lost; take a newer snapshot
                    # (throttled: depth?limit=1000 carries heavy request weight
                    # and a tight loop here would invite a 429/418 IP ban).
                    self.log.info(
                        "snapshot too old for %s (U=%d > lastUpdateId+1=%d), refetching",
                        symbol,
                        pending[0].U,
                        last_id + 1,
                        extra={"venue": self.venue.value, "symbol": symbol},
                    )
                    await asyncio.sleep(_SNAPSHOT_RETRY_S)
                    continue

                mono_ns = time.monotonic_ns()
                wall_ns = time.time_ns()
                book.apply_snapshot(
                    snapshot["bids"],
                    snapshot["asks"],
                    exchange_ts_ms=None,
                    mono_ns=mono_ns,
                    wall_ns=wall_ns,
                )
                # Replay the buffer with the SAME continuity rule as live
                # events: a hole mid-buffer (e.g. one frame was dropped by the
                # per-message error handler) must force a fresh snapshot, or
                # levels changed inside the hole survive as ghosts in a book
                # we would then wrongly mark synced.
                gap = False
                for ev in pending:
                    if ev.U > last_id + 1:
                        gap = True
                        break
                    book.apply_deltas(
                        ev.b, ev.a, exchange_ts_ms=ev.E, mono_ns=mono_ns, wall_ns=wall_ns
                    )
                    last_id = ev.u
                if gap:
                    self.note_resync(symbol, "buffer_gap")
                    await asyncio.sleep(_SNAPSHOT_RETRY_S)
                    continue

                state.last_u = last_id
                state.buffer.clear()
                state.synced = True
                book.synced = True
                self.log.info(
                    "book synced for %s (depth %s)",
                    symbol,
                    book.depth(),
                    extra={"venue": self.venue.value, "symbol": symbol},
                )
                self._safe_on_update(symbol)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.warning(
                    "snapshot sync attempt failed for %s: %r — retrying",
                    symbol,
                    exc,
                    exc_info=True,
                    extra={"venue": self.venue.value, "symbol": symbol},
                )
                await asyncio.sleep(_SNAPSHOT_RETRY_S)

    def _safe_on_update(self, symbol: str) -> None:
        """The detector callback must not be able to unwind sync state; the
        base class shields handle_raw the same way."""
        try:
            self.on_update(self.venue, symbol)
        except Exception:
            self.log.exception(
                "on_update callback failed",
                extra={"venue": self.venue.value, "symbol": symbol},
            )
            self.metrics.message_errors.labels(venue=self.venue.value).inc()

    async def _fetch_snapshot(self, symbol: str) -> dict[str, Any]:
        params = {"symbol": venue_symbol(symbol), "limit": str(_SNAPSHOT_LIMIT)}
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(_REST_DEPTH_URL, params=params) as resp:
                resp.raise_for_status()
                payload = await resp.read()
        parsed: dict[str, Any] = orjson.loads(payload)
        return parsed
