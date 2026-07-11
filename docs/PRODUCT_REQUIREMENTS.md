# Product Requirements

> **Status: PARTIALLY IMPLEMENTED — Phase-1 market-data observer only.**
> What exists in code today: public WebSocket market data for Binance, Bybit,
> OKX and Bitget; validated local L2 order books; a depth- and fee-aware
> spread detector; a JSONL research log; a staleness monitor; metrics and a
> terminal dashboard (operating Mode 1). Deterministic recording/replay
> (Mode 2) is in progress this iteration. Everything else in this document —
> execution, inventory, hedging, risk engine, accounting, Modes 3–6 — is
> **specification for later milestones** (CLAUDE.md §§49–50 and beyond), with
> no corresponding code: `arb/execution/` is empty and no API keys are read.

Source of authority: `CLAUDE.md` §§1, 2, 4, 5, 44, 45. Where this document
and `CLAUDE.md` disagree, `CLAUDE.md` wins.

Conventions used below:

- **Risk class** uses the CLAUDE.md §42 priority taxonomy: the class a defect
  in that capability would occupy (P0 worst).
- **Status** is one of `implemented` (exists and tested today), `in-progress`
  (active in the current iteration, see `PROGRESS.md`), `specified` (spec
  only — no code). Partial cases carry a footnote.
- Requirement language: "must/shall" = binding spec; present tense is used
  only for behavior that exists in code today.

---

## 1. Product definition

A deterministic, low-latency, cross-exchange crypto-arbitrage system for
**OKX Global, Bitget, Bybit, Binance Global** spot markets. The primary
strategy is pre-funded spot-to-spot cross-exchange arbitrage (e.g. buy
BTC/USDT on Bitget, sell BTC/USDT on Binance). The system must detect,
validate, execute, reconcile and account for cross-exchange price
discrepancies after considering order-book depth, maker/taker fees,
account-specific fee tiers, slippage, precision and rounding, order latency,
fill probability, partial fills, adverse price movement, inventory exposure,
stablecoin conversion differences, recovery costs, rebalancing costs, and
exchange-specific trading restrictions (CLAUDE.md preamble).

The system must never be described as profitable, high-frequency, world-class
or production-ready without objective supporting evidence.

## 2. Fundamental model: non-atomic execution (§4)

Cross-exchange arbitrage is not atomic: one exchange may fill while the other
rejects. Therefore:

- **PR-M1.** Every arbitrage cycle must begin with sufficient pre-funded
  inventory on both venues (quote on the buy venue, base on the sell venue).
- **PR-M2.** A completed cycle changes inventory distribution (buy venue
  gains base / loses quote; sell venue loses base / gains quote); inventory
  management must be a first-class component, not an afterthought.
- **PR-M3.** External transfers must be strictly separated from live
  execution — never part of an arbitrage cycle.

All three are `specified` — no inventory or execution code exists.

## 3. Platform capability requirements (§1)

