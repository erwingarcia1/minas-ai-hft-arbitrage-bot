# Arbitrage Mathematics

> **Status: IMPLEMENTED** for the Phase-1 observer evaluation (CLAUDE.md §48):
> depth-aware VWAP pricing of both legs, exact fee netting, and venue rounding
> are in code and cited by file below. Sections marked **SPECIFIED — NOT
> IMPLEMENTED** describe the remaining §15 reserve terms, the §16 size
> optimizer, and the §17 confidence model, none of which exist yet. There is
> no execution code in this repository; every number this document describes
> is a *theoretical, observed* edge, never a realized profit (§34).
> Implementation sources: `arb/signals/spread.py` (evaluation pipeline),
> `arb/signals/fees.py` (edge math), `arb/instruments/registry.py` (rounding),
> `arb/feeds/book.py` (depth walk). Test evidence: `tests/test_fees.py`,
> `tests/test_instruments.py`, `tests/test_properties.py`.

## 1. The governing formula (§15)

For asset `X` quoted in `Q`, buying on venue A and selling on venue B at
candidate quantity `q`:

```text
Expected Net Profit(q)
=   Depth-Aware Sell Proceeds(q)
  − Depth-Aware Buy Cost(q)
  − Buy Fees(q)
  − Sell Fees(q)
  − Expected Slippage(q)
  − Latency Reserve(q)
  − Adverse Selection Reserve(q)
  − Residual Hedge Reserve(q)
  − Inventory Rebalancing Reserve(q)
  − Other Explicit Costs(q)
```

Phase 1 implements the first four terms exactly and treats decision-time
slippage as *inherent* in the depth walk (the VWAP for the full clip is the
executable price, not top-of-book). The five reserve terms are not yet
modeled — see §4 below for the term-by-term status.

## 2. What the implemented evaluation computes

`SpreadDetector.evaluate()` (`arb/signals/spread.py`) runs on every book
update. For each symbol it takes every ordered pair of *usable* venues
(`permutations(usable, 2)` — both directions priced independently) and every
configured clip size, and calls `_price()`:

1. **Usability gate.** A book participates only when `book.synced` is True
   and `book.age_ms(now) <= risk.staleness_ms` (`_usable()`). Unsynced or
   stale books are excluded and any opportunity touching them force-closes
   with reason `book_unusable` / `venue_unusable`. Suspended instruments
   (`InstrumentMeta.status != "trading"`) are excluded from evaluation
   entirely at construction.
2. **Top-of-book presence.** `buy_book.best_ask()` and `sell_book.best_bid()`
   must both exist, else no evaluation.
3. **Reference price.** `ref_price = (buy_ask + sell_bid) / 2.0` — the
   cross-venue mid of the buy leg's best ask and the sell leg's best bid.
   Guarded `ref_price > 0`.
4. **Clip → quantity conversion.** Clips are configured as *quote notional*
   (`signals.clip_sizes_usdt`). The raw base quantity is
   `clip_usdt / ref_price`, converted **once** so both legs price the *same*
   base amount.
5. **Venue rounding** via `round_qty_for_pair(qty, buy_meta, sell_meta)`
   (`arb/instruments/registry.py`) — see §3. Returns `None` when the clip is
   unexecutable after rounding; the detector treats that identically to
   missing depth (close reason `insufficient_depth`).
6. **Depth-aware VWAP, both legs.** `buy_book.vwap_for_qty("ask", qty)` walks
   the asks upward accumulating `take * price` until `qty` is filled;
   `sell_book.vwap_for_qty("bid", qty)` walks the bids downward
   (`arb/feeds/book.py`). Either returns **`None` when the book cannot fill
   the full quantity** — the caller must treat the clip as unexecutable,
   never approximate with top-of-book. Displayed-depth slippage is therefore
   part of every edge by construction.
7. **Minimum-notional gates.** Checked last because they need executable
   prices: `qty * buy_vwap >= buy_meta.min_notional` and
   `qty * sell_vwap >= sell_meta.min_notional`, else `None`.
8. **Edge computation.** `gross_edge_bps(buy_vwap, sell_vwap)` and
   `net_edge_bps(buy_vwap, sell_vwap, taker_bps_A, taker_bps_B)`
   (`arb/signals/fees.py`), with per-venue taker fees from
   `Settings.taker_bps()` (`arb/models.py`).
9. **Threshold and lifecycle.** `net_bps >= signals.min_net_edge_bps` opens
   (or extends) an opportunity; falling below closes it with reason
   `edge_below_threshold`. Every open→close interval is journaled as an
   `OpportunityRecord` (duration, open/max/mean net bps, VWAPs, close
   reason). Missing instrument metadata degrades to *unrounded* pricing with
   a boot-time WARNING — a visible, deliberate degradation, never silent.

