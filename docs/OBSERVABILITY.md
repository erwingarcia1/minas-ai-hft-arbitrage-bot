# Observability

> **Status: IMPLEMENTED** (Phase 1 / Milestone 1, CLAUDE.md §48) for
> everything described in present tense: Prometheus metrics
> (`arb/monitoring/metrics.py`), structured JSON logging
> (`arb/monitoring/logging.py`), the terminal dashboard
> (`arb/monitoring/dashboard.py`), and the JSONL data outputs
> (`arb/accounting/opportunities.py`, `arb/recording/recorder.py`).
> The §37 metric families that presuppose order placement — execution,
> recovery, financial, inventory — are **SPECIFIED — NOT IMPLEMENTED**;
> there is no execution code in this build, so those numbers cannot exist
> yet. No latency-performance claims are made anywhere in this document;
> the metrics measure, they do not advertise.

## 1. Prometheus metrics (`arb/monitoring/metrics.py`)

All metrics live on an **explicit `CollectorRegistry`** (never the
`prometheus_client` global default), so tests instantiate `Metrics()` freely
without duplicated-timeseries errors, and the replay engine
(`arb/replay/engine.py`) deliberately uses a throwaway `Metrics()` — replay
reports through its returned `ReplayStats` and the record sink, never through
Prometheus.

`Metrics.start_server(port)` exposes `/metrics` via
`prometheus_client.start_http_server` on the port from
`monitoring.metrics_port` (`config/config.yaml`, default **9109**). The
scrape handler runs in prometheus_client's own daemon thread and never
touches the asyncio event loop. A failed bind (port in use) **raises at
boot — fatal by design**: running this system without observability is not
acceptable (module docstring, `arb/monitoring/metrics.py`).

### 1.1 Bucket sets

| Constant | Values | Used by |
|---|---|---|
| `_LATENCY_BUCKETS_MS` | 5, 10, 25, 50, 100, 200, 400, 800, 1600, 5000 | `arb_feed_latency_ms` |
| `_SPREAD_BUCKETS_BPS` | −50, −30, −20, −10, −5, −2, 0, 1, 2, 3, 5, 8, 12, 20, 50 | `arb_spread_gross_bps`, `arb_spread_net_bps` |
| `_DURATION_BUCKETS_MS` | 50, 100, 250, 500, 1000, 2500, 5000, 15000, 60000 | `arb_opportunity_duration_ms` |

The spread buckets deliberately extend deep into negative territory: the
normal state of net-of-fees cross-venue spreads at retail fee tiers is
negative, and the histogram must show that honestly rather than clip it.

### 1.2 Complete metric inventory

| Metric | Type | Labels | Meaning / observation site |
|---|---|---|---|
| `arb_ws_messages_total` | Counter | `venue` | WebSocket frames received; incremented per frame in `WsFeed._run_once` (`arb/feeds/base.py`) |
| `arb_ws_reconnects_total` | Counter | `venue` | Reconnect events, incremented in the feed supervision loop (`arb/feeds/base.py`) |
| `arb_message_errors_total` | Counter | `venue` | Frames that failed handling (raised in the venue handler); the connection survives (`arb/feeds/base.py`, also Binance REST snapshot errors, `arb/feeds/binance.py`) |
| `arb_book_resyncs_total` | Counter | `venue`, `symbol`, `reason` | Order-book resyncs by cause (sequence gap, checksum mismatch, …), via `WsFeed.record_resync` (`arb/feeds/base.py`) |
| `arb_feed_latency_ms` | Histogram | `venue` | Exchange event timestamp → local arrival, via `record_feed_latency` in every venue adapter. **Includes local↔exchange clock skew** — a trend signal, not an absolute latency claim |
| `arb_book_age_ms` | Gauge | `venue`, `symbol` | Age of last book update in ms; **−1 means never updated**. Refreshed every 100 ms by the staleness monitor (`arb/risk/staleness.py`) |
| `arb_venue_usable` | Gauge | `venue` | 1 when *all* of a venue's books are synced and younger than `risk.staleness_ms`; set on every monitor tick |
| `arb_spread_gross_bps` | Histogram | `symbol`, `buy_venue`, `sell_venue` | Gross executable (depth-walk VWAP) spread per evaluation, observed by `SpreadDetector._observe_metrics` (`arb/signals/spread.py`) |
| `arb_spread_net_bps` | Histogram | `symbol`, `buy_venue`, `sell_venue` | Same evaluation net of both taker fees |
| `arb_opportunities_total` | Counter | `symbol`, `buy_venue`, `sell_venue` | Closed opportunity records (incremented at close, `SpreadDetector._close`) |
| `arb_opportunity_duration_ms` | Histogram | — | How long opportunities persisted (open→close), observed at close |
| `arb_records_written_total` | Counter | — | Research records written to the JSONL log (`arb/accounting/opportunities.py`) |
| `arb_record_write_errors_total` | Counter | — | Research-record write failures — makes silent data loss visible |
| `arb_recording_events_total` | Counter | `venue` | Book events journaled for replay by the recording tap (`arb/recording/recorder.py`) |
| `arb_recording_errors_total` | Counter | — | Book-event journal write failures; after 10 consecutive failures the recorder disables itself loudly (`_MAX_CONSECUTIVE_ERRORS`, `arb/recording/recorder.py`) |

