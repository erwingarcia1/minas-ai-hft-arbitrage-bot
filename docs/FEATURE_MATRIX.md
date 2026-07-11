# Feature Matrix

> **Status: the central status table for this repository.** `PROGRESS.md`
> points here; CLAUDE.md §7 requires every feature to carry requirement,
> risk classification, acceptance criteria, tests, reviewer, benchmark and
> status. **Milestone 1 (§48) rows are IMPLEMENTED with test evidence;
> every Milestone 2/3 row is SPECIFIED — NOT IMPLEMENTED.** There is no
> order-placement code in this repository: `arb/execution/` is a
> docstring-only placeholder and no credentials are read.

## Conventions

- **Risk class** uses the CLAUDE.md §42 priority taxonomy (P0 = a defect
  here would be a P0 item: accounting error, stale-book trading, unsafe
  activation; P1 = adapter/book-integrity/reconciliation defect class;
  P2 = core functionality; P3 = dashboard/non-critical).
- **Tests/Evidence:** the full suite is `pytest -q` → **144 passed**
  (verified in this workspace, 2026-07-11, after adversarial-review fixes); per-row cells name the specific
  files. "Sim evidence" = the seeded synthetic 4-venue session in
  `PROGRESS.md` iteration 2 (23,972 events, replayed twice → identical
  SHA-256 outputs).
- **Reviewer ¹:** an internal adversarial review (4 lenses + refutation
  pass) ran before the Milestone 1 commit (`PROGRESS.md` iter. 2), and one
  self-review finding (`round_qty_for_pair` clamp ordering) was fixed with
  tests. The **independent review required by §48 before any execution work
  has NOT happened** — it is the open gate below. Every M1 row therefore
  reads "independent review pending".
- **Benchmark ²:** no accepted performance baseline exists yet.
  `docs/BENCHMARK_STRATEGY.md` defines the plan; no latency or throughput
  claims are made anywhere in this matrix. All M1 cells are TBD².
- **Sandbox caveat ³:** this repository was built where egress to exchange
  endpoints is blocked. Feed protocol logic is fixture-tested; live
  end-to-end behaviour must be validated from an operator machine
  (README "Known limitations").

## Milestone 1 (§48) — public market data only

