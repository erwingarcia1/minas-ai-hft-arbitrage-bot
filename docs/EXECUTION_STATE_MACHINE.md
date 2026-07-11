# Execution State Machine — Dual-Leg Execution Coordinator

> **Status: SPECIFIED — NOT IMPLEMENTED.**
> No execution code exists in this repository today: `arb/execution/` is
> intentionally empty, no order is ever constructed or sent, and no API keys
> are read. The only related code that exists is the Phase-1 staleness monitor
> (`arb/risk/staleness.py`), which exposes the `on_unusable` hook where the
> Phase-2 kill switch will attach. This document specifies the coordinator
> that Milestone 2 (§49, paper execution) must implement against simulated
> fills, and that Phase 3 (limited live trading, §5 Mode 5) will later drive
> against real venues. Sources: CLAUDE.md §18, §19, §20, §31, §32.

## 1. Purpose and scope

Cross-exchange arbitrage has no atomic two-phase commit: one venue may fill
while the other rejects (§4). The execution coordinator must therefore manage
the buy leg and the sell leg as **one logical cycle** with an explicit,
persisted state machine, so that every partial outcome — one-leg fill, partial
fill, unknown order state, mid-cycle disconnect — resolves to a defined state
with a defined recovery action, never to guesswork.

Scope of this document:

- the cycle state set and transition rules (§19),
- the taker-taker per-leg procedure (§18),
- fill-mismatch calculation (§20),
- client-order-ID and idempotency rules (§31),
- disconnect and exchange-failure handling during execution (§32).

Residual-exposure handling after a confirmed mismatch is specified in
`docs/RESIDUAL_HEDGE_ENGINE.md`. Risk approval preceding a cycle is specified
in `docs/RISK_ENGINE.md`.

## 2. Cycle states (§19)

The coordinator must implement exactly this state set. A *cycle* is one
detected opportunity carried through both legs to completion or recovery.

| State | Meaning |
|---|---|
| `DETECTED` | Opportunity emitted by the detector; nothing validated yet. |
| `VALIDATING` | Re-checking book freshness/integrity, instrument rules, sizing, fees against live state. |
| `RISK_APPROVED` | Central risk engine returned an unexpired approval (approved quantity + price limits + `risk_snapshot_id`). |
| `INVENTORY_RESERVED` | Required quote balance (buy venue) and base balance (sell venue) reserved; reservations journaled. |
| `SUBMITTING_BOTH_LEGS` | Both leg orders serialized and dispatched as close together as technically possible. |
| `BUY_ACK_PENDING` | Buy order sent; acknowledgement not yet received. |
| `SELL_ACK_PENDING` | Sell order sent; acknowledgement not yet received. |
| `BUY_OPEN` | Buy order acknowledged and resting/working. |
| `SELL_OPEN` | Sell order acknowledged and resting/working. |
| `BUY_PARTIAL` | Buy leg partially filled. |
| `SELL_PARTIAL` | Sell leg partially filled. |
| `BUY_FILLED` | Buy leg completely filled. |
| `SELL_FILLED` | Sell leg completely filled. |
| `MATCHED` | Net base delta across both legs is zero (within one quantity step); no residual exposure. |
| `MISMATCHED` | Fill quantities differ; residual exposure exists (see §5 below). |
| `CANCELLING_REMAINDERS` | Cancelling safe-to-cancel open remainder quantity on either leg. |
| `RESIDUAL_EXPOSURE` | Exact residual computed and confirmed after cancellation + reconciliation. |
| `HEDGE_REQUIRED` | Residual exceeds tolerance; hedge engine engaged. |
| `HEDGING` | Price-protected hedge order(s) in flight. |
| `HEDGED` | Residual neutralized within approved limits. |
| `RECONCILING` | Verifying orders, fills, fees and balances against venue records. |
| `COMPLETED` | Cycle fully reconciled; realized P&L journaled. |
| `RECOVERED` | Cycle ended abnormally but all exposure resolved and accounted. |
| `FAILED` | Cycle ended without fills (e.g. both legs rejected); no exposure; reservations released. |
| `MANUAL_INTERVENTION` | Automatic recovery exhausted or forbidden by limits; operator action required. Terminal until an authorized human acts. |

Leg-level states (`BUY_*`/`SELL_*`) are tracked independently per leg (§1.7 of
the primary objective: "monitors acknowledgements and fills independently");
the cycle state is derived from the pair of leg states plus recovery context.

### 2.1 Transition rules and triggers

