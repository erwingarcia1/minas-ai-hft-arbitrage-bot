# EXCHANGE_CAPABILITY_MATRIX.md — Venue Capability Matrix

> **Status: PARTIALLY IMPLEMENTED.**
> The **public market-data rows** below are implemented: the repository
> contains working adapters that connect to exactly four public WebSocket
> endpoints and four public REST endpoints (`arb/feeds/*.py`,
> `arb/instruments/fetch.py`), and their protocol logic is fixture-tested
> (`tests/test_feeds_sync.py`, `tests/test_book.py`). **Every other row is
> UNVERIFIED** — no account exists, no credential is read anywhere in the
> code, and this development sandbox blocks egress to exchange endpoints, so
> even the implemented rows are validated against recorded protocol fixtures,
> not a live connection. The versioned `ExchangeCapabilities` registry that
> §8 specifies does not exist in code; this document is its prose
> predecessor.

Governing spec: CLAUDE.md §2 (capability disclosure), §8 (discovery and the
capability registry). Companion: `docs/EXCHANGE_ENTITLEMENTS.md` (per-account
verification states and process); `docs/EXCHANGE_ADAPTER_CONTRACT.md` (the
interface behind which these capabilities sit).

---

## 1. Evidence classes and `verified_at` semantics

§8 requires a versioned registry where every capability carries
`verified_at` and `documentation_version`. Until that registry exists as
data, every cell in this document carries one of three evidence classes:

| Class | Meaning | `verified_at` equivalent |
|---|---|---|
| **CODE** | The repository connects to / implements this today; the value is readable in source and pinned by unit tests against recorded protocol fixtures. Not yet validated end-to-end against the live venue (sandbox egress is blocked; see PROGRESS.md "Known constraint"). | The commit date of the citing file; live `verified_at` remains unset |
| **DOC** | Publicly documented by the venue to exist; **no figure or detail is asserted here**; nothing checked against an account or live endpoint | Unset — "UNVERIFIED — public docs, no account; verify in entitlement phase (§8, docs/EXCHANGE_ENTITLEMENTS.md)" |
| **N/A-M1** | Deliberately out of scope for Milestone 1 (public data only, §48): nothing in this repository uses it and nothing may, per §3 | Unset |

