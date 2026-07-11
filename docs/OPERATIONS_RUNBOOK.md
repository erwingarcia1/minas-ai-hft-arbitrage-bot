# Operations Runbook

> **Status: PARTIALLY IMPLEMENTED — Phase-1/Milestone-1 operations only.**
> Sections 1–10 describe operating the code that exists today: four
> read-only subcommands — `observe` (four public WebSocket feeds, local L2
> books, spread detection, research + book-event recording), `replay`
> (deterministic re-run of a recording), `report` (ranked viability report),
> and `instruments fetch` (venue metadata refresh) — of which only `observe`
> is long-running. Section 11 is a clearly marked **FUTURE** specification
> of paper/shadow/live operations from CLAUDE.md §5, §30 and §44; none of
> that is implemented and nothing in this build can trade.

---

## 1. What this build is (and is not)

The primary mode is **observe** (CLAUDE.md §5 Mode 1). It connects to
the public market-data endpoints of Binance (global), Bybit (v5 spot), OKX
(global) and Bitget (v2 spot), maintains integrity-checked local order books,
detects theoretical cross-venue spreads net of taker fees, depth-implied
slippage and venue rounding rules, and records them for research — including
a deterministic book-event journal for replay.

Three further read-only commands operate on its outputs:

| Command | Purpose |
|---|---|
| `python -m arb replay --input data/recordings/books-*.jsonl` | Mode-2 deterministic re-run of a recorded session (same config ⇒ byte-identical records; changed config ⇒ sensitivity run, loudly warned) |
| `python -m arb report [--min-duration-ms 250] [--json out.json]` | ranked viability report over the research logs (§48.10) |
| `python -m arb instruments fetch` | refresh `config/instruments.yaml` from the venues' public REST endpoints |

Hard facts an operator must know:

- There is **no order-placement code**. `arb/execution/` is an empty package
  by design. There is no `--live` flag because there is nothing it could do.
- **No API keys are read.** No `.env` is needed. Public endpoints only.
- The process risks no capital; it risks only disk space, bandwidth, and
  drawing wrong research conclusions from misconfigured fees (see §3.2).

## 2. Installation