| From | To | Trigger |
|---|---|---|
| `DETECTED` | `VALIDATING` | Coordinator accepts the candidate. |
| `VALIDATING` | `RISK_APPROVED` | Risk engine approval received and unexpired. |
| `VALIDATING` | `FAILED` | Validation failure (stale/invalid book, rule violation, sizing below minimums). |
| `RISK_APPROVED` | `INVENTORY_RESERVED` | Both legs' inventory reserved atomically in the local ledger. |
| `RISK_APPROVED` | `FAILED` | Reservation impossible (insufficient available balance) or approval expired. |
| `INVENTORY_RESERVED` | `SUBMITTING_BOTH_LEGS` | Orders serialized; final pre-send checks pass. |
| `SUBMITTING_BOTH_LEGS` | `BUY_ACK_PENDING` + `SELL_ACK_PENDING` | Requests written to their venue connections. |
| `*_ACK_PENDING` | `*_OPEN` | Venue acknowledgement received. **Acknowledgement is not a fill** (§18, invariant 40.11). |
| `*_ACK_PENDING` | ack-timeout handling | Ack deadline exceeded → order state is *unknown*, not rejected; enter the §6 uncertain-state procedure. |
| `*_OPEN` | `*_PARTIAL` | First partial execution report on the private stream. |
| `*_OPEN` / `*_PARTIAL` | `*_FILLED` | Final execution report; cumulative quantity equals order quantity. |
| `*_OPEN` / `*_PARTIAL` | leg closed unfilled/short | IOC remainder cancelled by venue, or explicit rejection. |
| both legs terminal | `MATCHED` | Net base delta zero within one quantity step. |
| both legs terminal | `MISMATCHED` | Any other combination of fills. |
| `MISMATCHED` | `CANCELLING_REMAINDERS` | Open remainder quantity exists on either leg. |
| `MISMATCHED` / `CANCELLING_REMAINDERS` | `RESIDUAL_EXPOSURE` | Cancellations confirmed; fills reconciled; exact residual computed (§5). |
| `RESIDUAL_EXPOSURE` | `HEDGE_REQUIRED` | Residual exceeds configured tolerance (see RESIDUAL_HEDGE_ENGINE.md). |
| `RESIDUAL_EXPOSURE` | `RECONCILING` | Residual within tolerance; no hedge needed. |
| `HEDGE_REQUIRED` | `HEDGING` | Risk-validated, price-protected hedge submitted. |
| `HEDGING` | `HEDGED` | Hedge reconciled within `MAX_HEDGE_SLIPPAGE_BPS` / `MAX_HEDGE_LOSS`. |
| `HEDGE_REQUIRED` / `HEDGING` | `MANUAL_INTERVENTION` | Hedge cannot complete inside approved limits; relevant kill switch fires; state preserved. |
| `MATCHED` / `HEDGED` | `RECONCILING` | Begin final verification against venue order/fill/balance records. |
| `RECONCILING` | `COMPLETED` | Every fill, fee and balance change reconciles; realized P&L journaled. |
| `RECONCILING` | `RECOVERED` | Cycle abnormal but exposure fully resolved and accounted. |
| `RECONCILING` | `MANUAL_INTERVENTION` | Reconciliation mismatch exceeds the configured limit (§29). |
| any pre-submit state | `FAILED` | Abort before any order left the process; reservations released. |

Forbidden transitions (must be structurally impossible, not merely avoided):

- No path may reach `SUBMITTING_BOTH_LEGS` without passing `RISK_APPROVED`
  and `INVENTORY_RESERVED` in order (invariants 40.3, 40.6).
- No path may treat `*_ACK_PENDING` timeout as a rejection (§31, invariant 40.14).
- No path may leave `MISMATCHED` except toward residual quantification —
  the coordinator must never wait for the original spread to return (§20).

## 3. Persistence and restart recovery

Every state transition must be persisted to the append-only journal (§34)
**before** the transition takes effect, with at minimum: cycle ID, prior
state, new state, trigger, leg snapshots (order IDs, cumulative fills, fees),
reservation references, `risk_snapshot_id`, and monotonic + wall-clock
timestamps (§12).

On process restart the coordinator must, before permitting any new cycle:

1. Load every cycle whose last journaled state is non-terminal.
2. For each, reconcile actual venue state via private-stream replay and REST
   (`fetch_order`, `fetch_fills`, `fetch_balances` from the adapter contract).
3. Resume the cycle at the reconciled state, or drive it through
   `RESIDUAL_EXPOSURE`/`RECONCILING` to a terminal state.
4. Journal the recovery itself.

This is invariant 40.16: *every incomplete cycle survives restart*. A cycle
that cannot be reconciled automatically must land in `MANUAL_INTERVENTION`,
never be silently dropped.

## 4. Taker-taker leg procedure (§18)

Taker-taker is the only strategy eligible for first live use. The coordinator
must submit an aggressive buy on venue A and an aggressive sell on venue B as
close together as technically possible, using **protected, marketable IOC
limit orders — never unbounded market orders**.

For each leg, in order:

1. Determine the maximum acceptable price (from the depth-aware sizing that
   priced the opportunity, §15–§16).
2. Round the price to the venue's tick rules (never in the adverse direction
   that would exceed the approved limit).
3. Apply the slippage ceiling: the limit price must cap worst-case execution
   at the risk-approved buy/sell limits.
