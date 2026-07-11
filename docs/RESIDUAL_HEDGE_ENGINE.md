# Residual Hedge Engine

> **Status: SPECIFIED — NOT IMPLEMENTED.**
> No hedging, order-placement, or exposure-tracking code exists in this
> repository today; `arb/execution/` is intentionally empty and the only risk
> component in code is the Phase-1 market-data staleness monitor
> (`arb/risk/staleness.py`). This document specifies the engine that
> Milestone 2 (§49, paper execution) must implement against simulated fills
> and that Phase 3 (limited live trading) will run against real venues.
> Sources: CLAUDE.md §21, with the mismatch inputs from §20.

## 1. Purpose and objective

When a dual-leg arbitrage cycle fills unevenly — one leg fills, the other
rejects; or the legs fill different quantities — the system is left holding
an unhedged directional position ("residual exposure"). The residual hedge
engine must neutralize that position quickly and safely.

The objective is fixed by the spec and must shape every design decision:

> **Minimize risk-adjusted loss — not preserve the originally expected
> profit at any cost.**

Concretely: the engine must be willing to close a residual at a small certain
loss rather than hold it hoping the original spread returns. Waiting for the
opportunity to reappear is explicitly forbidden (§20). Uncontrolled residual
exposure is a P0 condition in the iteration priority order (§42).

## 2. Trigger and inputs (§20)

The execution coordinator (see `docs/EXECUTION_STATE_MACHINE.md`) computes,
after every fill update, the cycle's mismatch fields:

| Input field | Role in hedging |
|---|---|
| `net_base_delta` | Signed residual quantity to neutralize (long if buys exceed sells). |
| `net_quote_delta` | Quote-side result so far; input to loss accounting. |
| `effective_average_buy_price` / `effective_average_sell_price` | Basis for computing hedge loss vs. the executed legs. |
| `actual_buy_fees` / `actual_sell_fees` | Already-incurred costs; included in cycle accounting, per fee asset. |
| `remaining_open_quantity` | Quantity that must be cancelled before the residual is final. |
| `residual_exposure_value` | `net_base_delta` marked at a fresh reference price; compared against limits. |

The engine engages when the cycle reaches `MISMATCHED` and the confirmed
residual exceeds tolerance (`HEDGE_REQUIRED` state). Spec example: bought
1.00 BTC, sold 0.82 BTC ⇒ residual long 0.18 BTC, evaluated immediately.

## 3. Unmatched-fill procedure (§21, normative order)

When fills are unmatched the engine must execute these steps in order:

1. **Freeze new cycles involving the affected asset.** No new arbitrage
   cycles may open on that asset until the residual is resolved.
2. **Cancel safe-to-cancel open quantities.** Remaining open remainders on
   either leg are cancelled so the residual stops moving.
3. **Confirm cancellation.** Cancel requests are not assumed successful;
   confirmation comes from private order events (or REST when uncertain).
4. **Reconcile actual fills.** Cancellation may race a final fill; the fill
   record must be authoritative before sizing the hedge.
5. **Calculate exact residual exposure** from reconciled fills, in exact
   decimal arithmetic (partial fills must create *exact* residual exposure —
   invariant 40.12).
6. **Obtain fresh order books from all eligible venues.** A hedge must never
   price off stale or invalid books (invariants 40.1, 40.2 apply to hedge
   orders exactly as to arbitrage orders).
7. **Determine the safest hedge route** across the eligible venues (see §4),
   using depth-aware pricing.
8. **Validate the hedge with the central risk engine.** No hedge order may
   bypass risk (§28); the approval carries price limits and expiry.
9. **Submit a price-protected hedge** — a marketable limit order with an
   explicit worst-price bound derived from `MAX_HEDGE_SLIPPAGE_BPS`, never an
   unbounded market order.
10. **Reconcile the hedge** — fills, fees, and resulting balances, through
    the same reconciliation machinery as primary legs.
11. **Generate an incident record** (see §7).

Steps 1–5 belong to the `MISMATCHED → CANCELLING_REMAINDERS →
RESIDUAL_EXPOSURE` transitions of the execution state machine; steps 6–10
correspond to `HEDGE_REQUIRED → HEDGING → HEDGED`; every step must be
journaled (§34) so a restart can resume mid-procedure (invariant 40.16).

