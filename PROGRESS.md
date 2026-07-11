# PROGRESS.md — Durable Project Memory

> Read `CLAUDE.md` first (governing spec), then this file, then
> `docs/FEATURE_MATRIX.md`. This file is updated at the end of every
> iteration per CLAUDE.md §42 step 10. Newest iteration first.

## Current state (summary)

- **Phase / mode:** Milestone 1 (§48) — public market data only. **No
  order-placement code exists anywhere in this repository.** `arb/execution/`
  is empty by design; no API keys are read; nothing can trade.
- **Language decision:** Python 3.11 asyncio observer (existing sound
  architecture, preserved per §6 "first preserve any sound existing
  architecture"). The Rust latency-critical engine is deferred until the
  observation data justifies execution work — see docs/DECISIONS.md D-002.
- **Venues:** Binance (global), Bybit (v5 spot), OKX (global), Bitget (v2
  spot) — public WebSocket L2 feeds with venue-native integrity protocols
  (sequence chaining, CRC32 checksums where provided).
- **Known constraint:** this development sandbox blocks egress to exchange
  endpoints; live-connection behaviour is unit/contract tested against
  recorded protocol fixtures, and must be validated end-to-end from an
  operator machine (see README "Known limitations").

## Iteration log

### Iteration 2 — Milestone 1 completion (this iteration)

- **Objective:** close the §48 gaps: governing docs (CLAUDE.md, PROGRESS.md,
  docs/ tree per §7), deterministic market-data recording (§35), deterministic
  replay engine (§48.8), canonical instrument registry with precision/rounding
  in the opportunity math (§10, §48.1/.6), ranked viability report (§48.10).
- **Feature IDs:** M1-DOCS, M1-REC, M1-REPLAY, M1-INSTR, M1-REPORT
  (see docs/FEATURE_MATRIX.md).
- **Risk classification:** P2 core functionality (no P0/P1 defects known at
  iteration start; baseline 57 tests green).
- **Acceptance criteria:**
  - Recording: every applied book mutation (snapshot/delta/clear) and sync
    transition journaled with exchange/wall/mono timestamps; config-gated;
    write failures visible in metrics, never fatal to collection.
  - Replay: same recorded stream + same config ⇒ byte-identical opportunity
    records; replay of a synthetic stream matches live-path detector output.
  - Instruments: per-venue tick/step/min-qty/min-notional metadata; clip
    quantities rounded DOWN to a step valid on both legs; violating minimums
    marks the clip unexecutable; USDT and USDC are never treated as equal.
  - Report: `python -m arb report` ranks (symbol, buy venue, sell venue, clip)
    by persistence-filtered estimated capture; transparent formula.
- **Work completed:**
  - `CLAUDE.md` (master prompt saved per §51), `PROGRESS.md`, full `docs/`
    tree (26 documents, each with an explicit status header; spec-only docs
    authored from CLAUDE.md sections, code-grounded docs authored against
    the implementation by independent subagents).
  - Recording (`arb/recording/`): every book mutation and sync transition
    journaled via a structural tap on `OrderBook` (`arb/feeds/book.py`);
    literal level strings; exchange/wall/mono stamps; session header with
    config AND instrument-metadata snapshot; explicit `eval` events at live
    evaluation points (evaluation is not inferable from mutations — feeds
    do not evaluate state that failed integrity checks); teardown unsyncs
    excluded via `detach()`; `recording_end` stamp; buffered writer that
    disables itself loudly after 10 consecutive failures. Control-flow
    events are journaled at last data-frame stamps so every recorded
    timestamp originates from feed data (single time source per journal).
  - Replay (`arb/replay/`): `Clock` abstraction threaded through the
    detector (`SystemClock` live, `ReplayClock` in replay); engine re-applies
    events and evaluates ONLY where live evaluated (journaled eval events,
    the REAL `StalenessMonitor.tick` on its 100ms grid, sample sweeps on the
    recorded-cadence grid — the live sampler is now grid-anchored and
    drift-free); treats multiple headers as restarts, flushes truncated
    recordings, warns on every config/instrument divergence (sensitivity
    runs). CLI: `python -m arb replay`.
  - Instruments (`arb/instruments/`): canonical registry with per-venue
    tick/step/min-qty/min-notional; venue-symbol transform cross-checked at
    load; exact-lcm common step; floor-only rounding with max-clamp-then-
    check-mins ordering; suspended instruments excluded; USDT≠USDC as a
    tested invariant; static `config/instruments.yaml` snapshot (UNVERIFIED,
    conservative) + `python -m arb instruments fetch` refresh with
    fixture-tested parsers for all four venues.
  - Detector: clip quantities rounded to venue rules before pricing; min
    notionals gated after VWAPs; injectable clock; suspended exclusion.
  - Report (`arb/report/`): ranked viability report, persistence-floored,
    transparent score (sum of open-time est. profit over persistent
    opportunities), THEORETICAL labelling baked into output. CLI:
    `python -m arb report [--json]`.
- **Files changed:** arb/feeds/book.py, arb/signals/spread.py,
  arb/models.py, arb/monitoring/metrics.py, arb/__main__.py,
  config/config.yaml, config/instruments.yaml, pyproject.toml (hypothesis),
  new packages arb/{recording,replay,instruments,report}/, tests/
  {test_recording,test_replay,test_instruments,test_report,test_properties}.py,
  README.md, CLAUDE.md, PROGRESS.md, docs/ (26 files).
- **Tests run:** `pytest -q` → **144 passed** (57 pre-existing + 87 new),
  including property-based tests (fee/slippage monotonicity, rounding cannot
  create quantity, invalid book cannot evaluate) and a byte-exact
  live-vs-replay equivalence test.
- **Simulation evidence:** seeded synthetic 4-venue × 2-symbol session
  (5 min, 200ms cadence, scripted dislocations + one feed outage; 23,972
  applied events incl. journaled evaluation points) recorded through the production journal and replayed twice
  via the CLI → identical SHA-256 outputs; 64 opportunities, 717 samples,
  10 sample sweeps; ranked report produced; sensitivity replay at 15 bps
  threshold ran with the correct "not a reproduction" warning. Simulation
  scripts live outside the repo (scratchpad) by design — they are test
  drivers, not product.
- **Review findings and corrections (all fixed before this commit):**
  Independent adversarial review (4 lenses, 25 findings, each verified by a
  separate refutation agent; 1 refuted) plus self-review. Confirmed and
  fixed:
  - **P1 replay-evaluated-rejected-state:** feeds set `synced=True` before
    checksum/crossed validation and replay evaluated after every mutation,
    pricing books live rejected. Fixed by validating BEFORE marking synced
    (okx/bitget/bybit), journaling explicit `eval` events at live
    evaluation points, and evaluating in replay only at eval events/monitor
    ticks/sweeps. Regression: `test_integrity_rejected_state_is_never_priced`.
  - **P1 teardown unsyncs journaled:** shutdown-cancellation unsyncs made
    replay close tail opportunities as `book_unusable` instead of live's
    `shutdown`. Fixed with `recorder.detach()` before feed cancellation.
  - **P1 instruments not in header:** replay with a refreshed
    `instruments.yaml` silently changed the money math. Fixed: header
    snapshots detector-consumed instrument fields; `_compare_header` warns
    (also now compares symbols/venues).
  - **P1 profit on configured clip:** `est_profit_usdt_open` used clip
    notional although rounding/max-qty shrink the executable quantity.
    Fixed: fee-explicit profit on the executable notional (qty × VWAPs).
  - **P1 report half-ingestion:** malformed opportunity records were partly
    counted into money stats AND reported skipped. Fixed:
    parse-all-fields-before-mutate.
  - **P1 replay lacked the venue-level monitor:** multi-symbol
    `venue_unusable` closes never happened in replay. Fixed: the real
    `StalenessMonitor.tick()` (extracted) runs on a deterministic 100ms
    grid inside replay. Test: `test_monitor_grid_closes_venue_wide`.
  - P2s: live sampler re-armed relative timers (unbounded drift vs replay's
    grid) → now absolute-grid anchored; `_price` rejections mislabeled as
    `insufficient_depth` → distinct `below_venue_minimums` reason; replay
    output opened append-mode with 1s-granularity default names (breaking
    hash-comparison workflows) → truncate + µs names + clean errors; sink
    failures were blamed on the journal → attributed correctly; runbook and
    matrix contradictions (subcommands, trust_env scope, test counts,
    missing Bitget protocol tests — 5 added) → corrected; self-review:
    `round_qty_for_pair` max-clamp could land below an already-checked
    minimum → clamp-first-then-check, with tests.
  - Documented (not code-fixed, deliberate): four bounded live-vs-replay
    fidelity gaps in docs/MARKET_REPLAY.md §5.
- **Status:** COMPLETE (this commit). Milestone 1 acceptance criteria have
  implementation + test evidence; the §48 "independent review before
  execution work" gate remains the next milestone's entry condition, and
  live end-to-end feed validation must run from an operator machine.

### Iteration 1 — Phase 1 observer (pre-existing, commit e5d762d)

- Native WS feeds for 4 venues; L2 books (Decimal-exact, sequence +
  checksum validated, staleness-stamped); executable-spread detector (VWAP
  depth walk per clip, net of both taker fees); opportunity open/close
  lifecycle with JSONL research log; staleness monitor; Prometheus metrics;
  rich terminal dashboard; strict YAML config; 57 unit tests.
- Evidence: `pytest -q` → 57 passed. No execution code; no credentials read.

## Risks remaining / assumptions

- Fee model covers taker bps in quote terms only (documented equivalence
  assumption in arb/signals/fees.py); account-specific tiers must be set in
  config by the operator.
- Feed-latency histograms include local↔exchange clock skew (trend signal,
  not absolute).
- Live end-to-end feed behaviour unvalidated from this sandbox (egress
  blocked); protocol logic is fixture-tested.

## Blockers

- None.

## Next best action

- Finish Iteration 2 acceptance criteria, then: independent review of
  Milestone 1 (§48 requires it before any execution work), then long
  observation runs from an operator machine to gather viability data before
  specifying Milestone 2 (paper execution).