| ID | Requirement | Risk class | Acceptance criterion | Status | Evidence |
|---|---|---|---|---|---|
| R1 | Maintain synchronized local order books for all supported exchanges | P0 (stale-book trading) | Books pass venue-native integrity checks (sequence chaining, checksums where provided); invalid/stale books are excluded automatically | implemented¹ | `arb/feeds/book.py`, `arb/feeds/{binance,bybit,okx,bitget}.py`; `tests/test_book.py`, `tests/test_feeds_sync.py` |
| R2 | Normalize equivalent spot markets across exchanges | P1 | Canonical instrument registry maps venue symbols with tick/step/min-qty/min-notional; similarly named assets (USDT vs USDC) are never auto-equated | in-progress² | `PROGRESS.md` Iteration 2 (M1-INSTR); `arb/models.py` |
| R3 | Detect executable cross-exchange spreads | P2 | Spread computed from actual depth (VWAP walk per clip), net of both taker fees; open/close lifecycle recorded | implemented | `arb/signals/spread.py`, `arb/signals/slippage.py`, `arb/signals/fees.py`; `tests/test_spread.py`, `tests/test_slippage.py`, `tests/test_fees.py` |
| R4 | Determine the maximum safe and profitable order size | P1 | Full §16 optimizer: candidate quantities vs depth, balances, reservations, exchange filters, inventory targets, exposure limits; never rounds up past inventory | in-progress³ | Clip-size depth walk: `arb/signals/slippage.py`; full optimizer: specified only |
| R5 | Reserve sufficient inventory on both exchanges before a cycle | P0 (double-spend of reserved funds) | Reserved funds cannot be double-spent (property test); both legs reserved before submission | specified | `docs/INVENTORY_ENGINE.md` (spec) |
| R6 | Attempt both arbitrage legs concurrently | P1 | Dual-leg coordinator manages both legs as one logical cycle (§19 states); legs submitted as close together as technically possible | specified | `docs/EXECUTION_STATE_MACHINE.md` (spec) |
| R7 | Monitor acknowledgements and fills independently | P0 (ack treated as fill) | Acknowledgement is never treated as fill; fills tracked per leg via private streams, REST only on uncertainty | specified | `docs/EXECUTION_STATE_MACHINE.md` (spec) |
| R8 | Detect unmatched or partially matched execution | P0 (uncontrolled residual exposure) | Exact residual exposure computed from net base/quote deltas after every fill update (§20) | specified | `docs/EXECUTION_STATE_MACHINE.md` (spec) |
| R9 | Hedge residual exposure immediately when required | P0 (uncontrolled residual exposure) | §21 procedure: freeze, cancel, reconcile, price-protected hedge within `MAX_RESIDUAL_DURATION_MS`; kill switch on failure | specified | `docs/RESIDUAL_HEDGE_ENGINE.md` (spec) |
| R10 | Reconcile orders, fills, balances and fees | P1 (reconciliation defect) | Every completed cycle reconciles to exchange fills and balance changes (§34) | specified | — |
| R11 | Calculate realized net profit | P0 (accounting defect) | Theoretical / expected / paper / shadow / gross-live / net-realized categories never mixed (§34) | specified | Research-log precursor: `arb/accounting/opportunities.py` |
| R12 | Maintain target inventory across exchanges | P1 | Per venue/asset: actual, available, reserved, target, min, max, deviation tracked; inventory constrains opportunity size (§22) | specified | `docs/INVENTORY_ENGINE.md`, `docs/REBALANCING_POLICY.md` (spec) |
| R13 | Stop automatically when risk limits are breached | P0 (risk-engine bypass) | Central risk engine + §30 kill switches block new cycles while preserving emergency recovery | in-progress⁴ | Staleness monitor: `arb/risk/staleness.py`, `tests/test_staleness.py`; full engine: `docs/RISK_ENGINE.md` (spec) |
| R14 | Record every decision and state transition | P1 | Append-only journal covering §34 event list; deterministic market-data recording (§35) | in-progress⁵ | `arb/recording/recorder.py`; opportunity JSONL: `arb/accounting/opportunities.py`; `PROGRESS.md` Iteration 2 (M1-REC) |
| R15 | Survive exchange outages, network failures and process restarts | P1 (missing failure-recovery) | Feed gap ⇒ resync/rebuild (exists); every incomplete execution cycle resumes or reconciles after restart (spec) | in-progress⁶ | Feed recovery: `arb/feeds/*.py`, `tests/test_feeds_sync.py`; cycle restart recovery: specified only |

Footnotes:

1. Implemented for public market data on all four venues, unit/contract
   tested against recorded protocol fixtures. Live end-to-end feed behavior
   is unvalidated from the development sandbox (egress to exchange endpoints
   is blocked) and must be validated from an operator machine — see
   `README.md` "Known limitations" and `PROGRESS.md`.
2. Cross-venue symbol comparison exists in the detector; the canonical
   instrument registry with precision/rounding metadata is Iteration-2 work.
3. Only the depth-walk over configured clip sizes exists. Balance,
   reservation, inventory-target and exposure inputs to sizing require
   Milestone-2+ components that do not exist.
4. Only the market-data staleness monitor exists (venue unusable at
   >500 ms book age or unsynced state; force-closes affected opportunities).
   It is the designated attachment seam for the Phase-2 kill switch. No
   other risk limit is enforced in code.
5. Opportunity decisions are journaled to JSONL today; deterministic
   recording of book mutations is Iteration-2 work; execution/risk journaling
   has no implementation target yet.
6. Feed-level recovery (gap ⇒ snapshot resync, resubscribe storm guard,
   connection recycle) is implemented and tested. Execution-cycle restart
   recovery is pure spec.