Requirements: Python 3.11+, Linux or macOS, outbound HTTPS/WSS to the four
exchanges. `uvloop` is used automatically if installed; otherwise the default
asyncio loop is used (a notice is printed to stderr).

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest -q                    # must pass before first run
python -m arb observe        # starts the observer
```

Network note: two code paths make public REST calls with `trust_env=True`
(so they work behind HTTP(S) proxies): the Binance feed's per-symbol depth
snapshot (`api.binance.com`, required by Binance's official book-sync
algorithm) and `python -m arb instruments fetch`
(`api.binance.com`, `api.bybit.com`, `www.okx.com`, `api.bitget.com`).
aiohttp will then also consult `~/.netrc` for those hosts — **do not keep
exchange credentials in `.netrc` for any exchange host**. Nothing else
reads the environment in Phase 1.

## 3. Configuration — `config/config.yaml`

One YAML file, strictly validated at boot (`arb/config/loader.py` +
`arb/models.py`). **Unknown keys are fatal**, as are: fewer than two enabled
venues, malformed symbols (must look like `BTC/USDT`), duplicate symbols, and
out-of-range values. A bad config prints `FATAL: <reason>` and exits with
code 1 before any connection is opened.

### 3.1 Keys

| Key | Type / default | Meaning |
|---|---|---|
| `symbols` | list, required | Canonical symbols, e.g. `BTC/USDT` |
| `signals.min_net_edge_bps` | float (default config: 3.0) | Net edge above which an opportunity "opens" |
| `signals.clip_sizes_usdt` | list of floats | Quote-notional clip sizes priced by depth-walk VWAP |
| `risk.staleness_ms` | int (500) | Book older than this ⇒ venue unusable for signals |
| `risk.max_position_usdt` | float | Validated now; **enforced only from Phase 2** (no positions exist) |
| `risk.max_daily_loss_usdt` | float | Validated now; **enforced only from Phase 2** |
| `venues.<name>.enabled` | bool | Enable/disable a venue (≥2 must be enabled) |
| `venues.<name>.fees.maker_bps` / `taker_bps` | float | **Your** fee tier in bps — see §3.2 |
| `monitoring.metrics_port` | int (9109) | Prometheus `/metrics` port |
| `monitoring.dashboard` | bool (true) | Terminal dashboard (auto-off when stdout is not a TTY) |
| `monitoring.spread_sample_interval_s` | float (30.0) | Period of unconditional spread samples to JSONL |
| `monitoring.log_dir` | path (`logs`) | JSON log files when the dashboard owns the terminal |
| `recording.enabled` | bool (false) | Deterministic market-data journal — see §3.3 |
| `recording.dir` | path (`data/recordings`) | Where recordings land |
| `instruments_file` | path or null (`config/instruments.yaml`) | Per-venue tick/step/minimum metadata; `null` disables rounding explicitly |
| `data_dir` | path (`data`) | JSONL research records |

### 3.2 Fee tiers — the single most important setting

The shipped defaults are **standard retail VIP0 with no token discounts**
(10 bps taker on most venues). The entire purpose of Phase 1 is measuring
edge net of *your* fees: at 10 bps × 2 legs a spread must clear ~20 bps after
depth-walk slippage to be worth anything. **Set `venues.*.fees` to your
actual account tier before trusting any net-spread data this system
produces.** All net-edge numbers in the dashboard, metrics and JSONL are
computed from these configured taker rates (`arb/signals/fees.py`; fees are
modelled in quote terms on both legs — a documented equivalence assumption).

### 3.3 Recording (deterministic market-data journal)

With `recording.enabled: true` (the shipped default), `python -m arb observe`
journals every applied book mutation and sync transition to
`<recording.dir>/books-<UTC>.jsonl` (`arb/recording/recorder.py`). These
files are the input to `python -m arb replay` — see `docs/MARKET_REPLAY.md`.
Disk cost is real (every delta, every venue/symbol) — budget for it or set
`recording.enabled: false` deliberately. Write failures are counted in
`arb_recording_errors_total` and after 10 consecutive failures the recorder
disables itself loudly while market-data collection continues; a recording
that ends without a `recording_end` stamp (crash) is still replayable.

## 4. Running the observer

```bash
python -m arb observe [--config config/config.yaml] [--no-dashboard] [--log-level INFO]
```

- The banner printed to stderr states the phase and that no order code
  exists. The subcommands are `observe`, `replay`, `report` and
  `instruments` (all read-only); anything else prints help and exits 2.
- Boot failures are fatal **before any connection is opened**: bad config
  (exit 1) and a metrics port already in use (`start_server` raises —
  running without observability is not acceptable by design).
- Run it for days. Short runs answer nothing; opportunity persistence and
  frequency are the research product.

Suggested long-run invocation (headless):

```bash
nohup python -m arb observe --no-dashboard >> observer.stderr.log 2>&1 &
```

### 4.1 Task-failure semantics (what dies takes what down)

| Component | On failure |
|---|---|
| One venue's feed task | Process continues degraded: that venue's books go permanently unusable (loudly visible via `arb_venue_usable=0` and an error log) until restart. Other venues keep collecting. |
| Staleness monitor | **Fatal — whole process stops.** Running blind is worse than not running. |
| Spread sampler | Fatal (core task, same `asyncio.wait` group). |
| Dashboard | Cosmetic by contract: it logs the exception and stops; data collection continues. |
| Research-log write | Non-fatal: logged + counted in `arb_record_write_errors_total`; the process keeps running. |

Note: feed tasks are designed never to raise — `run()` retries connect
errors forever with exponential backoff (0.5 s → 30 s cap, jittered). A feed
task actually dying indicates a bug and is logged as such.

## 5. The terminal dashboard

On by default in a TTY; refreshes 4×/s; disable with `--no-dashboard`. It is
read-only and cannot affect data collection. Three panels plus a header:

| Panel | Contents |
|---|---|
| Header | Phase banner + per-venue usable/unusable (green/red) from the staleness monitor |
| Venues / books | Per (venue, symbol): feed state (`disconnected`/`connecting`/`live`), synced yes/NO, book age ms, best bid/ask, bid/ask level counts |
| Best current net spreads | Per symbol, the best direction at the **smallest** configured clip: buy venue, sell venue, gross and net bps (green ≥ threshold, yellow > 0, red ≤ 0) |
| Recent opportunities | Last 10 closed records: net at open, max net, duration ms, close reason |

When the dashboard is active it owns stdout, so logs route to files (§7).

## 6. Prometheus metrics

Scrape `http://localhost:9109/metrics` (port from `monitoring.metrics_port`).
The exporter runs in its own daemon thread; scrapes never touch the event
loop. Exact series exported by `arb/monitoring/metrics.py`:

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `arb_ws_messages_total` | counter | venue | WebSocket frames received |
| `arb_ws_reconnects_total` | counter | venue | Reconnect events (incremented on every disconnect) |
| `arb_message_errors_total` | counter | venue | Frames that failed parsing/handling (connection survives) |
| `arb_book_resyncs_total` | counter | venue, symbol, reason | Book resyncs by cause — see §9 for reason values |
| `arb_feed_latency_ms` | histogram | venue | Exchange event time → local arrival, ms. **Includes clock skew: trend signal, not absolute truth.** Values outside (−1000, 60000) ms are discarded as skew-broken |
| `arb_book_age_ms` | gauge | venue, symbol | Age of last book update (−1 = never updated) |
| `arb_venue_usable` | gauge | venue | 1 when all the venue's books are synced and fresh |
| `arb_spread_gross_bps` | histogram | symbol, buy_venue, sell_venue | Gross executable spread per evaluation |
| `arb_spread_net_bps` | histogram | symbol, buy_venue, sell_venue | Net-of-fees executable spread per evaluation |
| `arb_opportunities_total` | counter | symbol, buy_venue, sell_venue | Closed opportunity records |
| `arb_opportunity_duration_ms` | histogram | — | How long opportunities persisted |
| `arb_records_written_total` | counter | — | Research records written |
| `arb_record_write_errors_total` | counter | — | Research record write failures (**alert on any increase**) |
| `arb_recording_events_total` | counter | venue | Book events journaled for replay (see §3.3) |
| `arb_recording_errors_total` | counter | — | Journal write failures |

