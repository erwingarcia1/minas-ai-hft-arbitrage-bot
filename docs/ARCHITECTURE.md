# ARCHITECTURE.md — The Modular-Monolith Observer As Built

> **Status: IMPLEMENTED (Phase 1 / Milestone 1, CLAUDE.md §48).** Everything
> described in present tense below exists in this repository and is exercised
> by the test suite (121 passing tests, `pytest -q`). Items marked
> **SPECIFIED — NOT IMPLEMENTED** do not exist in code. There is **no
> order-placement code**: `arb/execution/__init__.py` is a docstring-only
> placeholder, no API keys are read, and nothing in this build can trade.

## 1. Shape of the system

One Python 3.11 process, one asyncio event loop (uvloop when importable —
`arb/__main__.py:_cmd_observe`), no database, no message broker, no threads in
the data path. This is the "modular monolith containing independently testable
modules" that CLAUDE.md §6 prescribes as the starting point; every module below
has its own unit tests and the boundaries between them are plain function
calls on the same loop.

Four read-only entry points (`python -m arb <cmd>`, `arb/__main__.py`):

| Command | Mode (CLAUDE.md §5) | What runs |
|---|---|---|
| `observe` | Mode 1 — market-data observation | feeds + books + detector + monitor + sampler + recorder + dashboard |
| `replay` | Mode 2 — historical replay | `arb/replay/engine.py` over recorded journals; same detector, `ReplayClock` |
| `report` | offline analysis | `arb/report/viability.py` over research logs |
| `instruments` | offline metadata refresh | `arb/instruments/fetch.py` against public REST endpoints |

There is no `--live` flag "because there is nothing it could enable"
(`arb/__main__.py` module docstring).

## 2. Module map

| Module | Responsibility | Key names |
|---|---|---|
| `arb/feeds/` | Native public-WS clients per venue; local L2 books | `WsFeed` (reconnect/backoff/heartbeat/idle-watchdog base, `base.py`); `BinanceFeed`, `BybitFeed`, `OkxFeed`, `BitgetFeed`; `OrderBook`, `BookRegistry` (`book.py`) |
| `arb/signals/` | Executable-spread detection and opportunity lifecycle | `SpreadDetector`, `EvalResult` (`spread.py`); `gross_edge_bps`, `net_edge_bps` (`fees.py`); `executable_vwap`, `slippage_bps` (`slippage.py`) |
| `arb/risk/` | Per-venue staleness monitoring; the Phase-2 kill-switch seam | `StalenessMonitor` (`staleness.py`), 100 ms tick |
| `arb/instruments/` | Per-venue precision/minimum metadata; asset identity; pair rounding | `InstrumentRegistry`, `InstrumentMeta`, `round_qty_for_pair`, `common_qty_step`, `assets_equivalent` (`registry.py`); `fetch_all` (`fetch.py`) |
| `arb/recording/` | Deterministic book-event journal (replay input) | `BookEventRecorder`, `header_record`, `SCHEMA_VERSION` (`recorder.py`) |
| `arb/replay/` | Deterministic re-run of recorded sessions | `run_replay`, `ReplayStats`, `ReplayError` (`engine.py`); `Clock`, `SystemClock`, `ReplayClock` (`clock.py`) |
| `arb/report/` | Ranked viability report (§48.10) | `build_report`, `render_table`, `PairClipStats` (`viability.py`) |
| `arb/accounting/` | JSONL research log (opportunities + spread samples) | `OpportunityLog` (`opportunities.py`), one file per UTC day |
| `arb/monitoring/` | Prometheus metrics, JSON logs, terminal dashboard | `Metrics` (`metrics.py`); `JsonFormatter` (`logging.py`); `run_dashboard` (`dashboard.py`, 4×/s, cosmetic by contract) |
| `arb/config/` | YAML → strictly validated settings; fatal at boot on any violation | `load_settings`, `ConfigError` (`loader.py`); `Settings` et al. in `arb/models.py` |
| `arb/execution/` | **Intentionally empty.** Reserved seam for Phase 2 (paper) / Phase 3 (live, double-gated) | docstring only |
| `arb/models.py` | Shared pydantic models | `VenueName`, `Settings`, `OpportunityRecord`, `SpreadSample`, `StrictModel` (unknown keys fatal) |

## 3. Data flow

### 3.1 Frame path (per WebSocket message)

