# Market Data Model

> **Status: IMPLEMENTED** (Phase 1 / Milestone 1, CLAUDE.md §48) except where
> a subsection is explicitly marked **SPECIFIED — NOT IMPLEMENTED**. Everything
> described in present tense exists in code and is cited by file. The model
> covers: canonical identifiers (`arb/models.py`), the local L2 book
> (`arb/feeds/book.py`), the book-event tap and its recording JSONL schema
> (`arb/recording/recorder.py`), the instrument/asset registries
> (`arb/instruments/registry.py`), and the timestamp ladder of CLAUDE.md §12
> as it stands today. Venue-specific protocol facts live in
> `docs/ORDER_BOOK_INTEGRITY.md`.

## 1. Canonical identifiers

### 1.1 Symbols

The canonical instrument key is the string `"BASE/QUOTE"` (e.g. `BTC/USDT`),
validated at config load by `_SYMBOL_RE = ^[A-Z0-9]+/[A-Z0-9]+$` in
`arb/models.py` (`Settings._symbols_canonical`; duplicates rejected). Every
internal structure — book registry, detector keys, recording lines, research
records — uses the canonical form. Venue-native symbols exist only at the
adapter boundary.

### 1.2 Venues

`VenueName` (`arb/models.py`) is a `str` Enum with exactly four members whose
values are used verbatim as config keys, Prometheus label values, log fields,
and the `v` field of recorded book events:

| Member | Value |
|---|---|
| `VenueName.BINANCE` | `"binance"` |
| `VenueName.BYBIT` | `"bybit"` |
| `VenueName.OKX` | `"okx"` |
| `VenueName.BITGET` | `"bitget"` |

### 1.3 Canonical → venue symbol transforms

Each feed module defines a `venue_symbol()` function; the instrument registry
independently defines `venue_symbol_for(venue, symbol)`
(`arb/instruments/registry.py`) and **rejects at load any metadata whose
`exchange_symbol` disagrees with the transform** — a typo cannot silently map
one venue's book onto another market's metadata. Agreement between the two is
pinned by `tests/test_instruments.py`.

| Venue | Transform | `BTC/USDT` becomes |
|---|---|---|
| binance, bybit, bitget | strip `/` | `BTCUSDT` |
| okx | `/` → `-` | `BTC-USDT` |

## 2. The `OrderBook` structure (`arb/feeds/book.py`)

One `OrderBook` per (venue, symbol), created once at boot by `BookRegistry`
and mutated only by its owning feed on the single asyncio event loop (not
thread-safe by design; `__slots__`, no per-update allocation of containers).

| Field | Type | Semantics |
|---|---|---|
| `venue`, `symbol` | `VenueName`, `str` | identity (canonical symbol) |
| `bids`, `asks` | `SortedDict` | `Decimal` price → `Decimal` size, ascending |
| `synced` | property over `_synced` | set `True` by the feed only after a venue-valid snapshot; cleared on disconnect / gap / checksum failure |
| `last_exchange_ts_ms` | `int \| None` | venue event time, milliseconds |
| `last_local_wall_ns` | `int` | `time.time_ns()` at frame read |
| `last_local_mono_ns` | `int` | `time.monotonic_ns()` at frame read |
| `tap` | `BookTap \| None` | recording seam (§4 below) |

**Why Decimal keyed by the literal exchange strings:** OKX and Bitget CRC32
checksums are computed over the *literal* price/size strings the venue sent,
trailing zeros included. `Decimal` round-trips those strings faithfully;
`float` formatting would not. `_plain()` renders with `format(d, "f")` because
`str(Decimal("0.0000001"))` is `"1E-7"` — scientific notation would corrupt
checksum payloads for small-priced assets (asserted against in
`tests/test_book.py::test_checksum_never_uses_scientific_notation`).

Levels enter via `RawLevel = tuple[str, str]` (price, size, exactly as
received); a size that parses to zero deletes the level (`_set_level`).

**Views for signal math** (float64 is far beyond bps-level spread precision):
`best_bid()`, `best_ask()`, `mid()`, `crossed()` (locally-crossed bid ≥ ask
means corrupt sync), `depth()`, `iter_bids()`/`iter_asks()` (best first),
`age_ms(now_mono_ns)` (`+inf` before the first update — a never-fed book is
stale from boot), and `vwap_for_qty(side, qty)`, which walks depth and
**returns `None` when the book cannot fill the full quantity** — callers must
treat that as "no executable price", never fall back to top-of-book.

## 3. Timestamp semantics (CLAUDE.md §12)

Every applied book mutation is stamped with three timestamps:

1. **Exchange event time** (`exchange_ts_ms`, milliseconds) — the venue's own
   stamp: Binance `E`, Bybit `cts or ts` (matching-engine time preferred),
   OKX `ts`, Bitget `ts`. Binance REST depth snapshots carry no event time,
   so their `ets` is `null`.
2. **Local wall clock** (`wall_ns`, `time.time_ns()`).
3. **Local monotonic clock** (`mono_ns`, `time.monotonic_ns()`).

Both local stamps are captured **at frame read** — the first two statements
inside the `async for raw in ws` loop of `WsFeed.run()`
(`arb/feeds/base.py`) — before any parsing. Durations (book age, opportunity
lifetime) use only the monotonic domain; wall time is for human-readable
record fields.

Status of the full §12 ladder:

| §12 timestamp | Status |
|---|---|
| Exchange event timestamp | **Implemented** (per-venue, above) |
| Network receive timestamp | **Implemented** (wall + mono at frame read) |
| Decoder completion timestamp | **NOT IMPLEMENTED** — book application reuses the frame-read stamps; decode and apply latency are not separately measured |
| Order-book application timestamp | **NOT IMPLEMENTED** (conflated with frame read, deliberately, until measurement justifies the extra stamps) |
| Opportunity detection timestamp | **Implemented** — `EvalResult.mono_ns` (`arb/signals/spread.py`), taken from the injected `Clock` |
| Risk-decision / order-send / ack / first-fill / final-fill timestamps | **SPECIFIED — NOT IMPLEMENTED**: no execution code exists (`arb/execution/` is empty) |

`record_feed_latency()` (`arb/feeds/base.py`) observes
`wall_ns/1e6 − exchange_ts_ms` into `arb_feed_latency_ms`, discarding values
outside (−1000 ms, 60 000 ms). This is one-way latency **plus local↔exchange
clock skew** — a trend/anomaly signal, never an absolute latency claim.

Time reaches the detector only through the `Clock` protocol
(`arb/replay/clock.py`): `SystemClock` live, `ReplayClock` in replay, driven
solely by recorded stamps — the basis of deterministic replay (§35).

## 4. Book-event tap model (`BookEventKind`)

Every state change of every book flows through exactly four seams —
`apply_snapshot`, `apply_deltas`, `clear`, and the `synced` setter — and each
fires the optional `tap` callback
(`tap(book, kind, bids, asks, exchange_ts_ms, mono_ns, wall_ns)`). Recording
completeness is therefore **structural**, not per-feed.

| `kind` | Meaning | Levels carried | Stamps |
|---|---|---|---|
| `snapshot` | levels replaced wholesale (book cleared first) | full snapshot | from the data frame |
| `delta` | incremental changes applied | the changed levels | from the data frame |
| `clear` | levels dropped (disconnect/resync) | none | book's **last data-frame** stamps |
| `sync` | `synced` False → True | none | last data-frame stamps |
| `unsync` | `synced` True → False | none | last data-frame stamps |

Control-flow events (`clear`/`sync`/`unsync`) happen in feed control flow, not
on a data frame, so they are journaled at the book's last data-frame stamps:
every recorded timestamp originates from feed data — one time source per
journal — and the replay clock is never advanced by control-flow events. File
order still records true application order. A raising `tap` is contained and
logged (`_emit`): recording failure degrades recording, never market-data
processing (`tests/test_recording.py::test_raising_tap_does_not_break_mutation`).

## 5. Recording JSONL schema (`arb/recording/recorder.py`, `SCHEMA_VERSION = 1`)

One file per observer session: `data/recordings/books-<UTC-stamp>.jsonl`.
`BookEventRecorder.attach()` sets itself as the `tap` of every book in the
registry. Three record types, in file order = application order (single event
loop, single writer):

**Line 1 — `recording_header`** (built by `header_record()`): the
detector-relevant config snapshot so replay can verify comparability.

| Field | Content |
|---|---|
| `type`, `schema_version` | `"recording_header"`, `1` |
| `started_at`, `wall_ns`, `mono_ns` | session start (ISO-8601 UTC + raw stamps) |
| `symbols`, `venues` | active canonical symbols and venue values |
| `signals` | `min_net_edge_bps`, `clip_sizes_usdt` |
| `staleness_ms` | book-freshness threshold |
| `taker_bps` | per-venue configured taker fee (bps) |
| `spread_sample_interval_s` | sampler cadence (replay re-creates the sweep) |

**Per mutation — `book_event`** (compact keys; written by `on_book_event`):