Alerting suggestions for long observation runs: `arb_venue_usable == 0` for
> 5 min; any increase in `arb_record_write_errors_total`; sustained increase
rate of `arb_book_resyncs_total`; `arb_ws_reconnects_total` climbing faster
than a few per hour per venue.

## 7. Logs

Structured JSON, one object per line: `ts` (ISO-8601 UTC ms), `level`,
`logger`, `msg`, plus contextual fields (`venue`, `symbol`, `reason`, …) and
`exc_info` with the traceback on exceptions. Serialization failures are
stringified, never raised — logging cannot take the process down.

| Mode | Destination |
|---|---|
| Dashboard active (TTY) | `<monitoring.log_dir>/arb-YYYYMMDD.log` (UTC day in filename; stdout belongs to the dashboard) |
| `--no-dashboard` / non-TTY | stderr as JSON lines |

The log file is chosen at process start; a run crossing UTC midnight keeps
writing to the file named for its start day. Third-party `websockets` and
`aiohttp` debug noise is suppressed to INFO. No log rotation/retention is
built in — manage `logs/` (and `data/`) with your own logrotate/cron policy.

## 8. Data outputs — `data/opportunities-YYYYMMDD.jsonl`

Append-only JSONL in `data_dir`, one file per UTC day, rotated on write.
Writes are flushed per record; a crash loses at most the in-flight line.
Two record types, discriminated by `type` (both carry `schema_version: 1`):

- **`type=opportunity`** — one full open→close lifecycle of a theoretical
  opportunity. Fields: `symbol`, `buy_venue`, `sell_venue`, `clip_usdt`,
  `qty_base`, `opened_at`, `closed_at`, `duration_ms`, `gross_bps_open`,
  `net_bps_open`, `max_net_bps`, `mean_net_bps`, `samples`, `buy_vwap_open`,
  `sell_vwap_open`, `est_profit_usdt_open`, `threshold_bps`, `close_reason`.
  `est_profit_usdt_open` is **theoretical** (clip × net bps at open); per
  CLAUDE.md §34 it must never be presented as realized profit.
- **`type=spread_sample`** — unconditional periodic sample (every
  `spread_sample_interval_s`) per symbol/direction/clip, **including negative
  edges**: `ts`, venues, `clip_usdt`, `qty_base`, both VWAPs, `gross_bps`,
  `net_bps`, both book ages. This is the dataset for "does positive edge
  exist at my fee tier at all".

Opportunity `close_reason` values: `edge_below_threshold` (normal decay),
`insufficient_depth` (book too thin for the clip), `book_unusable` /
`venue_unusable` (integrity/staleness gate — see §9), `shutdown` (flush on
stop; treat the duration as right-censored, not a real lifetime).

## 9. Interpreting resyncs and staleness

A **resync** means a venue's book failed an integrity check and was rebuilt
rather than trusted (CLAUDE.md §11: invalidate after a gap, rebuild before
reuse). Each one logs a WARNING (`"book resync"` with venue/symbol/reason)
and increments `arb_book_resyncs_total`. Reason values in the current code:

| Reason | Venue(s) | Meaning |
|---|---|---|
| `connect` | Binance | Normal initial snapshot sync on (re)connect — expected, not a problem |
| `sequence_gap` | Binance, Bybit, OKX | Update-ID/seq chain broke; book invalidated and resynced |
| `checksum_mismatch` | OKX, Bitget | CRC32 book checksum failed |
| `crossed_book` | Bybit, Bitget | Best bid ≥ best ask after apply — internal inconsistency |
| `buffer_overflow`, `buffer_gap` | Binance | Snapshot-sync buffering anomalies during initial sync |