## 4. Initial production scope (§1)

Binding scope for the first live-eligible configuration:

```text
Spot-to-spot
Same asset
Same or economically equivalent quote asset
Pre-funded inventory
No leverage
No exchange withdrawal permission
```

- **PR-S1.** The first live-eligible execution strategy shall be taker-taker
  (§18): protected marketable IOC limit orders on both legs, never unbounded
  market orders.
- **PR-S2.** Quote currencies must not be treated as economically equivalent
  without an explicit, configured conversion model and haircut (§27); USDT
  and USDC are never auto-equated (already enforced in the Phase-1 detector
  by symbol identity).

## 5. Excluded / deferred strategies (§1, §24, §25)

The following must remain **disabled** until separately specified, tested,
reviewed and approved by a human (§46). None has any code today.

| Strategy | Gate before enabling |
|---|---|
| Maker-taker arbitrage | §24 models (fill probability, queue position, adverse selection, hedge liquidity, cancel latency) + human approval; historical-simulation profitability alone is insufficient |
| Stablecoin parity arbitrage | §27 conversion graph, haircuts, depeg kill switch |
| Spot-perpetual arbitrage | §25 derivatives gates (eligibility, leverage/liquidation/funding limits, legal review, human authorization) |
| Perpetual-to-perpetual arbitrage | §25 derivatives gates |
| Inventory-aware market making | Separate specification and review |
| Multi-leg routing | Separate specification and review |
| Triangular arbitrage within one exchange | Separate specification and review |

The spot strategy must never be silently converted into a leveraged strategy.

## 6. Operating modes (§5)

Each mode is a requirement. "Places no orders" is a hard acceptance criterion
for Modes 1–4, verified by absence of order-placement code paths, not by
configuration alone, until Mode 5 is separately built and gated.

| ID | Mode | Requirement | Risk class | Acceptance criterion | Status | Evidence |
|---|---|---|---|---|---|---|
| OM-1 | Market-data observation | Connect to public feeds, reconstruct order books, detect opportunities, measure opportunity lifetime; place no orders | P2 | Books pass integrity checks; opportunities use actual depth and fees; all timestamps recorded; no order code exists | **implemented** | `python -m arb observe`; `arb/` tree (no execution code — `arb/execution/__init__.py` is empty); test suite in `tests/` |
| OM-2 | Historical replay | Replay recorded exchange events; reconstruct historical state deterministically; simulate decisions; measure latency sensitivity; place no orders | P2 | Same recorded stream + same config ⇒ byte-identical opportunity records; replay of a synthetic stream matches live-path detector output | **in-progress** (this iteration) | `arb/recording/recorder.py`; acceptance criteria in `PROGRESS.md` Iteration 2 (M1-REC, M1-REPLAY) |
| OM-3 | Paper trading | Live books + simulated order submission, exchange latency, queue position, partial fills, fees, slippage; virtual balances; place no orders | P1 | Reuses the real market-data/opportunity/sizing/risk/inventory/state-machine/hedge/accounting engines with only exchange execution simulated (§36); simulator never assumes displayed liquidity fills | specified (Milestone 2, §49) | `docs/EXECUTION_STATE_MACHINE.md`, `docs/RISK_ENGINE.md` (spec) |
| OM-4 | Shadow trading | Actual account balances via read-only private feeds; generate would-have-been orders; compare decisions with later market events; place no orders | P1 | Read-only credentials only (no trade permission); hypothetical orders recorded and analyzed against subsequent fills (§50) | specified (Milestone 3, §50) | `docs/SECURITY_MODEL.md` (spec) |
| OM-5 | Limited live trading | Approved accounts, minimal capital limit, only approved symbols, only taker-taker spot arbitrage, automatic kill switches | P0 | All twelve §3 live-activation gates pass, including server-side authorization, operator approval and an immutable activation audit record; §44 qualification thresholds met | specified | `docs/RISK_ENGINE.md`, `docs/SECURITY_MODEL.md` (spec) |
| OM-6 | Qualified live trading | Available only after limited live operation proves correct accounting, controlled latency, reliable fills and recovery, stable inventory, positive net execution after all costs, no unresolved critical defects | P0 | Each listed proof backed by recorded evidence; expansion only by explicit human approval (§46) | specified | — |

