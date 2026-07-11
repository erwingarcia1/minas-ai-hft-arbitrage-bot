# Test Strategy

> **Status: IMPLEMENTED** for the unit, property-based, recording/replay and
> report tiers described in present tense below (CLAUDE.md §39, Phase 1 /
> Milestone 1 scope); the contract-vs-live, integration, chaos-harness and
> soak tiers are **SPECIFIED — NOT IMPLEMENTED** and are marked as such with
> the phase that unblocks them. Evidence baseline for this iteration:
> `pytest -q` → **144 passed** (verified in this workspace; see PROGRESS.md
> Iteration 2). No test reaches a network, reads a credential, or places an
> order — there is no order code to test (`arb/execution/` is empty).

## 1. How to run

```bash
pip install -e '.[dev]'    # includes hypothesis>=6.100
pytest -q                  # 144 tests, ~2s, fully hermetic (no network)
pytest -q tests/test_replay.py tests/test_properties.py   # focused subsets
```

pytest is configured in `pyproject.toml` (`asyncio_mode = "auto"`,
`testpaths = ["tests"]`). Shared helpers live in `tests/conftest.py`:
`make_settings()` (a strict two-venue `Settings`), `fill_book()` (fresh,
synced book), `ListSink` (in-memory record sink), `make_metrics()` (isolated
`CollectorRegistry` per test). There is no CI pipeline in this repository
yet; the suite is run locally per CLAUDE.md §42 step 5.

## 2. Suite inventory

| File | Tests | Subject |
|---|---:|---|
| `tests/test_instruments.py` | 31 | asset identity (USDT ≠ USDC), exact-LCM common step, `round_qty_for_pair` floor/clamp/minimums, registry load validation, detector rounding integration, all four venue metadata parsers (fixture-driven) |
| `tests/test_feeds_sync.py` | 15 | Binance snapshot-sync algorithm (buffered overlap, duplicates, gap resync, malformed-snapshot retry), Bybit v5 sequencing (`u` chain, `u==1` restart, gap resubscribe, subscribe chunking, snapshot deadline), OKX `seqId` chain, checksum verification, resync-storm connection recycle — all against recorded protocol fixtures via a `FakeWs` |
| `tests/test_book.py` | 15 | snapshot/delta/removal semantics, crossed-book detection, VWAP depth walk, insufficient depth → `None`, CRC32 checksum payloads (interleaving, uneven sides, trailing zeros, no scientific notation, top-25 cap) |
| `tests/test_replay.py` | 12 | `ReplayClock` monotonicity/reset, **live-vs-replay byte equality**, replay-vs-replay determinism, recorded-clock durations, sample-sweep cadence, out-of-config skips, config-divergence warnings, truncated-recording flush, malformed-line `ReplayError`, multi-session restart semantics |
| `tests/test_spread.py` | 9 | detector lifecycle: open, decay-close, staleness gate, unsynced gate, insufficient-depth close, venue-unusable force-close, shutdown flush, both-direction samples, slippage priced into edge |
| `tests/test_recording.py` | 9 | tap completeness (every mutation and sync transition, in order, literal levels), raising tap contained, journal header/events/end shape, disable-after-10-failures, write failures never raise into the feed |
| `tests/test_report.py` | 8 | persistence floor, ranking by capturable profit, aggregation, junk-record counting, empty dataset handling, THEORETICAL labelling in JSON output |
| `tests/test_properties.py` | 6 | hypothesis property tests — see §4 |
| `tests/test_fees.py` | 6 | exact gross/net edge math, fees-exceed-gross negative case, non-naive compounding, input validation |
| `tests/test_config.py` | 6 | strict YAML validation: shipped config valid; missing file, unknown keys, bad symbol format, single venue, invalid YAML all fatal |
| `tests/test_slippage.py` | 4 | buy/sell-side depth-walk slippage, top-level no-slippage case, insufficient depth is `None` not an approximation |
| `tests/test_staleness.py` | 2 | venue usable/unusable transitions, per-book degradation backstop, never-updated books unusable from boot |

## 3. Mapping to §39 unit-test requirements

| §39 unit topic | Where covered |
|---|---|
| Fee calculations | `tests/test_fees.py`; monotonicity in `tests/test_properties.py` |
| Depth calculations, VWAP calculations | `tests/test_book.py` (`vwap_for_qty`), `tests/test_slippage.py` |
| Precision, rounding | `tests/test_instruments.py` (`common_qty_step`, `round_qty_for_pair`, detector rounding); property test 3 below |
| Opportunity profitability | `tests/test_spread.py` (net-of-fees threshold gating), `tests/test_fees.py` |
| State transitions (Phase-1 scope: opportunity lifecycle, book sync state) | `tests/test_spread.py`, `tests/test_feeds_sync.py`, `tests/test_staleness.py` |
| Inventory reservations, fill matching, residual exposure, risk limits, profit accounting | **SPECIFIED — NOT IMPLEMENTED** (Phase 2): the code under test does not exist yet |

Additions beyond the §39 list that Milestone 1 required: recording-tap
completeness (`tests/test_recording.py`), replay determinism and
live-equivalence (`tests/test_replay.py`), viability-report aggregation
(`tests/test_report.py`), and strict config validation
(`tests/test_config.py`).

## 4. Property-based tests (§39 numbered list)

`tests/test_properties.py` uses hypothesis over randomized prices, fees,
quantities and venue steps. Mapping to §39's ten required properties:

| # | §39 property | Status | Evidence |
|---|---|---|---|
| 1 | Increasing fees cannot increase expected profit | **PROVEN** | `TestFeeMonotonicity::test_increasing_either_fee_cannot_increase_net_edge`; also `test_net_never_exceeds_gross` |
| 2 | Increasing slippage cannot increase expected profit | **PROVEN** | `TestSlippageMonotonicity::test_worse_execution_on_either_leg_cannot_increase_net_edge` (worse VWAP on either leg) |
| 3 | Rounding cannot create money | **PROVEN** | `TestRoundingCannotCreateMoney`: rounded qty never exceeds raw qty and is an exact `Fraction`-verified multiple of both venues' steps; `common_qty_step` is a multiple of both |
| 4 | Reserved funds cannot be double spent | SPECIFIED — NOT IMPLEMENTED (Phase 2: no reservation code) |
| 5 | Actual sold quantity cannot exceed available base inventory | SPECIFIED — NOT IMPLEMENTED (Phase 2: no inventory code) |
| 6 | Duplicate events cannot create duplicate orders | SPECIFIED — NOT IMPLEMENTED (Phase 2+: no orders). Phase-1 analogue tested: duplicate feed events are ignored by sync chaining (`test_binance_applies_chain_and_ignores_duplicates`) |
| 7 | A timeout cannot authorize resubmission | SPECIFIED — NOT IMPLEMENTED (Phase 2+: no orders) |
| 8 | Net accounting remains balanced | SPECIFIED — NOT IMPLEMENTED (Phase 2: no PnL accounting) |
| 9 | More adverse execution cannot improve realized profit | SPECIFIED — NOT IMPLEMENTED (Phase 2: no realized profit); the theoretical-edge analogue is property 2 |
| 10 | An invalid book cannot authorize a trade | **PROVEN** (Phase-1 form: cannot produce an *evaluation*) | `TestInvalidBookCannotEvaluate`: any unsynced or stale book yields no evaluation and opens nothing; reinforced deterministically by `test_spread.py::test_stale_book_blocks_and_closes` / `test_unsynced_book_blocks` |

## 5. Milestone-1 acceptance evidence (§48.8)

The §48.8 criterion — *events can be replayed deterministically* — has two
pieces of evidence:

1. `tests/test_replay.py::TestLiveReplayEquivalence::test_replay_reproduces_live_records_exactly`
   drives books exactly the way the feeds do (apply → synced → evaluate)
   with the production recorder attached, then replays the journal and
   asserts record-list equality **and** `orjson.dumps` byte equality against
   the live detector output. This proves the journal captures everything the
   detector's decisions depend on.
2. PROGRESS.md Iteration 2: an 11,992-event synthetic 4-venue session
   recorded through the production journal and replayed twice via
   `python -m arb replay` produced identical SHA-256 output hashes.

Known live-vs-replay scheduling skews (staleness-monitor tick, sampler start
skew, µs wall-read skew) are documented in `docs/MARKET_REPLAY.md` §5.

## 6. Tiers not yet built (SPECIFIED — NOT IMPLEMENTED)

| §39 tier | Blocked by | Phase |
|---|---|---|
| **Exchange contract tests** (one behavioural suite all adapters pass, exercised against real endpoints) | Partially approximated today: all four feeds implement the common `WsFeed` interface and their sync protocols are fixture-tested in `tests/test_feeds_sync.py`, but there is no single parameterized contract suite and **no live-endpoint validation — this sandbox blocks exchange egress** (PROGRESS.md "Known constraint"). Live validation must run from an operator machine. | Phase 1 hardening / Milestone-1 review follow-up |
| **Integration tests** (authentication, clock sync, private streams, order submission, cancellation, reconciliation, fee/balance retrieval) | No credentials are read and no private-stream or order code exists; also blocked by sandbox egress. | Phase 2 (paper: none needed) → Phase 3 (shadow: read-only private streams) → live |
| **Chaos tests** (systematic injection: WS failure, REST timeout, sequence gap, duplicate/out-of-order fill, one-leg rejection, process crash, clock drift, rate limit, key revocation) | No injection harness exists. In-process failure cases *are* unit-tested where the code exists: sequence gaps force resync (`test_binance_gap_forces_resync`, `test_bybit_gap_resubscribes`, `test_okx_sequence_break_resubscribes`), resync storms recycle connections, malformed snapshots retry, recorder write failures disable loudly, malformed journal lines raise, truncated recordings flush, restarts replay as session boundaries. Fill/order/crash-recovery chaos requires execution and persistence code that does not exist. | Phase 4 (per README phase plan); execution-facing cases from Phase 2 |
| **Long-running soak tests** (memory, connections, book integrity, journal growth, latency stability) | Requires multi-hour live feed runs from an operator machine with exchange access; impossible in this sandbox. | Phase 1 operator task (long observation runs are also the Milestone-1 data-gathering step) |

## 7. Policy

- Tests are hermetic and deterministic: controlled clocks (`ReplayClock` in
  `tests/test_replay.py`), isolated metrics registries, `tmp_path` journals,
  recorded protocol fixtures instead of network I/O.
- Per CLAUDE.md §43, deleting or weakening a valid test to obtain a passing
  build is prohibited; the §40 invariants that apply to Phase 1 (stale or
  invalid books cannot evaluate; no orders can exist; theoretical results
  labelled THEORETICAL in `arb/report/viability.py` output) are enforced by
  the tests named above, and every new invariant lands with its test in the
  same iteration.
- The failure-injection style is "loud, contained, counted": tests assert
  not only that failures are handled but that they are *visible* (metrics,
  warnings, `ReplayError` with file:line) — silent degradation is treated as
  a defect.