### The exact (non-additive) net edge formula

```text
cost      = buy_vwap  * (1 + f_buy)          f = taker_bps / 1e4
proceeds  = sell_vwap * (1 − f_sell)
net_bps   = (proceeds / cost − 1) * 1e4
gross_bps = (sell_vwap / buy_vwap − 1) * 1e4
```

This is the exact ratio form, **not** the additive approximation
`gross − f_buy − f_sell`. For positive edges the exact value is strictly
below the naive subtraction (buy-side fee compounds against the whole
proceeds ratio), so the implemented form is the conservative one —
`tests/test_fees.py::test_net_is_not_naive_subtraction` pins this down.

## 3. Rounding: §16's never-round-up rule

`round_qty_for_pair` (`arb/instruments/registry.py`) makes one quantity
executable on **both** venues:

1. Clamp to the tightest `max_qty` **first** (a max-clamp landing below the
   other venue's minimum must yield "unexecutable", never a violating qty).
2. Floor to `common_qty_step(a, b)` — the least common multiple of the two
   venues' `qty_step`s, computed exactly via `Fraction` (correct for
   non-nested steps: `0.002` vs `0.005` → `0.01`).
3. Reject (`None`) if the floored quantity is `<= 0` or below either venue's
   `min_qty`. Minimum notionals are checked by the caller once VWAPs exist.

Quantities are **never rounded up** — rounding up could exceed depth or
(later) inventory. Property evidence
(`tests/test_properties.py::TestRoundingCannotCreateMoney`): the rounded
quantity never exceeds the raw quantity and is an exact rational multiple of
both steps. Metadata comes from `config/instruments.yaml`, a **static,
UNVERIFIED documentation snapshot** (this sandbox cannot reach exchange
endpoints); refresh with `python -m arb instruments fetch` from an operator
machine before trusting research output.

## 4. §15 term-by-term status

| §15 term | Status | Where |
|---|---|---|
| Depth-aware buy cost / sell proceeds | **IMPLEMENTED** | `OrderBook.vwap_for_qty`, `SpreadDetector._price` |
| Buy fee / sell fee (taker) | **IMPLEMENTED** | `net_edge_bps`, per-venue `venues.<v>.fees.taker_bps` — see `docs/FEE_MODEL.md` |
| Slippage vs. displayed depth | **IMPLEMENTED** (inherent) | VWAP walk prices the whole clip; no top-of-book fallback |
| Rounding loss | **IMPLEMENTED** | `round_qty_for_pair` floor + min-qty/min-notional gates |
| Latency reserve | **SPECIFIED — NOT IMPLEMENTED** | book movement between decision and (future) execution; will plug in as a configured bps deduction in `SignalSettings` (`arb/models.py`, `extra="forbid"` — new field required) composed into `net_edge_bps` |
| Adverse selection reserve | **SPECIFIED — NOT IMPLEMENTED** | same seam; to be calibrated from Phase-1 opportunity-lifetime data (`OpportunityRecord.duration_ms`) |
| Residual hedge reserve | **SPECIFIED — NOT IMPLEMENTED** | requires the Phase-2 execution state machine (`docs/RESIDUAL_HEDGE_ENGINE.md`); no positions exist in Phase 1 |
| Inventory rebalancing reserve | **SPECIFIED — NOT IMPLEMENTED** | requires the inventory engine (`docs/INVENTORY_ENGINE.md`) |
| Stablecoin basis reserve | **SPECIFIED — NOT IMPLEMENTED**; structurally avoided today | the detector only compares the *same canonical symbol* across venues, and `assets_equivalent` (`arb/instruments/registry.py`) is exact string equality — USDT-quoted books are never netted against USDC-quoted books (§27, invariant §40.23) |
| Minimum absolute profit gate | **SPECIFIED — NOT IMPLEMENTED** | only the bps threshold `signals.min_net_edge_bps` gates today |
| Worst-case approved loss ≤ max cycle loss | **SPECIFIED — NOT IMPLEMENTED** | Phase-2 risk engine (`docs/RISK_ENGINE.md`); `risk.max_position_usdt` / `max_daily_loss_usdt` are validated but not enforced (no positions exist) |

Every reserve, when added, must preserve the §39 monotonicity properties:
increasing any cost term cannot increase net edge
(`tests/test_properties.py::TestFeeMonotonicity`, `TestSlippageMonotonicity`).

## 5. Worked example (matches the test suite)

Setup from `tests/test_instruments.py::TestDetectorRounding` and
`tests/test_replay.py`: OKX book 9990 bid / **10000 ask**, Binance book
**10030 bid** / 10040 ask, depth ample at top of book; 10 bps taker fees both
venues; clip 1000 USDT; steps 0.001 (OKX) vs 0.01 (Binance) in the rounding
test.

