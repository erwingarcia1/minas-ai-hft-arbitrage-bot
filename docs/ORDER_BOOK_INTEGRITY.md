# Order-Book Integrity

> **Status: IMPLEMENTED** (Phase 1 / Milestone 1, CLAUDE.md §48 and §11) with
> two honest caveats. (1) This development sandbox blocks egress to exchange
> endpoints: every protocol rule below is implemented in
> `arb/feeds/{binance,bybit,okx,bitget}.py` and exercised against
> fixture-driven unit tests (`tests/test_feeds_sync.py`,
> `tests/test_book.py`), **not validated against live venue endpoints from
> here** — endpoint URLs, channels, heartbeat cadences and sequencing rules
> are transcribed from public exchange documentation into the adapters and
> must be validated end-to-end from an operator machine. (2) Two items of
> §11's never-use-when list are not yet gated in code; see §6. Coverage gaps
> are listed in §8.

## 1. The core contract

A book is trustworthy iff **`synced == True` AND `age_ms()` ≤
`risk.staleness_ms`** (default 500 ms, `arb/models.py::RiskSettings`).
`synced` starts `False`, is set `True` by the owning feed only after a
venue-valid snapshot is applied, and is cleared on disconnect, sequence gap,
or checksum mismatch (`arb/feeds/book.py`). Consumers never see partially
recovered state: while a resync is in flight the book is simply invisible to
signals. Any integrity ambiguity is resolved toward resync — the only cost is
temporary blindness, never a trade decision on corrupt data (there are no
trade decisions in this phase; the same gate is the Phase-2 seam).

## 2. Shared machinery (`arb/feeds/base.py`)

All four adapters subclass `WsFeed`, which provides:

| Mechanism | Implementation |
|---|---|
| Reconnect loop | `run()`: connect → read → on any exit, reconnect forever with exponential backoff `0.5 s → ×2 → 30 s cap`, jitter `×(1 + 0.25·rand)`; a venue being down degrades, never crashes, the process |
| Disconnect marking | `on_disconnected()` sets **every book of the venue** `synced = False` before reconnecting; consumers see the venue unusable within one event-loop cycle |
| Idle watchdog | `_heartbeat_loop()`: no frame for `IDLE_TIMEOUT_S` ⇒ force-close the socket (half-open TCP must never keep stale books looking alive) |
| App heartbeats | per-venue `HEARTBEAT_INTERVAL_S` + `heartbeat_payload()`; Binance needs none (protocol ping frames, answered by the `websockets` library) |
| Resync deadline | `arm_resync_deadline(symbol)` on every in-band resubscribe: no fresh snapshot within `RESYNC_DEADLINE_S = 10 s` ⇒ recycle the whole connection — a rejected/lost resubscribe must not leave one symbol silently dark on a healthy socket |
| Storm guard (OKX/Bitget) | `> _MAX_CONSECUTIVE_RESYNCS = 5` back-to-back resyncs for one symbol ⇒ recycle the connection instead of spamming sub/unsub against per-connection rate limits; only a **clean update** ends a streak (the post-resubscribe snapshot also applies cleanly and must not defeat the guard) |
| Error containment | a frame that fails parsing/handling is logged with a payload excerpt and counted in `arb_message_errors_total`; the connection survives |
| Resync accounting | `note_resync(symbol, reason)` ⇒ `arb_book_resyncs_total{venue,symbol,reason}` |
| Timestamps | `wall_ns`/`mono_ns` captured at frame read, before parsing (see `docs/MARKET_DATA_MODEL.md` §3) |

Socket parameters (as coded): `max_size 2²³`, `open_timeout 15 s`,
`close_timeout 5 s`, protocol `ping_interval 20 s` / `ping_timeout 15 s`,
`max_queue 1024`.

## 3. Per-venue mechanics

