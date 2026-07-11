# Rebalancing Policy

> **Status: SPECIFIED — NOT IMPLEMENTED.**
> No rebalancing, transfer, or balance code exists in this repository today —
> by explicit policy, no fund-movement code exists at all (README: "this
> system will never move funds between venues; no transfer code exists by
> policy"). The current build is the Milestone 1 (CLAUDE.md §48) market-data
> observer. This document specifies the rebalancing recommendation engine for
> Milestone 2+ (alongside the inventory engine, §49). Execution of external
> transfers is **not automated in any specified phase**; it remains a human
> action outside the trading system.

Normative sources: CLAUDE.md §23 (rebalancing engine), with constraints from
§3 (safety defaults), §4 (transfers separated from execution), §15
(profitability reserves), §22 (inventory targets), §34/§37/§40/§46.

---

## 1. Principle: rebalancing is not arbitrage execution (§23)

Arbitrage cycles consume inventory distribution: every completed cycle moves
base toward the buy venue and quote toward the sell venue (§4). Rebalancing is
the separate, slower process that restores target distribution so trading can
continue. It must be kept strictly apart from live execution:

- External transfers must be separated from live execution (§4) and must never
  occur in the hot path (§40, invariant 21). No blockchain transfer occurs
  during an arbitrage cycle (§2).
- The rebalancing engine produces **recommendations**; it does not move funds.
  Any actual external transfer is executed by a human, or — in a future,
  separately specified and approved phase — by a separately secured system
  that does not share credentials with trading (§23).
- Rebalancing activity must never be reported as arbitrage profit; its costs
  are accounted separately and fed back into cycle profitability (§34, §15,
  and §7 of this doc).

## 2. Hard safety defaults (§3, §23)

| Setting | Default | Meaning |
|---|---|---|
| `AUTOMATIC_EXTERNAL_REBALANCING` | `false` | The system shall never initiate an external (cross-venue) transfer on its own. |
| `AUTOMATIC_EXTERNAL_TRANSFERS` | `false` | §3 blanket default; supersedes any strategy convenience. |
| `WITHDRAWAL_PERMISSIONS_ALLOWED` | `false` | Trading API keys must not possess withdrawal permission; the system must **reject** withdrawal-enabled credentials (§3; §40, invariant 19). |

Consequences:

- Even a correct, profitable rebalancing recommendation cannot be executed by
  the trading system across venues — by construction, its keys cannot move
  funds. This is a security boundary, not a missing feature (§38 threat model:
  withdrawal-enabled credentials are a listed threat).
- Enabling automatic external transfers of any kind requires the full §46
  human-authorization gate, a separately secured transfer system, and a
  separate security review. It is out of scope for every currently specified
  milestone.
- The operator-facing key checklist already published in the README (trade-only
  keys, withdrawals and transfers disabled, IP-whitelisted) is the
  Phase 2+ operational counterpart of these defaults.

## 3. Rebalancing methods, ranked (§23)

Methods are ranked by preference: prefer methods that move no funds off any
venue, then methods confined to a single venue, then human-executed transfers,
then future institutional services. A lower-ranked method may be recommended
only when higher-ranked methods cannot restore targets at acceptable cost.

| Rank | Method | Funds leave a venue? | Cost drivers | Status |
|---|---|---|---|---|
| 1 | **Natural reverse arbitrage** — bias opportunity selection toward cycles whose direction reduces inventory deviation | No | None beyond normal cycle costs; may even be profitable | Milestone 2+ (scoring bias in the opportunity engine) |
| 2 | **Local trades on the same exchange** — convert asset mix in place (e.g. sell excess base into quote on the overweight venue) | No | Taker/maker fees, slippage, spread | Milestone 2+ (recommendation only; execution follows normal risk-gated order path in live phases) |
| 3 | **Stablecoin conversions** — adjust quote-asset mix | No (on-venue) | Conversion fees, stablecoin spread and liquidity, §27 haircut; never assume 1 USDT = 1 USDC = 1 USD | Milestone 2+ (recommendation only), subject to §27 controls |
| 4 | **Scheduled manual transfers** — human moves funds between venues | Yes | Withdrawal/network fees, settlement delay, operational risk | Recommendation record only; execution always human (§23, §46) |
| 5 | **Prime broker / settlement service** | Yes (off-exchange settlement) | Service fees, onboarding, counterparty terms | Future approved phase only (§23) |
| 6 | **Authorized external transfers in a separately secured system** | Yes | As rank 4, plus the cost of operating the secured system | Future phase; separate credentials, separate security review, §46 authorization |

Ranks 1–3 are the only methods the trading system itself may ever act on, and
only through the normal risk-gated execution path (risk engine approval,
reservation, journaling — no special bypass; §28 applies to rebalancing trades
exactly as to arbitrage legs). Ranks 4–6 exist in the system solely as
recommendation records and audit trails.

## 4. When recommendations are generated

The trigger is the inventory engine (`docs/INVENTORY_ENGINE.md`):

1. `inventory_deviation` for some (exchange, asset) exceeds its configured
   threshold, or a balance approaches `minimum_balance` / `maximum_balance`.
2. The engine first attempts rank 1: it raises the selection bias for
   deviation-reducing cycle directions and reports whether natural flow is
   expected to restore targets within the configured horizon.
3. If natural flow is insufficient, it evaluates ranks 2–3 and, failing those,
   emits a rank-4 manual-transfer recommendation for operator action.
4. Every generated, superseded, approved, or rejected recommendation is a
   journal event (§34, administrative changes) and a metric
   (§37: rebalancing frequency, rebalancing cost).

Recommendations are advisory state, not orders: they expire, they can be
superseded by newer balance snapshots, and acting on rank-4+ recommendations
requires explicit human approval recorded in the audit trail (§46).

## 5. Recommendation record (§23)

| Field | Type | Definition |
|---|---|---|
| `recommendation_id` | `str` | Unique, journaled identifier. |
| `asset` | `AssetId` | Canonical asset to move or convert (§10 registry). |
| `source_exchange` | `VenueName` | Venue holding the excess. |
| `destination_exchange` | `VenueName` | Venue holding the deficit (equal to source for rank 2–3 local actions). |
| `amount` | `Decimal` | Quantity to move/convert; fixed-point, never binary float (§6). |
| `reason` | `str` (reason code + detail) | E.g. deviation breach, minimum-balance approach, hedge-capacity restoration. |
| `expected_cost` | `Decimal` | Total expected cost in quote terms: fees + slippage + spread (+ §27 stablecoin haircut where applicable). |
| `network_cost` | `Decimal` | Withdrawal/network fee for cross-venue transfers (ranks 4–6); zero for on-venue methods. |
| `expected_settlement_time` | duration | Time until funds are usable at the destination (network confirmations, venue crediting delay). |
| `operational_risk` | `enum {LOW, MEDIUM, HIGH}` + notes | E.g. network congestion, venue withdrawal-freeze risk, wrong-network loss risk. |
| `required_human_approval` | `bool` | `true` for all rank-4+ methods, without exception. |
| `method` | `enum` | One of the ranked methods in §3 of this doc. |
| `created_at` / `expires_at` | timestamps | Stale recommendations must not be acted on. |
| `status` | `enum` | `OPEN → APPROVED \| REJECTED \| SUPERSEDED \| EXPIRED \| COMPLETED` |

## 6. Costing rules

- `expected_cost` must be computed from live depth and current fee schedules,
  not from static assumptions, and must apply §27 in full for any stablecoin
  leg: conversion price, spread, liquidity, fees, depeg haircut. A
  recommendation whose benefit depends on treating unequal quote currencies as
  identical must not be generated (§27).
- For rank-4 transfers, `expected_cost` includes `network_cost` plus the
  opportunity cost of `expected_settlement_time` (capital unusable in transit
  is unavailable capital, §37).
- Cost estimates carry the same conservatism rule as fees (§26): when
  uncertain, estimate high and mark the recommendation estimated.

## 7. Rebalancing cost must price into arbitrage profitability (§15, §23)

The §15 expected-net-profit formula contains an explicit
`Inventory Rebalancing Reserve(q)` term. The rebalancing engine is the source
of that number:

- Each candidate cycle's inventory displacement is priced at the expected
  marginal cost of eventually reversing it — derived from the current cheapest
  viable method in §3 (zero when natural reverse flow is expected to absorb
  it, rising toward per-unit transfer cost as deviation grows).
- A cycle whose expected net profit survives only by ignoring the rebalancing
  it causes must not execute (§15 execution conditions include this reserve).
- Realized rebalancing costs are accounted in their own category —
  `Inventory-rebalancing cost` is a separate line in §34 accounting and must
  never be netted silently into gross profit. §37 requires reporting
  rebalancing frequency and cost as first-class financial metrics.

## 8. What exists in code today

Nothing. There are no balances, no transfer code, no recommendation records.
Grounding for later phases:

- `arb/signals/spread.py` evaluates both directions of every venue pair
  (`itertools.permutations`), which is the hook rank-1 natural-reverse-arb
  scoring will bias.
- `arb/accounting/opportunities.py` is the JSONL research sink; the journal
  that must record recommendations (§34) will be a superset of this pattern.
- `.env.example` and the README key checklist already document the
  no-withdrawal-permission requirement operators must follow before Phase 2.

## 9. Acceptance criteria (Milestone 2+ gate)

1. Recommendations are generated from inventory deviation with all §5 fields
   populated, journaled append-only, and visible in metrics (§34, §37).
2. No code path exists that initiates an external transfer; static checks and
   tests demonstrate `AUTOMATIC_EXTERNAL_REBALANCING=false` cannot be
   overridden without the §46 authorization gate and an audit record (§29
   audit rules apply to this limit as to any other).
3. Withdrawal-enabled API credentials are rejected at key inspection
   (§40, invariant 19) — tested with a simulated entitlement response.
4. The `Inventory Rebalancing Reserve(q)` term is present in the opportunity
   profitability calculation and property tests confirm that increasing the
   reserve can only decrease expected profit (§39.1 analogue).
5. Stablecoin-related recommendations apply §27 haircuts, and the
   stablecoin-risk kill switch (§27, §30) blocks such recommendations when
   tripped.
6. Realized rebalancing cost reconciles as its own accounting category and is
   excluded from gross arbitrage profit (§34).