- **PR-OM7.** Live activation (OM-5/OM-6) additionally requires every §3
  precondition; no frontend control may independently enable live trading.
  Default configuration is `MARKET_DATA_ONLY=true`, `PAPER_TRADING=true`,
  `LIVE_TRADING=false` — the safety defaults in CLAUDE.md §3 must never be
  weakened.

## 7. Realistic high-frequency engineering requirements (§2)

"High frequency" for this project means the following engineering properties,
not marketing claims. The system must never claim equivalence to a colocated
institutional trading firm.

| ID | Requirement | Status | Evidence / note |
|---|---|---|---|
| HF-1 | Event-driven market data (no polling loop as the data source) | implemented | Native WS push feeds, `arb/feeds/*.py` |
| HF-2 | Persistent network connections with managed reconnect | implemented | WS clients with resync/recycle logic; `tests/test_feeds_sync.py` |
| HF-3 | Local order books as the decision input | implemented | `arb/feeds/book.py`; `tests/test_book.py` |
| HF-4 | Minimal allocations in the hot path | specified | Applies to the future latency-critical engine; the Python observer does not claim this. Rust engine deferred pending observation data (`PROGRESS.md`, D-002) |
| HF-5 | No binary floating-point for price, quantity, balance, fee, profit, exposure, risk limits; scaled integers in the hot path where practical | implemented (partial)⁷ | Phase 1 uses `Decimal`-exact arithmetic throughout money math (`arb/feeds/book.py`, `arb/signals/*`); scaled-integer hot path is a requirement on the future execution engine |
| HF-6 | Precomputed symbol and fee metadata (no repeated lookup/parsing in the decision path) | in-progress | Fee config precomputed (`arb/signals/fees.py`); instrument registry is Iteration-2 work (M1-INSTR) |
| HF-7 | Concurrent order submission | specified | Milestone 2+ (simulated first) |
| HF-8 | Measured end-to-end latency, monotonic clocks for internal durations; never claim latency from exchange timestamps alone | implemented (partial)⁸ | Feed-latency histograms + book-age gauges exist (`arb/monitoring/metrics.py`); documented caveat: histograms include local↔exchange clock skew, so they are trend signals, not absolute claims |
| HF-9 | Deterministic execution (same inputs ⇒ same decisions) | in-progress | Replay determinism is the Iteration-2 acceptance criterion (OM-2) |
| HF-10 | Native exchange protocols where available (WS today; FIX/SBE only where available and authorized) | implemented | Venue-native WS book-sync protocols implemented explicitly (see `README.md` integrity table); no FIX/SBE entitlement exists |
| HF-11 | Non-blocking telemetry | implemented | Prometheus `/metrics` endpoint and async logging outside the data path (`arb/monitoring/`) |
| HF-12 | No database access in the decision-critical path | implemented | No database exists in Phase 1; requirement binds all future phases |
| HF-13 | No LLM in the trading path; no live order generated by an LLM | implemented (vacuously) | No trading path exists; requirement binds all future phases (§40 invariant 4) |
| HF-14 | No REST polling for normal opportunity discovery | implemented | REST is used only for initial/resync book snapshots (Binance), never for discovery |
| HF-15 | No blockchain transfer during an arbitrage cycle | implemented (vacuously) | No transfer code exists by policy; requirement binds all future phases |
| HF-16 | Document, per exchange: public/private WS, WS order entry, FIX, SBE, fast order-response feeds, VIP-only channels, institutional access, disconnect-cancel-all, demo environments | specified | `docs/EXCHANGE_CAPABILITY_MATRIX.md` (planned, §7/§8); versioned `ExchangeCapabilities` registry (§8) |
| HF-17 | Enable only capabilities available to the actual account; fail safely when an expected entitlement is unavailable | specified | Requires private-account phase (Milestone 3+) |

Footnotes:

7. The §6 scaled-integer forms (`price_ticks: i64`, `quantity_steps: i64`,
   `quote_units: i128`) apply to the deferred Rust latency-critical engine.
   The binding, already-satisfied core of HF-5 is: no binary float ever
   touches money.
8. Only market-data-side latency is measurable today (no orders exist, so no
   order-send/ack/fill latency). The full §12 timestamp set (ten timestamps
   per event through fill) is specified for execution phases.

## 8. Live-trading qualification requirements (§44)

