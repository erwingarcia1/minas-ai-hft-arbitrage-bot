# EXCHANGE_ADAPTER_CONTRACT.md — Adapter Interface: Normative Contract and Phase-1 Reality

> **Status: PARTIALLY IMPLEMENTED.**
> The full §9 contract below is the **normative target** for all four venue
> adapters. What exists today is its public-market-data subset: a shared
> `WsFeed` base class (`arb/feeds/base.py`) with four venue subclasses
> (`arb/feeds/{binance,bybit,okx,bitget}.py`), a public REST instrument
> fetch (`arb/instruments/fetch.py`), and health signals via the staleness
> monitor and Prometheus metrics. **Every private, order, or account method
> is NOT IMPLEMENTED — `arb/execution/` is an empty package by design, and
> no code path reads a credential** (§3, PROGRESS.md). No entry in this
> document weakens that.

Governing spec: CLAUDE.md §9 (this contract), §8 (capabilities gate what an
adapter may do), §11 (book integrity every adapter must uphold), §39
(contract tests). Companions: `docs/EXCHANGE_CAPABILITY_MATRIX.md`,
`docs/ORDER_BOOK_INTEGRITY.md`, `docs/MARKET_DATA_MODEL.md`.

---

## 1. The normative §9 contract

Each of OKX, Bitget, Bybit and Binance gets a **separate native adapter**
(no ccxt-style generic layer) implementing:

```text
connect_market_data()          connect_private_stream()    connect_order_entry()
disconnect()

fetch_server_time()            synchronize_clock()

fetch_instruments()            fetch_symbol_rules()
fetch_fee_schedule()           fetch_account_fee_rate()
fetch_balances()               fetch_open_orders()
fetch_order()                  fetch_fills()

subscribe_order_books()        subscribe_best_bid_offer()  subscribe_public_trades()
subscribe_orders()             subscribe_executions()      subscribe_balances()

place_limit_order()            place_ioc_order()
place_fok_order()              place_post_only_order()

cancel_order()                 cancel_all_orders()
activate_disconnect_cancel_all()

reconcile_order()              reconcile_fills()           reconcile_balances()
reconcile_account()

health_check()
```

Two normative rules travel with the method list:

1. **One common contract-test suite** — every adapter must pass the same
   behavioural tests (§9, §39).
2. **Do not force exchanges into an incorrect abstraction** — exchange-
   specific behaviour is allowed *behind* the common interface. The
   interface promises outcomes (a synced book, a reconciled order), not
   uniform mechanics.

## 2. Contract coverage today

| §9 method | Status | Phase-1 realization |
|---|---|---|
| `connect_market_data()` | **IMPLEMENTED** | `WsFeed.run(stop)` — connect/read/reconnect loop with exponential backoff + jitter (0.5–30 s), heartbeat task, idle watchdog (`arb/feeds/base.py`) |
| `disconnect()` | **IMPLEMENTED** | Setting the shared `stop` event ends `run()`; `on_disconnected()` marks every venue book `synced = False` and clears per-connection sync state |
| `fetch_instruments()` / `fetch_symbol_rules()` | **IMPLEMENTED** (public subset) | `arb/instruments/fetch.py`: `fetch_venue()` + pure per-venue parsers (`parse_binance`, `parse_bybit`, `parse_okx`, `parse_bitget`) producing `InstrumentMeta` (tick, step, min-qty, min-notional, status, provenance); CLI `python -m arb instruments fetch`; all-or-nothing refresh — a missing symbol or parse failure aborts the write |
| `subscribe_order_books()` | **IMPLEMENTED** | Per-venue `on_connected(ws)`: URL-embedded combined streams (Binance), `{"op":"subscribe"}` requests (Bybit — chunked at 10 args, OKX, Bitget); in-band resubscribe paths for resync |
| `health_check()` | **IMPLEMENTED** (as push, not poll) | `StalenessMonitor.venue_usable()` recomputed every 100 ms (`arb/risk/staleness.py`); gauges `arb_venue_usable`, `arb_book_age_ms`; counters `arb_ws_reconnects_total`, `arb_message_errors_total`, `arb_book_resyncs_total`; histogram `arb_feed_latency_ms` (`arb/monitoring/metrics.py`) |
| `fetch_fee_schedule()` | **PARTIAL — static config only** | Per-venue maker/taker bps from `config/config.yaml` (`FeeTier`, `arb/models.py`; consumed by `arb/signals/fees.py`). Nothing is fetched from a venue; operator sets tiers, retail defaults assumed conservative |
| `subscribe_best_bid_offer()` / `subscribe_public_trades()` | NOT IMPLEMENTED | L2 `books` channels only; BBO derives from the local book (`OrderBook.best_bid/best_ask`) |
| `fetch_server_time()` / `synchronize_clock()` | NOT IMPLEMENTED | Feed-latency measurement acknowledges unbounded clock skew instead (`record_feed_latency` docstring: trend signal, not absolute) |
| `connect_private_stream()` / `connect_order_entry()` | **NOT IMPLEMENTED — by design** | Requires credentials; none are read (§3) |
| `fetch_account_fee_rate()`, `fetch_balances()`, `fetch_open_orders()`, `fetch_order()`, `fetch_fills()` | **NOT IMPLEMENTED — by design** | Milestone 3 at the earliest (shadow trading, read-only keys) |
| `subscribe_orders()` / `subscribe_executions()` / `subscribe_balances()` | **NOT IMPLEMENTED — by design** | Same gate |
| `place_*_order()`, `cancel_order()`, `cancel_all_orders()`, `activate_disconnect_cancel_all()` | **NOT IMPLEMENTED — by design** | `arb/execution/` is empty; there is no order-placement code in this repository |
| `reconcile_*()` | NOT IMPLEMENTED | Depends on private streams; specified in docs/EXECUTION_STATE_MACHINE.md |