```
WS frame
  └─ WsFeed.run() read loop                      arb/feeds/base.py
       stamps wall_ns + mono_ns, increments arb_ws_messages_total
  └─ <venue>.handle_raw()                        arb/feeds/{binance,bybit,okx,bitget}.py
       parse; venue-native integrity protocol (sequence chaining,
       CRC32 checksum where the venue provides one); gap/mismatch ⇒ resync,
       never a silent apply
  └─ OrderBook.apply_snapshot / apply_deltas     arb/feeds/book.py
       Decimal levels keyed by the literal exchange strings
       ├─ tap ──► BookEventRecorder.on_book_event   arb/recording/recorder.py
       │          buffered JSONL journal (flush every 500 events or 1 s);
       │          a raising tap is contained — recording failures degrade
       │          recording, never market-data processing
       └─ on_update(venue, symbol) callback  ◄── REGION-SPLITTING SEAM
  └─ SpreadDetector.on_book_update → evaluate(symbol)   arb/signals/spread.py
       usable books only (synced AND age ≤ risk.staleness_ms);
       for each ordered venue pair × configured clip:
         round_qty_for_pair (floor to a step valid on BOTH venues, check
         minimums) → vwap_for_qty depth walk both legs → net_edge_bps
         (both taker fees) → open / extend / close opportunity
  └─ OpportunityLog.write                        arb/accounting/opportunities.py
       open→close records + samples, JSONL, one file per UTC day
```

The `tap` on `OrderBook` is the single seam every mutation and sync transition
flows through, so recording completeness is structural, not per-feed
(`arb/feeds/book.py` and `arb/recording/recorder.py` docstrings). File order
equals application order because there is a single loop and a single writer.

### 3.2 Periodic loops

| Loop | Cadence | Purpose |
|---|---|---|
| `StalenessMonitor.run` (`arb/risk/staleness.py`) | 100 ms (`_TICK_S`) | per-book age gauges; on a venue usable→unusable transition calls `detector.on_venue_unusable` (force-closes affected opportunities); per-book fresh→stale backstop calls `detector.evaluate(symbol)` |
| `_sampler` (`arb/__main__.py`) | `monitoring.spread_sample_interval_s` (default 30 s) | unconditional `evaluate(symbol, emit_sample=True)` — records negative edges too, and acts as a lifecycle backstop when no book ticks |
| `run_dashboard` (`arb/monitoring/dashboard.py`) | 250 ms | read-only rich TUI; any exception stops the dashboard, not the collection |
| Prometheus scrape server | on demand | `prometheus_client` daemon thread; never touches the event loop (`Metrics.start_server`) |

### 3.3 Replay path (Mode 2)

`run_replay` (`arb/replay/engine.py`) reads `books-*.jsonl`, re-applies each
event to fresh books, and drives the **same** `SpreadDetector` under a
`ReplayClock` advanced only by recorded stamps. Determinism contract (§35):
same files + same config ⇒ byte-identical records (asserted by a live-vs-replay
equivalence test in `tests/test_replay.py`). Config divergence is a supported
sensitivity run and is warned about against the recording header, never hidden.

## 4. Concurrency model

Single event loop; ownership replaces locking:

- Each `OrderBook` is mutated only by its owning feed task and read by the
  detector and monitor **on the same loop** — "not thread-safe by design: all
  mutation happens on the single asyncio event loop" (`arb/feeds/book.py`).
  There are no locks, and none are needed, because nothing yields mid-mutation.
- Tasks at runtime: one `feed.run(stop)` task per venue (each spawning a
  heartbeat/watchdog subtask, plus per-symbol REST snapshot tasks on Binance
  during resync), the staleness monitor, the sampler, and optionally the
  dashboard.
- **Failure asymmetry** (`arb/__main__.py`): a feed task dying degrades to one
  venue offline — its books go permanently unsynced, which the staleness
  monitor makes loudly visible while other venues keep collecting. The core
  tasks (monitor, sampler) are load-bearing: the process `await`s them with
  `FIRST_EXCEPTION` and shuts everything down if one dies, because "running
  blind is worse than not running".
- Shutdown: SIGINT/SIGTERM set a stop event; feeds close, open opportunities
  flush with reason `shutdown`, the research log closes, and the recorder
  appends a `recording_end` stamp *after* the flush so replay can close
  leftovers at the exact same clock.

## 5. Seams reserved for later phases (marked in code today)

### 5.1 Region-splitting seam (Phase 4)

`grep -rn "REGION-SPLITTING"` finds three markers:

