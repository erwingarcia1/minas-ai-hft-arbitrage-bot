# Fee Model

> **Status: IMPLEMENTED** for Phase-1 observation (CLAUDE.md §48): static
> per-venue taker fees in basis points, configured in `config/config.yaml`,
> applied exactly (multiplicatively, not additively) to every spread
> evaluation by `arb/signals/fees.py` and `arb/signals/spread.py`. Everything
> beyond that — the full §26 fee engine (account-specific tiers, rebates,
> fee-asset variants, uncertainty reserve, live fee retrieval) — is
> **SPECIFIED — NOT IMPLEMENTED** and mapped to phases in §5 below. No
> credentials are read in this build, so account fee tiers *cannot* be
> fetched; the operator must set them by hand (§2).

## 1. What is implemented

### 1.1 Configuration schema

Fees are per-venue constants in `config/config.yaml`, validated by `FeeTier`
(`arb/models.py`):

```yaml
venues:
  binance: { enabled: true, fees: { maker_bps: 10.0, taker_bps: 10.0 } }
  okx:     { enabled: true, fees: { maker_bps: 8.0,  taker_bps: 10.0 } }
```

| Field | Bounds | Used today? |
|---|---|---|
| `taker_bps` | `0.0 ≤ x ≤ 100.0` | **Yes** — every evaluation, via `Settings.taker_bps(venue)` |
| `maker_bps` | `−10.0 ≤ x ≤ 100.0` (negative = rebate) | **No** — validated only; the sole reference is `arb/models.py:44`. Maker-taker strategy is disabled per §24; the field exists so the schema is complete before Phase 2+ |

Unknown keys anywhere in the config are fatal (`extra="forbid"`), so a typo
like `taker_bp` cannot silently zero a fee.

### 1.2 Strategy scope: taker-taker only