Operational interpretation:

- **Occasional resyncs are normal** (venue restarts, packet loss). A resync
  marks the book unsynced; signals exclude it until it is rebuilt, and any
  open opportunity touching it closes with `book_unusable`.
- **Resync storms are guarded**: repeated back-to-back in-band resyncs on
  OKX/Bitget recycle the whole connection instead of spamming sub/unsub
  against venue rate limits. Every in-band resubscribe also arms a 10 s
  snapshot deadline — no snapshot ⇒ the socket is recycled.
- **Idle watchdog**: no frame for 30 s force-closes the socket so the
  reconnect loop takes over; a half-open TCP connection must never keep
  stale books looking alive.
- **Staleness**: every 100 ms the monitor checks each book against
  `risk.staleness_ms` (default 500 ms). `venue <name> became UNUSABLE` in the
  logs plus `arb_venue_usable=0` means stale-or-unsynced books; no signal may
  reference that venue until it recovers (`became usable`). Persistent
  staleness on one venue with healthy others usually means local network
  trouble toward that venue's region, or venue-side degradation — treat as an
  incident per `docs/INCIDENT_RESPONSE.md` IC-1/IC-2.

## 10. Graceful shutdown

Send SIGINT (Ctrl-C) or SIGTERM. The stop event propagates and, in order:
feeds close, all tasks are cancelled and awaited, every still-open
theoretical opportunity is flushed to JSONL with `close_reason="shutdown"`,
the research log is closed, and `observer stopped` is logged. This makes the
JSONL free of phantom open intervals. A SIGKILL/crash skips the flush —
open opportunities at that moment are simply absent from the log (writes up
to the crash are already flushed to disk per record).

---

## 11. FUTURE OPERATIONS — **SPECIFIED, NOT IMPLEMENTED**

Everything below is specification from CLAUDE.md for later phases. None of
it exists in code; there is no way to perform these procedures today.

### 11.1 Operating modes ladder (CLAUDE.md §5)

| Mode | Name | Places orders? | Status |
|---|---|---|---|
| 1 | Market-data observation | No | **Implemented (this runbook)** |
| 2 | Historical replay | No | **Implemented** (`python -m arb replay`, §1; docs/MARKET_REPLAY.md) |
| 3 | Paper trading | No (simulated) | NOT IMPLEMENTED — Milestone 2 (§49) |
| 4 | Shadow trading | No (hypothetical orders, read-only keys) | NOT IMPLEMENTED — Milestone 3 (§50) |
| 5 | Limited live trading | Yes (minimal capital) | NOT IMPLEMENTED — requires §44 qualification |
| 6 | Qualified live trading | Yes | NOT IMPLEMENTED |

Each mode must be qualified before the next is attempted. Defaults are and
must remain `MARKET_DATA_ONLY=true`, `PAPER_TRADING=true`,
`LIVE_TRADING=false` (§3). When Phase 3 introduces live capability, live
runs will require BOTH a `--live` flag AND `ARB_CONFIRM_LIVE_TRADING` in the
environment, plus every §3 authorization step — no frontend control may
independently enable live trading.

### 11.2 Kill-switch operations (CLAUDE.md §30) — NOT IMPLEMENTED

Phase 2 must implement kill switches (global, per-venue, per-asset,
per-symbol, per-strategy, stablecoin, market-data, latency, daily-loss,
residual-exposure, reconciliation, repeated-rejection, security-event). The
staleness monitor's `on_unusable` hook is the designed attachment seam. On
activation a switch must: block new cycles, preserve emergency hedging,
cancel appropriate open orders, reconcile, record the cause, notify
operators, and **require authorized restart** — an operator will never
"just restart" a tripped system. Operator duties and restart authorization
will be specified in `docs/RISK_ENGINE.md` and `docs/INCIDENT_RESPONSE.md`.

### 11.3 Live-trading qualification gates (CLAUDE.md §44) — NOT IMPLEMENTED

Before any live order, measurable thresholds must be defined and met for:
observation/paper/shadow durations, recorded event counts, qualified
opportunity counts, book error rate, false-opportunity rate, rejection rate,
fill-mismatch rate, hedge failure rate, reconciliation error rate, latency
and residual-duration maxima, simulated drawdown, and net result after all
modeled costs. Profitability alone is insufficient. Live operation must
begin with one approved symbol, two approved exchanges, minimal capital,
strict daily-loss and residual limits, and immediate operator alerts. API
keys must be trade-only, withdrawal-disabled, IP-whitelisted (see README
checklist); the system must reject withdrawal-enabled credentials (§3, §40.19).