**This milestone uses public data only.** Per §8 ("enable only capabilities
available to the actual account") no account-gated capability is enabled,
because no account exists. A `DOC` cell never authorizes building on the
capability; it only marks a row for the §8 entitlement pass.

## 2. Public WebSocket market data — CODE-evidenced

All values below are read directly from the venue adapters. File citations
are the source of truth; if this table and the code disagree, the code wins.

| | Binance Global | Bybit | OKX Global | Bitget |
|---|---|---|---|---|
| Adapter | `arb/feeds/binance.py` (`BinanceFeed`) | `arb/feeds/bybit.py` (`BybitFeed`) | `arb/feeds/okx.py` (`OkxFeed`) | `arb/feeds/bitget.py` (`BitgetFeed`) |
| WS endpoint | `wss://stream.binance.com:9443/stream` | `wss://stream.bybit.com/v5/public/spot` | `wss://ws.okx.com:8443/ws/v5/public` | `wss://ws.bitget.com/v2/ws/public` |
| Book channel | `<sym>@depth@100ms` diff-depth, combined stream | `orderbook.50.<SYMBOL>` | `books` (400-level, per adapter docstring) | `books` (`instType: SPOT`) |
| Subscription | Embedded in the combined-stream URL (`ws_url()`) | `{"op":"subscribe","args":[...]}`, chunked at 10 args (`_MAX_SUB_ARGS`) | `{"op":"subscribe","args":[{channel, instId}]}` | `{"op":"subscribe","args":[{instType, channel, instId}]}` |
| Snapshot source | REST `GET /api/v3/depth?limit=1000`, buffered-diff sync per Binance's documented algorithm | In-band `type:"snapshot"` push on (re)subscribe | In-band `action:"snapshot"` push on (re)subscribe | In-band `action:"snapshot"` push on (re)subscribe |
| Sequence mechanism | `U`/`u` chaining; overlap form `U <= last_u+1 <= u` accepted; gap ⇒ new REST snapshot | `u == last_u + 1`; `u == 1` treated as service-restart snapshot; gap ⇒ resubscribe | `seqId`/`prevSeqId` chaining; break ⇒ resubscribe | None implemented (adapter docstring: no documented per-update chain on this channel) |
| Checksum mechanism | None | None — sequence continuity is the only integrity check | CRC32 (signed int32, top 25 levels) — verified **only when non-zero**; field deprecated and fixed to 0 per OKX changelog dated 2026-05-21 in the adapter docstring (public docs, UNVERIFIED) | CRC32 (signed int32, top 25 interleaved levels, same construction as OKX) — verified on **every** message carrying one; primary integrity mechanism |
| Heartbeat | Protocol-level ping frames, auto-answered by the `websockets` library; no app ping (`HEARTBEAT_INTERVAL_S = None`) | App-level `{"op":"ping"}` every 20 s | App-level literal `ping` every 25 s; `pong` reply consumed | App-level literal `ping` every 30 s; `pong` reply consumed |
| Local idle watchdog (`IDLE_TIMEOUT_S`) | 30 s | 45 s | 45 s | 60 s |
| Crossed-book detection | Not checked post-apply (sequence rules only) | `book.crossed()` after every apply ⇒ resubscribe | Not checked post-apply (seqId chain + non-zero checksum) | `book.crossed()` after every apply ⇒ resubscribe |
| Resync path | Per-symbol REST snapshot task; retries inside the task; buffer overflow (>10 000 events) forces fresh resync | Unsubscribe/resubscribe one topic; base-class `RESYNC_DEADLINE_S` (10 s) recycles the socket if no snapshot follows | Same as Bybit, plus storm guard: >5 consecutive resyncs (`_MAX_CONSECUTIVE_RESYNCS`) recycles the connection | Same as OKX (deadline + 5-resync storm guard) |
| Venue symbol form | `BTC/USDT` → `BTCUSDT` | `BTCUSDT` | `BTC-USDT` | `BTCUSDT` |

Shared connection policy (all four, `arb/feeds/base.py` `WsFeed.run`):
exponential backoff with jitter (0.5 s → 30 s cap) on any disconnect; every
book for the venue is marked `synced = False` before reconnecting; frames
that fail handling are contained, logged with a payload excerpt, and counted
in `arb_message_errors_total`.

## 3. Public REST endpoints used — CODE-evidenced

The Milestone-1 code performs exactly five kinds of unauthenticated REST
calls, all GETs:

| Venue | Endpoint | Purpose | Code |
|---|---|---|---|
| Binance | `https://api.binance.com/api/v3/depth` (`limit=1000`) | Depth snapshot for diff-stream sync | `arb/feeds/binance.py` `_fetch_snapshot` |
| Binance | `https://api.binance.com/api/v3/exchangeInfo` | Instrument tick/step/min-notional (filters: `PRICE_FILTER`, `LOT_SIZE`, `NOTIONAL`/`MIN_NOTIONAL`) | `arb/instruments/fetch.py` `parse_binance` |
| Bybit | `https://api.bybit.com/v5/market/instruments-info?category=spot` | Instrument metadata (`priceFilter.tickSize`, `lotSizeFilter`) | `arb/instruments/fetch.py` `parse_bybit` |
| OKX | `https://www.okx.com/api/v5/public/instruments?instType=SPOT` | Instrument metadata (`tickSz`/`lotSz`/`minSz`; no spot min-notional ⇒ 0) | `arb/instruments/fetch.py` `parse_okx` |
| Bitget | `https://api.bitget.com/api/v2/spot/public/symbols` | Instrument metadata (precision as decimal places; `minTradeAmount`, `minTradeUSDT`) | `arb/instruments/fetch.py` `parse_bitget` |

Instrument refresh runs out-of-band via `python -m arb instruments fetch`
(never in the data path); the checked-in `config/instruments.yaml` snapshot
records its own provenance and is marked conservative/UNVERIFIED.

## 4. §2 capability disclosure matrix

Per §2 the project must document, per venue, whether each capability class
exists — and must not claim access it does not have. Legend: **CODE** /
**DOC** / **N/A-M1** per §1 above. Every **DOC** cell reads in full:
*"UNVERIFIED — public docs, no account; verify in entitlement phase (§8,
docs/EXCHANGE_ENTITLEMENTS.md)"* — abbreviated below as **DOC-UNVERIFIED**.
No rate-limit, connection-limit, or tier figures are stated anywhere in this
document, deliberately (§8: never hard-code published limits as constants).