4. Generate the unique client order ID (§5.1 below).
5. Reserve the required balance in the local inventory ledger.
6. Submit the order.
7. Track the acknowledgement against its deadline.
8. Track partial and complete fills from the private execution stream.
9. Reconcile the leg through private streams as the primary source of truth.
10. Use REST queries only when state remains uncertain after stream evidence.

**An order acknowledgement must never be interpreted as a fill** (invariant
40.11). Fills come only from execution reports, and final accounting comes
only from reconciliation.

## 5. Fill-mismatch calculation (§20)

After every fill update, and again after remainder cancellation, the
coordinator must compute for the cycle:

| Field | Definition |
|---|---|
| `net_base_delta` | bought base quantity − sold base quantity, across both legs. |
| `net_quote_delta` | quote received − quote spent, across both legs. |
| `effective_average_buy_price` | quote spent ÷ base bought (fill-weighted). |
| `effective_average_sell_price` | quote received ÷ base sold (fill-weighted). |
| `actual_buy_fees` | fees actually charged on the buy leg, per fee asset (§26). |
| `actual_sell_fees` | fees actually charged on the sell leg, per fee asset. |
| `remaining_open_quantity` | unfilled quantity still open on either leg. |
| `residual_exposure_value` | `net_base_delta` marked against a fresh reference price. |

All arithmetic must use exact decimal / fixed-point types, never binary
floating point (§6). Worked example from the spec: bought 1.00 BTC, sold
0.82 BTC ⇒ residual **long 0.18 BTC**. That residual must be evaluated
immediately by the hedge engine — the coordinator must not hold the position
waiting for the original opportunity to return.

## 6. Client order IDs and idempotency (§31)

### 6.1 ID composition

Every order must carry a deterministic client order ID encoding:

```text
system ID | exchange | cycle ID | leg (BUY/SELL) | attempt number | strategy version
```

The encoding must respect each venue's character-set and length restrictions
(recorded per venue in `docs/EXCHANGE_CAPABILITY_MATRIX.md`); the mapping from
the logical tuple to the venue string must be injective and reversible so any
venue-side order can be attributed to its cycle, leg and attempt. Duplicate
events must not be able to create duplicate orders (property test 39.6;
invariant 40.15).

### 6.2 Resubmission after an uncertain response

**A timeout is never proof of rejection** (invariant 40.14; property test
39.7). Before any resubmission after a timeout, connection loss, or ambiguous
response, the coordinator must prove non-execution by executing, in order:

1. Search the venue for the client order ID.
2. Inspect the private order-event stream for the ID.
3. Inspect the private execution (fill) stream for the ID.
4. Query REST order status for the ID.
5. Reconcile balances against expected pre-order balances.
6. Resubmit **only when non-execution is proven** — and then under the same
   cycle ID with an incremented attempt number.

If non-execution cannot be proven, the leg stays in unknown state, the cycle
follows the §7 uncertainty policy, and blind resubmission remains blocked.

## 7. Disconnect and exchange-failure handling during execution (§32)

The coordinator must have a defined response to each of: public feed
disconnect, private feed disconnect, order-entry disconnect, partial regional
outage, exchange maintenance, REST timeout, WebSocket acknowledgement loss,
sequence gap, rate limit, authentication failure, API-key revocation, symbol
suspension, price-band rejection, duplicate order response, unknown order
state.

Universal policy during any uncertainty:

```text
Stop new cycles.
Preserve hedge capability.
Reconcile.
Do not guess.
```

Specific requirements:

- **Private-stream loss with orders in flight:** legs move to unknown state;
  the §6.2 proof procedure runs once connectivity or REST access permits; no
  cancel-and-resubmit races.
- **Order-entry disconnect:** where the venue offers disconnect-cancel-all,
  it must be used only if available on the account, authorized, and verified
  by tests (§8 capability registry); the coordinator must still reconcile
  after reconnect rather than assume cancellation happened.
- **Rate limits:** emergency cancellation, hedging and reconciliation draw on
  reserved rate-limit capacity that normal submission can never exhaust (§33).
- **Duplicate order response / duplicate fill events:** deduplicated by
  client order ID and execution ID; must not double-count fills or spawn
  duplicate orders.
- **Symbol suspension / price-band rejection mid-cycle:** the affected leg is
  terminal-rejected; the cycle proceeds to `MISMATCHED` handling with
  whatever the other leg did.

Kill switches (§30) triggered during execution block **new** cycles but must
never disable remainder cancellation, hedging, or reconciliation of cycles
already in flight (invariant 40.18).

## 8. Verification obligations

Milestone 2 must ship with, at minimum: unit tests for every transition in
§2.1 and every field in §5; property tests 39.4–39.10 (reservation
double-spend, duplicate events, timeout-resubmission, accounting balance);
chaos tests injecting each §7 failure with orders in flight; and restart
tests proving §3 recovery for every non-terminal state. The paper simulator
(§36) must exercise this exact state machine — only venue execution is
simulated, never the coordinator itself.