| Venue | Endpoint / channel (as coded, unverified live) | Heartbeat | Idle timeout | Primary integrity |
|---|---|---|---|---|
| Binance | `wss://stream.binance.com:9443/stream?streams=<sym>@depth@100ms` + REST `https://api.binance.com/api/v3/depth?limit=1000` | protocol pings | 30 s | `U`/`u`/`lastUpdateId` chaining |
| Bybit | `wss://stream.bybit.com/v5/public/spot`, `orderbook.50.<SYM>` | `{"op":"ping"}` / 20 s | 45 s | `u` continuity (no checksum on this venue) |
| OKX | `wss://ws.okx.com:8443/ws/v5/public`, `books` (400 levels) | text `ping` / 25 s | 45 s | `seqId`/`prevSeqId` chain (+ legacy non-zero checksum) |
| Bitget | `wss://ws.bitget.com/v2/ws/public`, `books`, `instType: SPOT` | text `ping` / 30 s | 60 s | CRC32 checksum on every message |

### 3.1 Binance (`arb/feeds/binance.py`)

Diff-depth events buffer while unsynced; a per-symbol `_sync_symbol` task runs
Binance's documented algorithm:

1. Buffer `depthUpdate` events from the socket.
2. GET REST depth (limit 1000) → `lastUpdateId`.
3. Drop buffered events with `u ≤ lastUpdateId`.
4. First survivor must satisfy `U ≤ lastUpdateId+1 ≤ u`; otherwise the
   snapshot is stale relative to the buffer — refetch (throttled at
   `_SNAPSHOT_RETRY_S = 1 s`; a tight loop on the heavy-weight depth request
   would invite an IP ban).
5. After sync each live event must chain `U ≤ last_u+1` (documented overlap
   form); `u ≤ last_u` is a stale duplicate and is dropped; anything else is
   a `sequence_gap` resync.

Two hardening details beyond the letter of the algorithm: **mid-buffer gap
detection** — buffered events are replayed with the *same* continuity rule as
live events, so a hole inside the buffer (e.g. one frame dropped by the
per-message error handler) forces a fresh snapshot instead of marking a book
with ghost levels as synced (`buffer_gap` reason); and **buffer overflow** —
more than `_MAX_BUFFER = 10 000` events while unsynced forces a resync rather
than unbounded memory growth. The sync task never exits on failure: every
error path (fetch failure, malformed/stale snapshot, buffer gap) retries
inside the same task, so the "already syncing" guard in `_start_resync` stays
accurate. REST snapshots carry no exchange timestamp (`ets = null` in the
journal). Deliberate exception to the no-environment-reads rule: the snapshot
session uses `trust_env=True` for proxy support (documented in the module
docstring and README).

### 3.2 Bybit (`arb/feeds/bybit.py`)

`type == "snapshot"` rebuilds the book and sets `last_u = u`. Deltas apply iff
`u == last_u + 1`; `u ≤ last_u` is a dropped duplicate; **`u == 1` is a
service-restart snapshot per Bybit docs and rebuilds the book** regardless of
message type; any other gap ⇒ unsubscribe/resubscribe (Bybit re-sends a
snapshot), with the base-class resync deadline armed. A delta arriving before
any snapshot (resubscribe race) waits. This venue exposes no checksum, so
sequence continuity is the only protocol check — augmented by a **crossed-book
check** after every apply (locally-crossed bid ≥ ask ⇒ corruption ⇒ resync).
Subscribe requests are chunked at `_MAX_SUB_ARGS = 10` (a rejected
oversized request would deliver no topics and silently darken the venue).

### 3.3 OKX (`arb/feeds/okx.py`)

`action:"snapshot"` rebuilds; `action:"update"` applies iff `prevSeqId`
equals the last applied `seqId` (heartbeat pushes repeat
`prevSeqId == seqId` and chain naturally). Any break — including sequence
resets after OKX maintenance, which OKX resolves by re-sending a snapshot —
⇒ unsubscribe/resubscribe with deadline + storm guard.