Two invariants worth alerting on: `arb_record_write_errors_total` and
`arb_recording_errors_total` should stay at zero (any increase means data
loss is happening and is being *counted*, not hidden), and
`rate(arb_recording_events_total)` dropping to zero while
`rate(arb_ws_messages_total)` stays positive means the recorder disabled
itself — replay data ends there while collection continues.

## 2. Structured JSON logging (`arb/monitoring/logging.py`)

`JsonFormatter` renders every log record as one `orjson`-encoded object per
line with fields `ts` (UTC ISO-8601, milliseconds), `level`, `logger`, `msg`,
plus **every `extra={...}` field** the call site attached (`venue`, `symbol`,
`reason`, `kind`, `record_type`, …) — anything not in the standard
`LogRecord` attribute set is passed through. Exceptions serialize their
traceback into `exc_info`. A field orjson cannot serialize is stringified via
a `TypeError` fallback rather than raised: logging must never take the
process down.

`setup_logging(level, log_dir)` routing:

| Condition | Destination |
|---|---|
| Dashboard active (`log_dir` set) | `logs/arb-YYYYMMDD.log` file — stdout belongs to the dashboard |
| Headless (`--no-dashboard`, `log_dir=None`) | stderr, JSON lines |

Third-party debug noise is clamped: the `websockets` and `aiohttp` loggers
are pinned to INFO so the research-grade stream stays parseable. No private
credential can appear in logs in this phase for the strongest possible
reason: **no credentials are read anywhere in this build** (§38 discipline
still applies to later phases).

## 3. Terminal dashboard (`arb/monitoring/dashboard.py`)

A read-only `rich.live.Live` view refreshed 4×/s (`_REFRESH_S = 0.25`),
enabled by `monitoring.dashboard: true` (auto-off outside a TTY). It is
**cosmetic by contract**: any exception logs `"dashboard crashed; continuing
without it"` and stops only the dashboard — feeds, detector, recorder and
monitor continue. `live.update` runs in an executor thread so a stalled
terminal (Ctrl+S, frozen ssh) blocks that worker, never the event loop
feeding the books.

Panels, top to bottom:

| Panel | Content (source) |
|---|---|
| Header | `PHASE 1 — OBSERVER MODE (no order code exists in this build)` plus per-venue usable/unusable coloring from `StalenessMonitor.usable` |
| **Venues / books** | Per (venue, symbol): feed state, `synced`, age ms, best bid/ask, bid/ask level counts (`BookRegistry.all_books()`) |
| **Best current net spreads** | Per symbol at the smallest configured clip: best direction from `detector.last_evals`, gross and net bps, colored green/yellow/red against `detector.threshold_bps` |
| **Recent opportunities** | `detector.recent_closed` (a `deque(maxlen=10)`): net at open, max net, duration ms, `close_reason`; title shows current `detector.open_count` |

## 4. JSONL data outputs