| Key | Content |
|---|---|
| `type` | `"book_event"` |
| `v` | venue value (e.g. `"binance"`) |
| `s` | canonical symbol |
| `k` | `BookEventKind` (`snapshot`/`delta`/`clear`/`sync`/`unsync`) |
| `ets` | exchange event time ms, or `null` (Binance REST snapshots; control-flow events) |
| `mono_ns`, `wall_ns` | local stamps as applied |
| `b`, `a` | bid/ask levels as `[price, size]` **literal strings** (Decimal-exact round trip) |

**Last line — `recording_end`**: `{type, mono_ns, wall_ns}`, appended by
`close()` so replay flushes still-open opportunities at the exact session-end
clock. A crash-truncated file (no end stamp) is still replayable; the engine
flushes at the last event's clock (`arb/replay/engine.py`).

Writer behavior: buffered (`_FLUSH_EVERY = 500` events or
`_FLUSH_INTERVAL_NS = 1 s`, whichever first — a crash loses at most one
buffer, counted, research-grade tolerance); write failures increment
`arb_recording_errors_total` and after `_MAX_CONSECUTIVE_ERRORS = 10` the
recorder disables itself loudly while market-data collection continues.
Successful events count into `arb_recording_events_total{venue}`.

## 6. Instrument metadata (`arb/instruments/registry.py`)

`InstrumentMeta` (strict pydantic model, unknown keys fatal):

| Field | Type / constraint | Notes |
|---|---|---|
| `venue`, `symbol` | `VenueName`, canonical `"BASE/QUOTE"` | `canonical_base`/`canonical_quote` derived properties |
| `exchange_symbol` | `str` | must equal `venue_symbol_for(venue, symbol)` or load fails |
| `status` | `trading` / `suspended` / `unknown` | non-`trading` excludes the (venue, symbol) from evaluation |
| `price_tick`, `qty_step` | `Decimal > 0` | venue precision rules |
| `min_qty`, `min_notional` | `Decimal ≥ 0` | notional in quote units |
| `max_qty`, `max_notional` | optional `Decimal > 0` | |
| `source`, `fetched_at` | provenance | the checked-in file is a **static, unverified snapshot**; refresh via `python -m arb instruments fetch` (`arb/instruments/fetch.py`) from a machine with exchange access |

`InstrumentRegistry.load()` is fatal on: unreadable file, bad YAML, wrong
`schema_version`, duplicate (venue, symbol), or transform mismatch — refuse to
start, never default around. A *missing* (venue, symbol) entry means "no
rounding possible": the detector prices unrounded and warns once at boot.

Pair rounding: `common_qty_step()` computes the exact LCM of two `Decimal`
steps via `Fraction` (correct for non-nested steps, e.g. 0.002 vs 0.005 →
0.01); `round_qty_for_pair()` **floors** to that step (never rounds up —
CLAUDE.md §16) and returns `None` when the result violates either venue's
`min_qty` (unexecutable clip). Minimum notionals are checked by the detector
after VWAPs are known (`arb/signals/spread.py::_price`).

## 7. Asset registry and the equivalence rule (§10, §27)

`ASSET_REGISTRY` maps canonical codes to `AssetInfo(code, kind, issuer,
note)`; current entries: BTC, ETH, SOL (crypto), USDT (issuer Tether), USDC
(issuer Circle) — both stablecoins carry the note "not interchangeable" with
the other.

```python
def assets_equivalent(a: str, b: str) -> bool:
    return a == b
```

Equivalence is **exact-match-only** and deliberately a named function: the
invariant "stablecoins are not automatically valued equally" (CLAUDE.md
§40.23) hangs on nobody ever "improving" this with a similarity table. There
is no conversion graph in this build; `USDT` never equals `USDC`. Evidence:
`tests/test_instruments.py::test_stablecoins_are_never_equivalent` and
`::test_identity_is_exact_match_only`. The configurable quote-conversion
graph of §27 is **SPECIFIED — NOT IMPLEMENTED**.

## 8. Downstream research records (`arb/models.py`)

Two JSONL record types are derived from the model above and written to the
research log by `arb/accounting/opportunities.py` via the detector:
`OpportunityRecord` (one open→close interval of net edge ≥ threshold:
open/close stamps, `duration_ms` from the monotonic clock, gross/net bps at
open, max/mean net bps, VWAPs, `close_reason`, `threshold_bps`) and
`SpreadSample` (periodic **unconditional** sample per pair/direction/clip,
negative edges included, with both books' `*_book_age_ms`). Both carry
`schema_version: 1`. These are theoretical-edge records only; nothing in this
repository labels them as realized anything (§34).