**Checksum deprecation (OKX changelog 2026-05-21, from public documentation,
UNVERIFIED against a live endpoint):** the `checksum` field is still present
on `books` pushes but is now fixed to 0 and must not be used for integrity;
`seqId`/`prevSeqId` chaining is the prescribed mechanism. The adapter
therefore **verifies the CRC32 only when the field is present AND non-zero**
(older/regional gateways) and skips it otherwise — a genuine CRC of 0 has
probability 2⁻³², so the skip is harmless either way
(`tests/test_feeds_sync.py::test_okx_nonzero_checksum_still_verified`).
Level format is `[price, size, "0", numOrders]`; only the first two fields
enter the book and checksum.

### 3.4 Bitget (`arb/feeds/bitget.py`)

`action:"snapshot"` then `action:"update"`; the v2 `books` channel exposes
**no documented per-update sequence chain**, so the signed-int32 CRC32
checksum over the top 25 interleaved levels (same construction as OKX) is the
primary integrity mechanism — verified on **every** message that carries one;
any mismatch ⇒ unsubscribe/resubscribe with deadline + storm guard. An update
before any snapshot (resubscribe race) is dropped; a locally-crossed book
after apply is treated as corruption ⇒ resync.

### 3.5 Shared checksum construction (`arb/feeds/book.py`)

`OrderBook.checksum_payload(depth=25)` builds the interleaved
`bidP:bidSz:askP:askSz` string over the top 25 levels using the exchange's
literal number formatting (Decimal round trip; `_plain()` forbids scientific
notation); when one side has fewer levels the remaining side's pairs continue
uninterleaved — matching both the OKX and Bitget specs. `crc32_signed()`
encodes as signed int32. Pinned by `tests/test_book.py`
(`test_checksum_payload_interleaves_and_preserves_formatting`,
`test_checksum_payload_uneven_sides`, `test_checksum_preserves_trailing_zeros`,
`test_checksum_never_uses_scientific_notation`, `test_checksum_uses_top_25_only`,
`test_crc32_signed_known_vector`).

## 4. The consumer gate

The detector's `_usable()` (`arb/signals/spread.py`) is the single predicate:

```python
def _usable(self, book: OrderBook, now_mono_ns: int) -> bool:
    return book.synced and book.age_ms(now_mono_ns) <= self.staleness_ms
```

Unusable books are excluded from evaluation; open opportunities touching them
are force-closed (`book_unusable`) and their last pricing is dropped from
`last_evals` so the dashboard/samplers never present a stale spread as
current. `vwap_for_qty` returning `None` (insufficient depth for the clip)
closes with `insufficient_depth` — a thin book cannot keep an opportunity
nominally open.

## 5. Staleness monitor (`arb/risk/staleness.py`)

Every 100 ms (`_TICK_S`), per venue: are ALL of the venue's books synced and
younger than `staleness_ms`? On a usable→unusable transition it calls
`on_unusable` (wired to `SpreadDetector.on_venue_unusable`, which force-closes
every open opportunity touching the venue) and logs; a per-book fresh→stale
backstop (`on_symbol_degraded`) re-evaluates the symbol so a second stale book
on an already-unusable venue cannot leave opportunities dangling until the
slow sampler. Gauges refreshed every tick: `arb_book_age_ms{venue,symbol}`
(−1 encodes +inf) and `arb_venue_usable{venue}`. Books that never received
data have `age == +inf` and are unusable from boot. The monitor's own task
ending is fatal by design — running blind is worse than not running. This
transition point is the documented Phase-2 kill-switch seam.

## 6. §11 never-use-when list → enforcement map

