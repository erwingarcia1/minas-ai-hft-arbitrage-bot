"""Reconnecting WebSocket feed base class shared by all four venue adapters.

Connection policy:
- Exponential backoff with jitter (0.5s .. 30s cap) on any disconnect/error.
- Per-venue application heartbeat (Bybit/OKX/Bitget require app-level pings;
  Binance uses protocol-level ping frames which the ``websockets`` library
  answers automatically).
- Idle watchdog: if no frame arrives for ``IDLE_TIMEOUT_S`` the socket is
  force-closed so the reconnect loop takes over — a half-open TCP connection
  must never keep stale books looking alive.

Failure modes:
- On ANY exit from the read loop, every book for the venue is marked unsynced
  before reconnecting; consumers therefore see the venue as unusable within
  one event-loop cycle of the disconnect.
- A message that fails parsing/handling is logged (with a payload excerpt)
  and counted in ``arb_message_errors_total``; the connection survives.
- Exceptions in the connect/subscribe path are logged and retried with
  backoff forever — a venue being down must degrade, not crash, the process.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from abc import ABC, abstractmethod
from typing import Callable, ClassVar, Sequence

import websockets
from websockets.asyncio.client import ClientConnection

from arb.feeds.book import BookRegistry
from arb.models import VenueName
from arb.monitoring.metrics import Metrics

# Called as on_update(venue, symbol) after every applied book change.
# REGION-SPLITTING SEAM (Phase 4): this callback is the boundary that would
# become an inter-region bus publish (feeds run venue-local; signals central).
UpdateCallback = Callable[[VenueName, str], None]

_BACKOFF_INITIAL_S = 0.5
_BACKOFF_CAP_S = 30.0


class FeedState:
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    LIVE = "live"


class WsFeed(ABC):
    """One venue's market-data connection. Subclasses implement subscribe,
    message handling, and book-sync/sequencing for their protocol."""

    venue: ClassVar[VenueName]
    HEARTBEAT_INTERVAL_S: ClassVar[float | None] = None  # None: protocol pings only
    IDLE_TIMEOUT_S: ClassVar[float] = 30.0
    # After an in-band resync (resubscribe), a fresh snapshot must arrive within
    # this window or the whole socket is recycled — a rejected/lost resubscribe
    # must not leave one symbol silently dark on an otherwise-healthy connection.
    RESYNC_DEADLINE_S: ClassVar[float] = 10.0

    def __init__(
        self,
        symbols: Sequence[str],
        registry: BookRegistry,
        on_update: UpdateCallback,
        metrics: Metrics,
    ) -> None:
        self.symbols = list(symbols)
        self.registry = registry
        self.on_update = on_update
        self.metrics = metrics
        self.log = logging.getLogger(f"arb.feeds.{self.venue.value}")
        self.state = FeedState.DISCONNECTED
        self._ws: ClientConnection | None = None
        self._last_msg_mono_ns: int = 0
        self._resync_deadlines: dict[str, float] = {}  # symbol -> time.monotonic() deadline

    # -- subclass protocol ---------------------------------------------------

    @abstractmethod
    def ws_url(self) -> str:
        """Connection URL (may embed the subscription for combined streams)."""

    @abstractmethod
    async def on_connected(self, ws: ClientConnection) -> None:
        """Send subscriptions / kick off snapshot syncs for a fresh socket."""

    @abstractmethod
    async def handle_raw(self, raw: str | bytes, wall_ns: int, mono_ns: int) -> None:
        """Parse and apply one frame. Exceptions are contained by the caller."""

    def heartbeat_payload(self) -> str | None:
        """App-level ping payload; None when the venue needs no app pings."""
        return None

    def on_disconnected(self) -> None:
        """Reset per-connection sync state. Subclasses extend (cancel snapshot
        tasks, clear buffers) and must call super()."""
        self._resync_deadlines.clear()
        for symbol in self.symbols:
            self.registry.get(self.venue, symbol).synced = False

    # -- shared machinery ------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Connect-read-reconnect forever until ``stop`` is set."""
        backoff_s = _BACKOFF_INITIAL_S
        while not stop.is_set():
            heartbeat_task: asyncio.Task[None] | None = None
            try:
                self.state = FeedState.CONNECTING
                self.log.info("connecting", extra={"venue": self.venue.value, "url": self.ws_url()})
                async with websockets.connect(
                    self.ws_url(),
                    max_size=2**23,
                    open_timeout=15,
                    close_timeout=5,
                    ping_interval=20,
                    ping_timeout=15,
                    max_queue=1024,
                ) as ws:
                    self._ws = ws
                    self._last_msg_mono_ns = time.monotonic_ns()
                    await self.on_connected(ws)
                    self.state = FeedState.LIVE
                    backoff_s = _BACKOFF_INITIAL_S
                    heartbeat_task = asyncio.create_task(
                        self._heartbeat_loop(ws), name=f"hb-{self.venue.value}"
                    )
                    async for raw in ws:
                        mono_ns = time.monotonic_ns()
                        wall_ns = time.time_ns()
                        self._last_msg_mono_ns = mono_ns
                        self.metrics.ws_messages.labels(venue=self.venue.value).inc()
                        try:
                            await self.handle_raw(raw, wall_ns, mono_ns)
                        except Exception:
                            excerpt = raw[:512] if isinstance(raw, (str, bytes)) else repr(raw)[:512]
                            self.log.exception(
                                "message handling failed",
                                extra={"venue": self.venue.value, "payload_excerpt": str(excerpt)},
                            )
                            self.metrics.message_errors.labels(venue=self.venue.value).inc()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.warning(
                    "feed connection error: %r", exc, extra={"venue": self.venue.value}
                )
            finally:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    # gather(return_exceptions=True) surfaces the child's
                    # CancelledError as a return value while still propagating
                    # cancellation of THIS task — a plain try/except here would
                    # swallow an outer cancel delivered at this await point.
                    (hb_result,) = await asyncio.gather(
                        heartbeat_task, return_exceptions=True
                    )
                    if isinstance(hb_result, BaseException) and not isinstance(
                        hb_result, asyncio.CancelledError
                    ):
                        self.log.warning(
                            "heartbeat task died: %r",
                            hb_result,
                            extra={"venue": self.venue.value},
                        )
                self._ws = None
                self.state = FeedState.DISCONNECTED
                self.on_disconnected()
                self.metrics.ws_reconnects.labels(venue=self.venue.value).inc()

            if stop.is_set():
                break
            delay = backoff_s * (1.0 + 0.25 * random.random())
            self.log.info(
                "reconnecting in %.1fs", delay, extra={"venue": self.venue.value}
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
            backoff_s = min(backoff_s * 2.0, _BACKOFF_CAP_S)

    async def _heartbeat_loop(self, ws: ClientConnection) -> None:
        """Send app-level pings on schedule; enforce the idle watchdog and the
        per-symbol resync deadline (see RESYNC_DEADLINE_S)."""
        next_ping_mono = time.monotonic() + (self.HEARTBEAT_INTERVAL_S or 0)
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            idle_s = (time.monotonic_ns() - self._last_msg_mono_ns) / 1e9
            if idle_s > self.IDLE_TIMEOUT_S:
                self.log.warning(
                    "idle watchdog: no frames for %.1fs, forcing reconnect",
                    idle_s,
                    extra={"venue": self.venue.value},
                )
                await ws.close(code=1000, reason="idle watchdog")
                return
            expired = self.expired_resync(now)
            if expired is not None:
                self.log.warning(
                    "resync deadline expired for %s; recycling connection",
                    expired,
                    extra={"venue": self.venue.value, "symbol": expired},
                )
                await ws.close(code=1000, reason="resync deadline")
                return
            if self.HEARTBEAT_INTERVAL_S is not None and now >= next_ping_mono:
                payload = self.heartbeat_payload()
                if payload is not None:
                    await ws.send(payload)
                next_ping_mono = now + self.HEARTBEAT_INTERVAL_S

    # -- helpers for subclasses ------------------------------------------------

    def arm_resync_deadline(self, symbol: str) -> None:
        """Start the clock on an in-band resubscribe: a snapshot must arrive
        for ``symbol`` within RESYNC_DEADLINE_S or the connection is recycled."""
        self._resync_deadlines[symbol] = time.monotonic() + self.RESYNC_DEADLINE_S

    def clear_resync_deadline(self, symbol: str) -> None:
        self._resync_deadlines.pop(symbol, None)

    def expired_resync(self, now_mono: float) -> str | None:
        for symbol, deadline in self._resync_deadlines.items():
            if now_mono >= deadline:
                return symbol
        return None

    def record_feed_latency(self, exchange_ts_ms: int, wall_ns: int) -> None:
        """Exchange event time -> local arrival, in ms. One-way latency plus
        clock skew; useful as a trend/anomaly signal, not an absolute truth."""
        latency_ms = wall_ns / 1e6 - exchange_ts_ms
        if -1000.0 < latency_ms < 60_000.0:  # discard obviously skew-broken values
            self.metrics.feed_latency_ms.labels(venue=self.venue.value).observe(latency_ms)

    def note_resync(self, symbol: str, reason: str) -> None:
        self.log.warning(
            "book resync",
            extra={"venue": self.venue.value, "symbol": symbol, "reason": reason},
        )
        self.metrics.book_resyncs.labels(
            venue=self.venue.value, symbol=symbol, reason=reason
        ).inc()