All edges are **taker edges**: the Phase-1 detector models crossing the
spread on both legs (§18's first live-eligible strategy, observed only).
There is no maker modeling, no queue model, no rebate accounting.

### 1.3 The exact formula

`net_edge_bps` (`arb/signals/fees.py`):

```text
f_buy   = buy_taker_bps  / 1e4
f_sell  = sell_taker_bps / 1e4
cost     = buy_vwap  * (1 + f_buy)      # buying q base costs  q·buy_vwap·(1+f)
proceeds = sell_vwap * (1 − f_sell)     # selling q base yields q·sell_vwap·(1−f)
net_bps  = (proceeds / cost − 1) * 1e4
```

Exact ratio form, **not** the additive approximation `gross − f_buy −
f_sell`; at 10 bps fees the difference is small, but this is the number
capital decisions rest on (`tests/test_fees.py::test_net_edge_exact`,
`test_net_is_not_naive_subtraction`). Non-positive VWAPs raise `ValueError`
immediately — a programming error, not a market condition.

### 1.4 Quote-terms convention (documented equivalence, not an omission)

Fees are expressed in basis points of notional and **modeled in quote
terms** on both legs. Some venues charge spot buy-side fees in the *base*
asset; expressing both legs in quote terms is equivalent for edge purposes
at these sizes and keeps the math venue-uniform. This is stated verbatim in
the `arb/signals/fees.py` module docstring precisely so nobody mistakes it
for a missing fee. The equivalence stops being adequate once real fills
exist (Phase 2+), where the actual fee asset must be accounted per fill
(§26, §34).

### 1.5 Where fees enter the pipeline

`SpreadDetector._price` (`arb/signals/spread.py`) computes both depth-walked
VWAPs, then calls `net_edge_bps(buy_vwap, sell_vwap,
settings.taker_bps(buy_venue), settings.taker_bps(sell_venue))`. The result
gates opportunity open/close against `signals.min_net_edge_bps` and is
recorded in every `OpportunityRecord`, every periodic `SpreadSample`
(including negative spreads — the "does edge exist at my fee tier at all"
dataset), the Prometheus `spread_net_bps` histogram, and the ranked
viability report (`arb/report/`). **Fees and rounding are part of every
evaluation** (§48.6); there is no fee-free code path to an opportunity.

## 2. Operator warning: tiers must match YOUR account (§26)

`config/config.yaml` carries this in the shipped file:

> Fee tiers in basis points. THESE ARE STANDARD RETAIL (VIP0, no BNB/token
> discounts) — SET THEM TO YOUR ACTUAL TIER before trusting net-spread data,
> since the whole point of Phase 1 is measuring edge at YOUR fees.

The shipped numbers (10 bps taker on Binance/Bybit/Bitget, 10 bps taker /
8 bps maker on OKX) are **placeholder retail rates from public
documentation, UNVERIFIED against any account or live endpoint** — this
sandbox cannot reach exchanges, and no account exists to inspect. A tier
that is 2 bps better per leg moves every recorded net edge by ~4 bps, which
at typical cross-venue spreads is the difference between "viable" and
"nothing survives fees." Wrong fee config invalidates the entire research
dataset, silently. Set the real tier first; re-record after any tier change.

## 3. Property-test evidence (§39)

| Property | Test |
|---|---|
| Increasing either fee cannot increase net edge (§39.1) | `tests/test_properties.py::TestFeeMonotonicity::test_increasing_either_fee_cannot_increase_net_edge` |
| Net edge never exceeds gross edge | `tests/test_properties.py::TestFeeMonotonicity::test_net_never_exceeds_gross` |
| Zero fees ⇒ net == gross | `tests/test_fees.py::test_net_edge_zero_fees_equals_gross` |
| Fees can turn positive gross negative (5 bps gross vs 10+10) | `tests/test_fees.py::test_net_edge_negative_when_fees_exceed_gross` |
| Exact form is conservative vs naive subtraction | `tests/test_fees.py::test_net_is_not_naive_subtraction` |

## 4. Quote-currency boundary (§27)

Fees are denominated in quote units of the pair being priced. The detector
only ever compares the **same canonical symbol** across venues, and
`assets_equivalent` (`arb/instruments/registry.py`) is exact string equality
— so no fee (or edge) arithmetic ever nets USDT against USDC (invariant
§40.23). Stablecoin conversion fees/haircuts (§27) therefore have no code
path to exist in yet; they enter with the conversion-graph work, not the
fee model.

## 5. The full §26 fee engine — SPECIFIED, NOT IMPLEMENTED

None of the following exists in code. Phase mapping per the milestone plan
(§48/§49/§50, README "Phase plan"):

| §26 capability | Status | Target phase / seam |
|---|---|---|
| Account-specific fee rates (`fetch_account_fee_rate`, `fetch_fee_schedule` per the §9 adapter contract) | NOT IMPLEMENTED | Phase 3 (shadow, §50): first phase with read-only credentials. Until then: manual config per §2 |
| VIP tiers and tier changes during operation | NOT IMPLEMENTED | Phase 3+; requires periodic re-fetch + re-validation of open assumptions |
| Maker fees / rebates in any calculation | NOT IMPLEMENTED (config field validated only) | Gated on the §24 maker-taker strategy, which stays disabled |
| Exchange-token discounts (e.g. fee-token balances) | NOT IMPLEMENTED | Phase 3+; must be modeled conservatively or disabled |
| Fee charged in base / quote / third asset, per fill | NOT IMPLEMENTED (quote-terms equivalence, §1.4) | Phase 2 (paper, §49): the fill simulator must emit fee amount + fee asset per partial fill |
| Partial-fill fee accounting | NOT IMPLEMENTED | Phase 2: accounting engine sums actual per-fill fees, never re-derives from notional |
| Estimated vs actual fee records (`estimated_fee`, `actual_fee`, `fee_asset`, `fee_rate_source`, `fee_rate_timestamp`, `fee_tier_id`) | NOT IMPLEMENTED | Phase 2 record schema; §34 requires fees separated in accounting, never mixed into gross |
| Fee-uncertainty reserve (conservative fee + wider reserve + "estimated" flag when the exact rate is unknown; block live if uncertainty is material) | NOT IMPLEMENTED | Phase 2 config seam alongside the §15 reserves (see `docs/ARBITRAGE_MATHEMATICS.md` §4); live-blocking behavior is a Phase-3 gate |
| Fee changes mid-operation (schedule updates without restart) | NOT IMPLEMENTED | Phase 3+; config is currently loaded once at boot |

Design constraints the future engine inherits from what is already built:
fee math stays in pure, exactly-unit-tested functions (`arb/signals/fees.py`
is the pattern); every added fee or reserve term must preserve the §39
monotonicity properties; and when exact fee information is uncertain the
engine must choose the conservative side and say so in the record — the same
philosophy as `config/instruments.yaml`'s "too-high minimum is safe,
too-low is not" rule.