| §11 condition | Enforcement today |
|---|---|
| Snapshot is missing | books boot `synced=False`; only a venue-valid snapshot path sets `True` |
| Sequence continuity failed | per-venue rules in §3 ⇒ `synced=False` + resync |
| Checksum failed | OKX (non-zero only) / Bitget ⇒ `synced=False` + resync |
| Feed is disconnected | `on_disconnected()` unsyncs all venue books; idle watchdog force-closes half-open sockets |
| Data age exceeds threshold | `_usable()` in the detector + `StalenessMonitor.venue_usable()` |
| Clock synchronization is uncertain | **NOT GATED — partially moot by construction**: book age uses the local monotonic clock only, so freshness gating has no cross-clock dependence; but no NTP-health check exists, and the wall-clock-based feed-latency metric absorbs skew (documented as trend-only). A clock-health gate remains **SPECIFIED — NOT IMPLEMENTED** |
| Exchange status is degraded | **NOT IMPLEMENTED**: no venue status/maintenance feed is consumed; degradation is detected only indirectly (idle watchdog, gaps, disconnects). Instrument `status != "trading"` metadata does exclude suspended symbols from evaluation (`arb/signals/spread.py`) |

## 7. Recovery-event recording (§11.10)

Every resync is counted in `arb_book_resyncs_total{venue,symbol,reason}` with
reasons as emitted by the feeds: `connect`-triggered syncs aside, observed
reasons are `sequence_gap`, `buffer_overflow`, `buffer_gap` (Binance),
`checksum_mismatch` (OKX/Bitget), `crossed_book` (Bybit/Bitget). Reconnects
count into `arb_ws_reconnects_total{venue}`. Additionally, every `clear`/
`sync`/`unsync` transition is journaled as a `book_event` by the recording
tap, so recovery history is replayable, not just counted.

## 8. Acceptance-criteria evidence (§48.2, §48.3, §48.4)

| Criterion | Evidence |
|---|---|
| §48.2 every book passes integrity checks | `tests/test_book.py` (snapshot/delta/removal semantics, crossed detection, checksum construction); `tests/test_feeds_sync.py` — Binance: `test_binance_snapshot_sync_drops_stale_and_applies_overlap`, `test_binance_applies_chain_and_ignores_duplicates`, `test_binance_gap_forces_resync`, `test_binance_buffer_gap_forces_fresh_snapshot`, `test_binance_sync_retries_inside_task_on_malformed_snapshot`; Bybit: `test_bybit_snapshot_then_chained_deltas`, `test_bybit_gap_resubscribes`, `test_bybit_u1_is_service_restart_snapshot`, `test_bybit_delta_before_snapshot_waits`, `test_bybit_subscribe_chunks_at_ten_args`, `test_bybit_resubscribe_arms_deadline_and_snapshot_clears_it`; OKX: `test_okx_snapshot_then_chained_update`, `test_okx_sequence_break_resubscribes`, `test_okx_nonzero_checksum_still_verified`, `test_okx_resync_storm_recycles_connection`; Bitget: `test_bitget_snapshot_then_update_with_valid_checksums`, `test_bitget_checksum_mismatch_resubscribes`, `test_bitget_update_before_snapshot_is_dropped`, `test_bitget_crossed_book_resyncs`, `test_bitget_resync_storm_recycles_connection` |
| §48.3 invalid books automatically excluded | `tests/test_spread.py::test_stale_book_blocks_and_closes`, `::test_unsynced_book_blocks`, `::test_venue_unusable_force_closes`, `::test_insufficient_depth_closes`; `tests/test_staleness.py::test_venue_transition_and_per_book_backstop`, `::test_never_updated_books_are_unusable_from_boot`; property test `tests/test_properties.py::test_unsynced_or_stale_book_yields_no_evaluation` (an invalid book can never produce an evaluation) |
| §48.4 all timestamps recorded | `tests/test_recording.py` (every mutation and sync transition journaled, in order, with literal level strings and all three stamps); replay determinism and live-vs-replay byte-exact equivalence in `tests/test_replay.py` |

A previously flagged coverage gap — no Bitget-handler protocol tests — was
closed in the same iteration: `BitgetFeed.handle_raw`/`_handle_entry` is now
directly exercised (valid-checksum apply, checksum-mismatch resubscribe with
armed resync deadline, update-before-snapshot drop, crossed-book resync,
resync-storm connection recycle).