| Capability (§2 / §8) | Binance Global | Bybit | OKX Global | Bitget |
|---|---|---|---|---|
| Public WebSocket market-data feeds | **CODE** (§2 above) | **CODE** | **CODE** | **CODE** |
| Public REST market/instrument data | **CODE** (§3 above) | **CODE** | **CODE** | **CODE** |
| Private WebSocket feeds (orders/executions/balances) | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED |
| WebSocket order entry | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED |
| FIX order entry | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED |
| SBE market data | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED |
| Fast order-response feeds | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED |
| VIP-only market-data channels | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED |
| Institutional API access | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED |
| Disconnect-cancel-all protection | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED |
| Self-trade prevention | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED |
| Batch orders | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED |
| Spot IOC / FOK / post-only | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED |
| Demo / test environment | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED |
| Rate / connection / subscription limits (figures) | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED | DOC-UNVERIFIED |
| Withdrawal-capable APIs | N/A-M1 — must be **rejected** at key load in every future phase (§3) | N/A-M1 | N/A-M1 | N/A-M1 |

Two venue constraints are already implemented defensively even though the
figures originate from public docs (still DOC-class, UNVERIFIED live):

- **Bybit** subscribe requests are chunked at 10 args (`_MAX_SUB_ARGS`,
  `arb/feeds/bybit.py`; tested in
  `test_bybit_subscribe_chunks_at_ten_args`) because an oversized request
  is rejected wholesale and would silently darken the venue.
- **Binance** 24-hour connection termination is noted in the adapter
  docstring; no special handling exists or is needed — the base-class
  reconnect loop absorbs it.

## 5. The §8 registry — SPECIFIED, NOT IMPLEMENTED

The `ExchangeCapabilities` struct of §8 (fields `websocket_market_data`,
`websocket_order_entry`, `fix_order_entry`, `sbe_market_data`,
`fast_order_response`, `disconnect_cancel_all`, `self_trade_prevention`,
`batch_orders`, `spot_ioc`, `spot_fok`, `spot_post_only`,
`demo_environment`, `verified_at`, `documentation_version`) has no code
counterpart yet. When it is built (no later than Milestone 3, the first
phase touching an account):

1. Each **CODE** row in this document seeds `websocket_market_data = true`
   with `verified_at` set only after a live end-to-end run from an operator
   machine (this sandbox cannot produce that evidence).
2. Each **DOC** row enters as `false`/unset until the §8/§4 entitlement
   pass in `docs/EXCHANGE_ENTITLEMENTS.md` upgrades it with `verified_at`,
   `documentation_version`, and account evidence.
3. Consumers must fail safely on absent capabilities (§8): a feature keyed
   to an unverified capability starts disabled and says so in logs/metrics.

## 6. Known limitations of this matrix

- Live endpoint behaviour (actual connect, heartbeat cadence acceptance,
  snapshot latencies) is untested from this sandbox; protocol logic is
  pinned by fixtures only. First operator-machine run should be treated as
  the real verification event for every CODE cell.
- The OKX checksum-deprecation date (2026-05-21) is transcribed from the
  adapter docstring, which cites the OKX changelog; it has not been
  re-verified against the venue from this environment.
- Depth figures ("400 levels" OKX, "50" Bybit, "1000" Binance snapshot,
  "top 25" checksums) are the values the code subscribes to/uses — they are
  CODE-evidenced choices, not statements about the maximum each venue
  offers.