## 3. Today's adapter interface: the `WsFeed` subclass protocol

Until the full contract exists, the *de facto* adapter contract is the
abstract surface of `WsFeed` (`arb/feeds/base.py`). A venue adapter is a
subclass providing:

| Member | Kind | Obligation |
|---|---|---|
| `venue: ClassVar[VenueName]` | class attr | Identity used in logs, metrics labels, `BookRegistry` keys |
| `ws_url() -> str` | abstract | Connection URL; may embed the subscription (Binance combined streams) |
| `on_connected(ws)` | abstract | Send subscriptions / start snapshot syncs for a fresh socket |
| `handle_raw(raw, wall_ns, mono_ns)` | abstract | Parse and apply one frame; exceptions are contained by the caller (logged, counted in `arb_message_errors_total`, connection survives) |
| `heartbeat_payload() -> str \| None` | hook | App-level ping payload; `None` when protocol pings suffice (Binance) |
| `on_disconnected()` | hook | Reset per-connection sync state; **must call `super()`**, which clears resync deadlines and marks all venue books unsynced |
| `HEARTBEAT_INTERVAL_S`, `IDLE_TIMEOUT_S`, `RESYNC_DEADLINE_S` | class attrs | Venue-tuned heartbeat cadence, idle watchdog, and snapshot-after-resubscribe deadline |

Base-class services every subclass builds on: `arm_resync_deadline` /
`clear_resync_deadline` / `expired_resync` (a resubscribe that never yields
a snapshot recycles the whole connection), `record_feed_latency`
(exchange-ts → arrival histogram with skew guard), `note_resync`
(structured log + `arb_book_resyncs_total{reason}`), and the
`on_update(venue, symbol)` callback fired after every applied book change —
the documented region-splitting seam for a future Phase-4 topology.

Behavioural obligations the base class enforces uniformly:

- On **any** exit from the read loop, every book of the venue is marked
  unsynced before reconnect — consumers see the venue as unusable within
  one event-loop cycle.
- A venue being down degrades the process, never crashes it (connect and
  subscribe errors retry with backoff forever).
- A book is only trustworthy when `synced` is `True` **and** `age_ms()` is
  within `risk.staleness_ms` — both checked by the detector and the
  staleness monitor, never by the adapter alone (`arb/feeds/book.py`
  docstring).

## 4. Exchange-specific behaviour behind the interface (§9 rule in practice)

The §9 "don't force an incorrect abstraction" rule is already load-bearing.
Consumers (`SpreadDetector`, `StalenessMonitor`, recorder) see only
`BookRegistry` books; each adapter hides a genuinely different protocol:

| Venue | Venue-specific mechanics fully contained in the adapter |
|---|---|
| Binance | REST snapshot + buffered diff sync (drop `u <= lastUpdateId`, overlap rule `U <= last_u+1 <= u`); the sync task retries every failure internally and only exits on success or cancellation; buffer-overflow (>10 000 events) and mid-buffer holes force fresh snapshots |
| Bybit | `u == 1` delta is a service-restart snapshot and rebuilds the book; sequence is the *only* integrity signal (no checksum); post-apply crossed-book check |
| OKX | `seqId`/`prevSeqId` chaining; deprecated checksum verified only when non-zero (field fixed to 0 per the changelog cited in the adapter); resync-storm guard (>5 consecutive ⇒ recycle connection) |
| Bitget | No documented sequence chain — CRC32 checksum (top 25 interleaved levels) is the primary integrity mechanism, verified on every message that carries one; crossed-book check; same storm guard as OKX |

All four converge on identical *outcomes*: an unsynced book during any
ambiguity, resync rather than guessing, and books whose Decimal levels
round-trip the exchange's literal strings (checksum-safe; `OrderBook`
stores levels exactly as received, `_plain()` prevents scientific
notation).

## 5. Contract tests — current form and target form

§9/§39 demand one common behavioural suite. Today the common behaviours are
tested venue-by-venue against the **real `handle_raw` code paths** with
canned protocol frames (`tests/test_feeds_sync.py`; only network I/O is
faked), plus shared book/property suites (`tests/test_book.py`,
`tests/test_properties.py` — e.g. an invalid book cannot be evaluated). The
behaviours every adapter is held to:

| Common behaviour | Evidence |
|---|---|
| Snapshot then chained deltas apply | `test_binance_snapshot_sync_drops_stale_and_applies_overlap`, `test_bybit_snapshot_then_chained_deltas`, `test_okx_snapshot_then_chained_update` |
| Stale duplicates are dropped, never re-applied | `test_binance_applies_chain_and_ignores_duplicates`, Bybit duplicate assertions |
| A sequence gap unsyncs the book and the gapped delta is **not** applied | `test_binance_gap_forces_resync`, `test_bybit_gap_resubscribes`, `test_okx_sequence_break_resubscribes` |
| Data before a snapshot waits (resubscribe races) | `test_bybit_delta_before_snapshot_waits`; equivalent guards in OKX/Bitget (`last_seq is None` / `_have_snapshot`) |
| Resync converges or escalates (deadline / storm guard / fresh snapshot) | `test_bybit_resubscribe_arms_deadline_and_snapshot_clears_it`, `test_okx_resync_storm_recycles_connection`, `test_binance_buffer_gap_forces_fresh_snapshot` |
| Integrity signals are honoured, including edge cases | `test_okx_nonzero_checksum_still_verified` (zero checksum skipped, wrong non-zero checksum resyncs) |

**Gap, stated honestly:** this is a set of per-venue tests asserting shared
behaviours, not yet a single parameterized suite run against all adapters.
Formalizing it into one contract-test harness (one suite, four adapters, as
§9 words it) is required before any adapter grows private/order methods —
tracked in docs/FEATURE_MATRIX.md, and a Milestone-2 entry condition.

## 6. Rules for extending an adapter (future phases)

1. No private or order method may be added while the repository is in
   Milestone 1; §48.7 ("no orders can be placed") is an acceptance
   criterion, and §3 defaults gate everything after it.
2. Any new method lands in the common contract suite *first*; an adapter
   that cannot pass a contract behaviour must fail loudly at startup, not
   approximate it (§8 fail-safe rule).
3. Venue quirks stay inside the adapter. If a quirk leaks a new concept to
   consumers (as Bybit's `u == 1` restart or OKX's checksum deprecation
   could have), the *interface* is what changes, deliberately and for all
   four venues at once.
4. Credentials, when they first appear (Milestone 3, read-only), are
   inspected on load and rejected if withdrawal/transfer-capable — see
   docs/EXCHANGE_ENTITLEMENTS.md §6 and CLAUDE.md §3. The adapter contract
   never handles raw secrets in method signatures.