Direction buy-OKX / sell-Binance:

```text
ref_price = (10000 + 10030) / 2                    = 10015
raw qty   = 1000 / 10015                           = 0.0998502… BTC
common step = lcm(0.001, 0.01)                     = 0.01
qty       = floor(0.0998502 / 0.01) * 0.01         = 0.09 BTC   (never up)
min-qty / min-notional gates: 0.09 ≥ min_qty; 0.09*10000 = 900 ≥ min_notional

buy_vwap  = 10000.0   (asks fill 0.09 at one level)
sell_vwap = 10030.0   (bids fill 0.09 at one level)

gross_bps = (10030/10000 − 1) * 1e4                = 30.00 bps
cost      = 10000 * 1.001                          = 10010.00
proceeds  = 10030 * 0.999                          = 10019.97
net_bps   = (10019.97/10010 − 1) * 1e4             =  9.96 bps
```

Naive additive netting would say `30 − 10 − 10 = 10.00` bps; the exact form
gives **9.96 bps** — the shipped, conservative number. Against the default
`min_net_edge_bps: 3.0` this opens an opportunity;
`est_profit_usdt_open = 1000 * 9.96/1e4 ≈ 1.00 USDT` is recorded on close.
Note that `est_profit_usdt_open` uses the *configured clip notional*; the
rounded executable quantity (0.09 BTC ≈ 900 USDT at the buy VWAP) is
slightly smaller, so the recorded estimate is an upper figure for the
rounded size — the per-unit edge (bps) is exact. The reverse direction
(buy Binance @ 10040, sell OKX @ 9990) prices negative and never opens.

## 6. Size optimization (§16) — status

**Implemented today: a fixed clip grid, not an optimizer.** The detector
prices every configured clip (`signals.clip_sizes_usdt`, validated positive,
deduplicated, sorted — `arb/models.py`) independently through the full
pipeline above. Each clip either prices executably at its depth-walked VWAPs
or is rejected (`insufficient_depth`). This yields the §16 quantities only
partially: the grid approximates `maximum_executable_quantity` by observing
which clips survive, and nothing computes `profit-maximizing_quantity`.

**SPECIFIED — NOT IMPLEMENTED:** the §16 optimizer — searching quantity
space for maximum expected net profit subject to buy/sell depth, available
and reserved balances (no balances exist in Phase 1), inventory targets,
exposure limits, and hedge capacity. It belongs to Milestone 2 (§49) where
virtual balances first exist, and must keep the invariants already
established here: round down only, reject below venue minimums, no
evaluation from an unusable book.

## 7. Opportunity confidence model (§17) — status

**SPECIFIED — NOT IMPLEMENTED.** No confidence score exists; the only gates
are the deterministic ones above (usability, depth, rounding, threshold).
When built it must be a **deterministic, explainable, backtestable** function
— never an opaque or LLM-derived score (§17, invariant §40.4). Inputs the
Phase-1 data layer already captures for it: book age (`OrderBook.age_ms`,
recorded per-leg in every `SpreadSample`), sync state, cross-venue timestamp
material (exchange/wall/mono stamps in the recording journal,
`arb/recording/`), opportunity lifetime distributions
(`OpportunityRecord.duration_ms`, `samples`, `mean_net_bps`), and the
persistence-floored ranking in `arb/report/`. Calibration should run against
the deterministic replay engine (`arb/replay/`) so scores are reproducible
from recorded streams (§35). A confidence gate may reject an opportunity
whose raw spread is positive.

## 8. Evidence map

| Claim | Test |
|---|---|
| Exact net-edge arithmetic (79.82 bps case) | `tests/test_fees.py::test_net_edge_exact` |
| Exact < naive for positive edges | `tests/test_fees.py::test_net_is_not_naive_subtraction` |
| Fees ↑ ⇒ net edge cannot ↑ (§39.1) | `tests/test_properties.py::TestFeeMonotonicity` |
| Worse VWAP either leg ⇒ net edge cannot ↑ (§39.2) | `tests/test_properties.py::TestSlippageMonotonicity` |
| Rounding cannot create money (§39.3) | `tests/test_properties.py::TestRoundingCannotCreateMoney` |
| Stale/unsynced book yields no evaluation (§40.1/.2) | `tests/test_properties.py::TestInvalidBookCannotEvaluate` |
| Rounded qty floors to common step (0.09 example) | `tests/test_instruments.py::TestDetectorRounding` |
| Same events + config ⇒ byte-identical records | `tests/test_replay.py::TestLiveReplayEquivalence` |