Live trading (OM-5) must not begin until measurable thresholds are **defined,
approved and met** for every row below. No threshold values are set today;
defining them is itself a prerequisite. All rows: risk class P0 (unsafe live
activation), status `specified`.

| ID | Qualification metric (threshold TBD, must be set and approved before Mode 5) |
|---|---|
| Q-1 | Market-data observation duration |
| Q-2 | Paper-trading duration |
| Q-3 | Shadow-trading duration |
| Q-4 | Recorded event count |
| Q-5 | Number of qualified opportunities |
| Q-6 | Number of simulated cycles |
| Q-7 | Book error rate |
| Q-8 | False-opportunity rate |
| Q-9 | Order rejection rate |
| Q-10 | Fill-mismatch rate |
| Q-11 | Hedge failure rate |
| Q-12 | Reconciliation error rate |
| Q-13 | Maximum latency |
| Q-14 | Maximum residual-exposure duration |
| Q-15 | Maximum simulated drawdown |
| Q-16 | Net result after all modeled costs |

- **Q-17.** Passing profitability tests alone is insufficient: the system
  must additionally prove correctness, security, recovery, reconciliation,
  inventory control, stable operation and risk containment.
- **Q-18.** Live trading must begin with one approved symbol, two approved
  exchanges, minimal capital, a strict daily loss limit, a strict residual
  limit, and immediate operator alerts; expansion only through explicit
  approval (§46).

## 9. Release-candidate conditions (§45)

A release candidate must not be declared until **every** condition below
holds. Current standing is shown honestly; almost all are unmet because only
Phase 1 exists.

| ID | Condition | Current standing |
|---|---|---|
| RC-1 | Four exchange adapters implemented or formally deferred | Not met — market-data-only adapters exist for all four venues; the full §9 adapter contract (private streams, order entry, reconciliation) has no code |
| RC-2 | All implemented adapters pass one common contract-test suite | Partially — feed-protocol behavior is fixture-tested per venue (`tests/test_feeds_sync.py`); a single cross-venue contract suite for the full §9 surface does not exist |
| RC-3 | Account entitlements documented | Not met (no accounts connected; `docs/EXCHANGE_ENTITLEMENTS.md` planned) |
| RC-4 | Order-book reconstruction verified | Partially — unit/fixture-verified; live end-to-end verification pending operator-machine runs |
| RC-5 | Cross-exchange normalization verified | Not met (in-progress, M1-INSTR) |
| RC-6 | Fee calculations verified | Partially — taker-fee math unit-tested (`tests/test_fees.py`); full §26 fee engine (tiers, rebates, fee-asset variants) not built |
| RC-7 | Depth-aware sizing verified | Not met (clip depth-walk only) |
| RC-8 | Inventory reservation verified | Not met |
| RC-9 | Dual-leg execution verified | Not met |
| RC-10 | Residual hedging verified | Not met |
| RC-11 | Restart recovery verified | Not met (feed-level only) |
| RC-12 | Kill switches verified | Not met (staleness monitor only) |
| RC-13 | Accounting reconciles | Not met |
| RC-14 | Security review passes | Not met |
| RC-15 | Risk review passes | Not met |
| RC-16 | Replay testing passes | Not met (in-progress, M1-REPLAY) |
| RC-17 | Chaos testing passes | Not met |
| RC-18 | Soak testing passes | Not met |
| RC-19 | Documentation matches code | Ongoing — every doc carries an explicit implemented/specified status header |
| RC-20 | No critical issue remains | No P0/P1 defects known at Iteration-2 start (`PROGRESS.md`) |
| RC-21 | Live mode remains disabled | **Holds** — no order-placement code exists anywhere in the repository |
| RC-22 | Independent release review approves | Not met |

## 10. Cross-references

- Safety defaults and live-activation gates: `CLAUDE.md` §3 (never weakened).
- Execution states and fill matching: `docs/EXECUTION_STATE_MACHINE.md`.
- Risk limits and kill switches: `docs/RISK_ENGINE.md`.
- Inventory and rebalancing: `docs/INVENTORY_ENGINE.md`,
  `docs/REBALANCING_POLICY.md`.
- Residual hedging: `docs/RESIDUAL_HEDGE_ENGINE.md`.
- Credentials and threat model: `docs/SECURITY_MODEL.md`.
- Current state and iteration log: `PROGRESS.md`; operator-facing overview:
  `README.md`.