| File | Writer | Contents |
|---|---|---|
| `data/opportunities-YYYYMMDD.jsonl` | `OpportunityLog` (`arb/accounting/opportunities.py`), rotated per UTC day, flushed per write | `type=opportunity` records (open→close lifecycle: `net_bps_open`, `max_net_bps`, `mean_net_bps`, `duration_ms`, `est_profit_usdt_open`, `close_reason`, …; `arb/models.py:OpportunityRecord`) and `type=spread_sample` unconditional periodic samples every `monitoring.spread_sample_interval_s` (30 s default), negative edges included |
| `data/recordings/books-<UTC>.jsonl` | `BookEventRecorder` (`arb/recording/recorder.py`) | Deterministic book-event journal: `recording_header` (config snapshot), one `book_event` per applied mutation/sync transition with literal level strings and exchange/wall/mono stamps, `recording_end`. Input to `python -m arb replay` |
| `logs/arb-YYYYMMDD.log` | `setup_logging` | JSON log lines (dashboard mode) |
| Report output | `python -m arb report [--json]` (`arb/report/viability.py`) | Ranked viability rows; the JSON carries a baked-in note that results are **THEORETICAL edge, not realized profit** |

Write failures in either JSONL writer are logged, counted in the
corresponding error metric, and never fatal to market-data collection.

## 5. CLAUDE.md §37 metric families — implemented vs specified

| §37 family | Status | Where |
|---|---|---|
| Market data: feed latency percentiles | **Implemented** (histogram; percentiles at query time; clock-skew caveat) | `arb_feed_latency_ms` |
| Market data: message rate | **Implemented** | `rate(arb_ws_messages_total)` |
| Market data: sequence gaps / resyncs | **Implemented** (by cause) | `arb_book_resyncs_total{reason=…}` |
| Market data: reconnects | **Implemented** | `arb_ws_reconnects_total` |
| Market data: stale-book incidents | **Implemented** | `arb_book_age_ms`, `arb_venue_usable`, monitor WARN logs |
| Market data: book rebuild time | **SPECIFIED — NOT IMPLEMENTED** (resyncs are counted, not timed) | — |
| Opportunity: detected / duration ("half-life") / profit distribution | **Implemented** | `arb_opportunities_total`, `arb_opportunity_duration_ms`, `arb_spread_net_bps`, plus full distributions in the JSONL research log |
| Opportunity: rejected-opportunity reasons | **Partially implemented** — close reasons (`edge_below_threshold`, `insufficient_depth`, `book_unusable`, `venue_unusable`, `shutdown`) are recorded per opportunity; there is no dedicated rejection counter metric | `close_reason` in `OpportunityRecord` |
| Opportunity: depth-qualified / risk-approved counts | **SPECIFIED — NOT IMPLEMENTED** (no risk engine exists yet) | — |
| Execution (order-send/ack/fill latency, fill rate, mismatch, …) | **SPECIFIED — NOT IMPLEMENTED** — no orders exist | Milestone 2+ |
| Recovery (residual exposure, hedge latency/slippage, …) | **SPECIFIED — NOT IMPLEMENTED** | Milestone 2+ |
| Financial (fees paid, slippage realized, net realized profit, …) | **SPECIFIED — NOT IMPLEMENTED** — nothing trades; only theoretical figures exist and they are labelled as such (§34) | Milestone 2+ |
| Inventory (deviation, utilization, rebalancing, …) | **SPECIFIED — NOT IMPLEMENTED** — no balances are read | Milestone 2+ |

## 6. Honest limitations

- `arb_feed_latency_ms` mixes network latency with local↔exchange clock
  skew (README "Operational honesty"). Use it for trend and venue
  comparison, never as an absolute latency claim.
- Percentiles come from Prometheus histogram buckets; resolution is bounded
  by the bucket edges in §1.1.
- The dashboard and Prometheus views are observation-only; nothing in
  `arb/monitoring/` can influence detection or (future) execution — the
  dashboard's crash isolation is tested behavior by contract, and metrics
  observation sits outside every decision.
- Replay runs emit **no** Prometheus metrics by design; determinism evidence
  lives in the record sink output (byte-identical files), not in gauges.