## 4. Hedge venues

Eligible hedge routes, per the spec:

| Venue option | Notes |
|---|---|
| Original buy venue | Sell back the excess base where it was bought. |
| Original sell venue | Buy back the shortfall where it was sold. |
| Another approved exchange | Only venues on the approved list with valid, fresh books and available inventory. |
| Approved derivatives venue | **Future phase only** — all derivatives functionality is disabled initially (§25) and requires separate eligibility, risk controls and human authorization. |

Route selection must weigh depth-aware execution cost, book freshness, venue
health, and available inventory — it is a safety decision, not a
profit-recovery decision.

## 5. Limits

The engine must enforce four configured limits. All are risk limits under
§29: they cannot be changed without an authorized, journaled audit record,
and must never be increased automatically.

| Limit | Meaning |
|---|---|
| `MAX_RESIDUAL_EXPOSURE` | Maximum residual position value the system may carry at any moment. Above it, hedging is mandatory, not advisory. |
| `MAX_RESIDUAL_DURATION_MS` | Maximum time a residual may remain unhedged, measured on the monotonic clock from residual confirmation. |
| `MAX_HEDGE_SLIPPAGE_BPS` | Ceiling on hedge slippage vs. the reference price; bounds the hedge order's protective limit price. |
| `MAX_HEDGE_LOSS` | Maximum acceptable loss on the hedge itself. A hedge expected to exceed this must not be submitted automatically. |

## 6. Kill-switch escalation

If the engine cannot hedge inside the approved limits — no eligible venue,
insufficient depth within `MAX_HEDGE_SLIPPAGE_BPS`, expected loss above
`MAX_HEDGE_LOSS`, or `MAX_RESIDUAL_DURATION_MS` exceeded — it must:

1. **Trigger the relevant kill switch** (residual-exposure switch at minimum;
   asset/exchange switches as applicable, §30).
2. **Preserve current state** — journal the exact residual, attempted routes,
   rejection reasons, and all order/fill records; the cycle enters
   `MANUAL_INTERVENTION`.
3. **Escalate for human intervention** — operator notification with the
   incident record; automatic restart is not permitted (§30: authorized
   restart required).

A fired kill switch blocks **new** arbitrage cycles but must preserve
emergency hedging and reconciliation capability (§30, invariant 40.18) —
stopping normal trading must never disable the recovery path, and emergency
hedge/cancel/reconcile traffic draws on reserved rate-limit capacity that
normal activity cannot exhaust (§33).

## 7. Incident record

Every engagement of the hedge engine (step 11) must produce an append-only
incident record containing at least:

- cycle ID, affected asset/symbol, venues of both original legs;
- the full §2 mismatch snapshot (all eight fields) at residual confirmation;
- cancellation actions and their confirmations;
- book snapshots (or journal references) used for route selection;
- routes considered, route chosen, and the reason codes for rejected routes;
- risk-engine decision (`risk_snapshot_id`, approved limits, expiry);
- hedge order(s): client order IDs, prices, protective limits, fills, fees;
- realized hedge loss, recorded under the dedicated **hedge loss** accounting
  category (§34) — never blended into gross or net arbitrage profit;
- residual duration (monotonic), limit values in force, and whether any limit
  was breached;
- final outcome: `HEDGED`, or kill-switch escalation with operator handoff.

## 8. Verification obligations

Before the engine is trusted in paper mode, Milestone 2 must provide: unit
tests for residual calculation across partial-fill permutations (including
the 1.00/0.82 spec example); property evidence that partial fills create
exact residual exposure and that residual exposure always triggers hedge
evaluation (invariants 40.12, 40.13); chaos tests for one-leg rejection,
duplicate and out-of-order fills, and cancel-vs-fill races; restart tests
resuming the §3 procedure from every intermediate step; and accounting tests
showing hedge losses reconcile to (simulated) fills and are reported
separately from arbitrage P&L. Phase 3 live qualification additionally
requires measured hedge latency, hedge slippage, and hedge failure rate
within the §44 thresholds.