| Feature ID | Requirement | Risk | Acceptance criteria | Tests / Evidence | Reviewer | Benchmark | Status |
|---|---|---|---|---|---|---|---|
| M1-FEED-BINANCE | §48.1, §9 (public subset), §11 | P1 | Diff-depth stream (`<sym>@depth@100ms`) synced by the official `U`/`u`/`lastUpdateId` algorithm, applied to live AND buffered events; gap ⇒ REST snapshot resync | `tests/test_feeds_sync.py::test_binance_*` (5 tests: stale-drop/overlap, chaining, duplicates, gap resync, malformed-snapshot retry) | pending independent¹ | TBD² | **IMPLEMENTED**³ |
| M1-FEED-BYBIT | §48.1, §9, §11 | P1 | `orderbook.50` (v5 spot) `u`-continuity; `u==1` service-restart rule; gap ⇒ resubscribe; snapshot deadline armed on resubscribe | `tests/test_feeds_sync.py::test_bybit_*` (6 tests) | pending independent¹ | TBD² | **IMPLEMENTED**³ |
| M1-FEED-OKX | §48.1, §9, §11 | P1 | `books` channel `seqId`/`prevSeqId` chain; legacy non-zero checksums still verified; resync storm recycles the connection | `tests/test_feeds_sync.py::test_okx_*` (4 tests) | pending independent¹ | TBD² | **IMPLEMENTED**³ |
| M1-FEED-BITGET | §48.1, §9, §11 | P1 | v2 `books` CRC32 checksum verified on **every** message carrying one; mismatch ⇒ resubscribe; crossed-book guard; update-before-snapshot dropped | `tests/test_feeds_sync.py::test_bitget_*` (5 tests: valid-checksum apply, checksum-mismatch resubscribe + armed deadline, update-before-snapshot drop, crossed-book resync, resync-storm recycle); checksum construction: `tests/test_book.py` (6 checksum tests). The channel exposes no documented sequence chain — the checksum is the primary integrity mechanism (`arb/feeds/bitget.py`) | pending independent¹ | TBD² | **IMPLEMENTED**³ |
| M1-BOOK | §11 local order-book engine | P1 | Decimal-exact L2 books; snapshot/delta/zero-removal semantics; locally-crossed detection; `vwap_for_qty` returns None on insufficient depth (never approximates); age/sync stamps | `tests/test_book.py` (15 tests) | pending independent¹ | TBD² | **IMPLEMENTED** |
| M1-INSTR | §10 normalization, §48.1 | P1 | Canonical instrument registry; venue-symbol transform cross-checked at load (mismatched metadata rejected); floor-only rounding to a step valid on both venues (exact-LCM); clamp-then-check-minimums ordering; suspended instruments excluded; **USDT ≠ USDC as a tested invariant** | `tests/test_instruments.py` (31 tests incl. all four venue metadata parsers against fixtures); `tests/test_properties.py` (rounding cannot create quantity) | pending independent¹ | TBD² | **IMPLEMENTED** (checked-in `config/instruments.yaml` is a static UNVERIFIED snapshot — see D-006) |
| M1-DEPTH | §15/§16 depth-aware spread math, §48.5 | P0 | Both legs priced by depth-walk VWAP for the rounded clip quantity; insufficient depth or violated minimum ⇒ unexecutable, no top-of-book fallback | `tests/test_slippage.py` (4 tests), `tests/test_spread.py::test_slippage_is_priced_into_edge`, `tests/test_properties.py` (worse execution cannot increase net edge) | pending independent¹ | TBD² | **IMPLEMENTED** |
| M1-FEES | §26 (taker subset), §48.6 | P0 | Exact (non-additive) net-edge formula over both taker fees; fee monotonicity proven by property tests; quote-terms equivalence assumption documented in `arb/signals/fees.py` | `tests/test_fees.py` (6 tests), `tests/test_properties.py` (higher fee can never increase net edge; net ≤ gross) | pending independent¹ | TBD² | **IMPLEMENTED** (taker bps only; tiers are operator-set config) |
| M1-LIFE | §48.10 opportunity-lifetime measurement | P2 | Open at net ≥ threshold on two usable books; close on decay / insufficient depth / stale / unsync / venue-unusable / shutdown, with `duration_ms`, max/mean net, and `close_reason` journaled | `tests/test_spread.py` (9 tests), `tests/test_staleness.py` (2 tests) | pending independent¹ | TBD² | **IMPLEMENTED** |
| M1-STALE | §11 "reject stale books", §48.3 | P1 | 100 ms monitor tick; venue unusable when any book unsynced or older than `staleness_ms`; per-book fresh→stale backstop re-evaluates the symbol; never-updated books unusable from boot | `tests/test_staleness.py`, `tests/test_spread.py::test_stale_book_blocks_and_closes`, `test_unsynced_book_blocks`; `tests/test_properties.py::test_unsynced_or_stale_book_yields_no_evaluation` | pending independent¹ | TBD² | **IMPLEMENTED** |
| M1-REC | §35 deterministic recording, §48.8 | P2 | Every applied book mutation and sync transition journaled at the `OrderBook.tap` seam with literal level strings and exchange/wall/mono stamps; session header carries config snapshot; write failures counted, contained, self-disabling after 10 consecutive errors | `tests/test_recording.py` (9 tests); sim evidence | pending independent¹ | TBD² | **IMPLEMENTED** |
| M1-REPLAY | §35, §48.8 | P2 | Same journal + same config ⇒ byte-identical records; live-vs-replay equivalence; multi-header = restart semantics; truncated recordings flush; config divergence warns (sensitivity runs) | `tests/test_replay.py` (12 tests incl. `test_replay_reproduces_live_records_exactly`); sim evidence (identical SHA-256 across two CLI replays) | pending independent¹ | TBD² | **IMPLEMENTED** |
| M1-LAT | §48.9 latency statistics by exchange, §12 (partial) | P2 | Per-venue `arb_feed_latency_ms` histogram (exchange stamp → local arrival); clock-skew caveat documented — trend signal, not an absolute claim | metric + call sites in `arb/feeds/base.py` (`record_feed_latency`) and every adapter; `docs/OBSERVABILITY.md` | pending independent¹ | TBD² | **IMPLEMENTED** (skew-inclusive by construction; §12's full timestamp ladder awaits execution phases) |
| M1-REPORT | §48.10 ranked viability report | P2 | `python -m arb report [--json]` ranks (symbol, buy venue, sell venue, clip) by persistence-floored sum of open-time est. profit; transparent formula (§17); THEORETICAL labelling baked into output | `tests/test_report.py` (8 tests incl. `test_json_shape_labels_results_theoretical`) | pending independent¹ | TBD² | **IMPLEMENTED** |
| M1-DASH | §48 monitoring dashboard | P3 | Read-only rich terminal view (books, best net spreads, recent opportunities); cosmetic by contract — a dashboard crash logs and never stops collection | code contract in `arb/monitoring/dashboard.py`; exercised manually (no automated TTY test) | pending independent¹ | TBD² | **IMPLEMENTED** |
| M1-NOEXEC | §3 safety defaults, §48.7 "no orders can be placed" | **P0** | No order-placement code exists; `arb/execution/__init__.py` is docstring-only; no API keys are read anywhere; no `--live` flag exists ("nothing it could enable", `arb/__main__.py`) | structural evidence: empty execution package, no credential reads in source, README key checklist is for Phase 2+; re-verified every commit by inspection | pending independent¹ | n/a | **IMPLEMENTED — standing invariant**, must hold on every commit until Milestone 2 is separately specified and approved |
| M1-DOCS | §7 documentation set | P2 | All §7 documents exist with explicit IMPLEMENTED vs SPECIFIED status headers; code-grounded docs cite files | `docs/` tree (26 documents), `CLAUDE.md`, `PROGRESS.md` | pending independent¹ | n/a | **IMPLEMENTED** |

### §48 acceptance criteria — evidence map

The milestone's ten numbered acceptance criteria, each with its current
evidence. "Test evidence" means part of the 144-pass suite above.

| # | §48 criterion | Evidence | Verdict |
|---|---|---|---|
| 1 | Equivalent instruments are mapped correctly | Canonical `BASE/QUOTE` keys everywhere; venue-symbol transform cross-checked at registry load, mismatches rejected (`arb/instruments/registry.py`); `tests/test_instruments.py` | Met (against the UNVERIFIED static snapshot — refresh required before trusting output, D-006) |
| 2 | Every order book passes integrity checks | Venue-native sequence chains + CRC32 checksums; crossed-book guard (`arb/feeds/*.py`); `tests/test_feeds_sync.py`, `tests/test_book.py` | Met (fixture-tested; live validation pending³) |
| 3 | Invalid books are automatically excluded | `synced` + `age_ms() ≤ staleness_ms` gate in `SpreadDetector._usable`; monitor force-closes (`arb/risk/staleness.py`); property test: invalid book yields no evaluation | Met |
| 4 | All timestamps are recorded | Exchange ms + local wall ns + local monotonic ns on every book event and journal line (`arb/feeds/book.py`, `arb/recording/recorder.py`) | Met for the market-data subset of §12's ladder; order-path timestamps have no orders to stamp |
| 5 | Opportunities use actual depth | Depth-walk `vwap_for_qty` on both legs; None (unexecutable) on insufficient depth; `tests/test_slippage.py` | Met |
| 6 | Fees and rounding are included | Exact two-leg taker netting (`arb/signals/fees.py`); floor-only venue rounding + minimums before/after pricing (`arb/signals/spread.py:_price`) | Met (taker bps model; operator must set real tiers) |
| 7 | No orders can be placed | M1-NOEXEC row: no execution code, no credentials, no flag to enable either | Met — standing invariant |
| 8 | Events can be replayed deterministically | Byte-identical replay tests; live-vs-replay equivalence; sim evidence (identical SHA-256) | Met |
| 9 | Latency statistics are available by exchange | `arb_feed_latency_ms{venue=…}` histogram | Met, with the documented clock-skew caveat |
| 10 | Ranked report of viable pairs/symbols | `python -m arb report`; `tests/test_report.py` | Met (THEORETICAL labelling enforced in output) |

## OPEN GATE — §48 independent review

> **§48: "Do not begin execution work until this milestone passes
> independent review."** That review has not occurred. Until it does, and
> until long observation runs from an operator machine³ validate live feed
> behaviour end-to-end, **no Milestone 2 implementation may start**. This is
> the single blocking item between the current state and any execution work,
> and it is recorded as the next best action in `PROGRESS.md`.

## Milestone 2 (§49) — deterministic paper execution

All rows: **SPECIFIED — NOT IMPLEMENTED.** No code exists for any of them.
Specifications live in the cited docs (each carries its own SPECIFIED
status header).

| Feature ID | Requirement | Risk | Acceptance criteria (summary) | Tests / Evidence | Reviewer | Benchmark | Status |
|---|---|---|---|---|---|---|---|
| M2-VBAL | §49, §36 virtual balances | P1 | Simulated per-venue balances; same engines as live path, only execution simulated | none | — | — | SPECIFIED — NOT IMPLEMENTED |
| M2-RESV | §49, §16, §40.6–7 inventory reservation | P0 | Both legs' inventory reserved per cycle; reserved funds cannot be double-spent (property test required) | none | — | — | SPECIFIED — NOT IMPLEMENTED |
| M2-DUAL | §49, §18–19 dual-leg simulation | P0 | Coordinator state machine (§19 states) persisted per transition; ack ≠ fill | none — see `docs/EXECUTION_STATE_MACHINE.md` | — | — | SPECIFIED — NOT IMPLEMENTED |
| M2-PART | §49, §36 partial fills | P1 | Simulated partials produce exact residuals | none | — | — | SPECIFIED — NOT IMPLEMENTED |
| M2-MISMATCH | §49, §20 fill mismatch | P0 | Net base/quote deltas and residual exposure computed after every fill update | none | — | — | SPECIFIED — NOT IMPLEMENTED |
| M2-HEDGE | §49, §21 residual hedge | P0 | Freeze → cancel → reconcile → hedge inside `MAX_*` limits; kill switch on failure | none — see `docs/RESIDUAL_HEDGE_ENGINE.md` | — | — | SPECIFIED — NOT IMPLEMENTED |
| M2-RECON | §49, §34 reconciliation | P0 | Every simulated cycle reconciles to simulated fills and balance changes | none | — | — | SPECIFIED — NOT IMPLEMENTED |
| M2-PNL | §49, §34 profit accounting | P0 | Paper profit computed and **never** mixed with theoretical or realized categories | none | — | — | SPECIFIED — NOT IMPLEMENTED |
| M2-KILL | §49, §30 kill switches | P0 | Block new cycles, preserve recovery, require authorized restart; seam already marked in `arb/risk/staleness.py` | none — see `docs/RISK_ENGINE.md` | — | — | SPECIFIED — NOT IMPLEMENTED |

## Milestone 3 (§50) — shadow trading

| Feature ID | Requirement | Risk | Acceptance criteria (summary) | Tests / Evidence | Reviewer | Benchmark | Status |
|---|---|---|---|---|---|---|---|
| M3-SHADOW | §50 | P0 | Read-only private feeds, real balances, hypothetical orders, later fill analysis; **no trade permission, no live orders** | none | — | — | SPECIFIED — NOT IMPLEMENTED |

## Beyond Milestone 3

Live trading (§5 Modes 5–6, §44), maker-taker (§24), derivatives hedging
(§25), and automatic rebalancing (§23) are **SPECIFIED — NOT IMPLEMENTED**
and additionally **BLOCKED on explicit human authorization** (§46). They do
not get matrix rows with acceptance criteria until the preceding milestones
pass their gates.

## Maintaining this matrix

- A row's Status may change to IMPLEMENTED only in the same commit that adds
  its cited tests; evidence cells name real test files, never intentions.
- Reviewer cells record who actually reviewed, with the iteration reference;
  "pending independent" is the honest default until the §48 gate closes.
  The agent implementing a critical component cannot be its only approver
  (§41).
- Benchmark cells stay TBD until an accepted baseline exists under
  `docs/BENCHMARK_STRATEGY.md`; a number here without a stored baseline and
  comparison method is a performance claim and is forbidden (§46).
- New features enter as SPECIFIED rows first, with requirement and risk
  class assigned at entry — never as retroactive IMPLEMENTED rows.