| Location | Boundary |
|---|---|
| `arb/feeds/base.py` (`UpdateCallback`) | the `on_update(venue, symbol)` callback type is the line that would become an inter-region bus publish |
| `arb/__main__.py` (feed wiring) | feeds publish via a plain callback; multi-region, each feed process runs next to its venue and the detector consumes the bus where the execution engine lives |
| `arb/__init__.py` (package docstring) | everything upstream of the callback (feeds → books) is venue-local |

Rule (see `docs/DEPLOYMENT.md` §4): this callback stays the *only* coupling
between feed-side and decision-side code, so splitting it later is a transport
change, not a rewrite. Per §6, no networked component enters the hot path
without benchmarks justifying it.

### 5.2 Kill-switch seam (Phase 2)

`arb/risk/staleness.py` is the attachment point: today the `on_unusable` hook
force-closes theoretical opportunities within one 100 ms tick; in Phase 2 the
same transition will additionally halt signal→execution flow, cancel open
orders, and latch a halted state requiring manual reset (module docstring,
"KILL-SWITCH SEAM"). The kill switches themselves are **SPECIFIED — NOT
IMPLEMENTED** (CLAUDE.md §30); only the seam and the staleness gate exist.

### 5.3 Execution seam (Phase 2/3)

`arb/execution/` is empty by design. Phase 3 live flow is specified to require
both `--live` and `ARB_CONFIRM_LIVE_TRADING` (docstrings in
`arb/execution/__init__.py` and `arb/__main__.py`). **SPECIFIED — NOT
IMPLEMENTED.**

## 6. Mapping to CLAUDE.md §6 (technology architecture)

| §6 prescription | Status here |
|---|---|
| "First preserve any sound existing architecture" | Followed: the pre-existing Python asyncio observer (Iteration 1, commit `e5d762d`) was preserved and extended, not rewritten (PROGRESS.md "Language decision"). |
| Rust + Tokio latency-critical engine | **SPECIFIED — NOT IMPLEMENTED.** Deliberately deferred until observation data justifies execution work — decision record `docs/DECISIONS.md` D-002. Python is acceptable for Modes 1–2; see `docs/HOT_PATH_ARCHITECTURE.md` for the honest §13 accounting. |
| No binary floating point for money | Books store `Decimal` parsed from the exchange's literal strings (`arb/feeds/book.py`); pair rounding uses `Decimal`/`Fraction` exactly (`arb/instruments/registry.py`). Signal math reads float64 views — a documented, deliberate concession for *research* edge numbers only; no balance, fee-settlement, or realized-profit arithmetic exists in this build to be endangered by it. |
| Scaled integers in the hot path | **NOT IMPLEMENTED** in Phase 1; specified for the execution engine (D-002). |
| Append-only event journal | Implemented twice: research log (`arb/accounting/opportunities.py`) and the deterministic book-event journal (`arb/recording/recorder.py`). |
| PostgreSQL | **NOT USED** in Phase 1 — no state requires it; JSONL journals suffice for research data. |
| Redis "only when measurement demonstrates a need" | Not used; no need demonstrated. |
| Prometheus-compatible metrics | Implemented (`arb/monitoring/metrics.py`, explicit `CollectorRegistry`). |
| Structured logging | Implemented (`arb/monitoring/logging.py`, one JSON object per line). |
| Docker, Linux | Linux assumed; Docker **NOT IMPLEMENTED** (see `docs/DEPLOYMENT.md`). |
| Dashboard: TypeScript/React | **NOT IMPLEMENTED**; a rich terminal dashboard exists instead (`arb/monitoring/dashboard.py`), sufficient for Mode 1. |
| Modular monolith; no hot-path microservices without benchmarks | Followed; the region-splitting seam (§5.1 above) is the pre-marked line for when benchmarks exist. |

## 7. What this architecture cannot do (by construction)

- Place, amend, or cancel orders on any venue — no such code exists.
- Read API credentials — the only environment read anywhere is the documented
  `trust_env=True` proxy exception in the Binance REST snapshot client
  (`arb/feeds/binance.py`; see README "Known limitations").
- Move funds — no transfer code exists by policy (README key checklist).
- Claim latency performance — feed-latency histograms include local↔exchange
  clock skew and are labelled trend indicators (`WsFeed.record_feed_latency`).

Everything past Mode 2 — paper execution, shadow trading, risk engine,
inventory, hedging — is **SPECIFIED — NOT IMPLEMENTED** and lives in the other
`docs/` files with the same status discipline.
