# Inventory Management Engine

> **Status: SPECIFIED — NOT IMPLEMENTED.**
> No inventory, balance, or reservation code exists in this repository today.
> The current build is the Milestone 1 (CLAUDE.md §48) public market-data
> observer: `arb/execution/` is intentionally empty, no API keys are read, and
> the only sizing input is a configured list of clip notionals in
> `arb/signals/spread.py`. This document specifies the inventory engine that
> Milestone 2 (paper trading, virtual balances — §49) must implement first;
> Milestone 3 (shadow trading — §50) adds real read-only balances; live modes
> (§5, Modes 5–6) make reservations binding.

Normative sources: CLAUDE.md §4 (pre-funded model), §16 (sizing bounds), §22
(inventory engine), with constraints from §3, §19, §26, §28–§31, §34, §39–§40.

---

## 1. The pre-funded model (§4)

Cross-exchange arbitrage is not atomic. The system must assume that one
exchange may fill while the other rejects. Therefore:

- Every cycle must begin with sufficient **pre-funded inventory on both
  venues**: the buy venue must already hold enough quote asset; the sell venue
  must already hold enough base asset.
- No blockchain or inter-exchange transfer may occur during an arbitrage
  cycle (§2), and external transfers must never occur in the hot path
  (§40, invariant 21). Transfers are the concern of the rebalancing policy
  (`docs/REBALANCING_POLICY.md`), not of execution.
- Each completed cycle shifts the inventory distribution: the buy venue gains
  base and loses quote; the sell venue loses base and gains quote. The
  inventory engine must track this drift continuously, because it is what
  eventually exhausts one side and creates rebalancing pressure.

Inventory management is a first-class component (§4): the opportunity engine,
sizing engine, and risk engine shall all consume its state; no order may be
submitted for inventory that is not demonstrably available and reserved.

## 2. Scope by operating mode

| Mode (§5) | Balance source | Reservation semantics | Phase |
|---|---|---|---|
| Market-data observation | none — no balances exist | none | **implemented today** (no inventory code) |
| Historical replay | recorded/virtual | simulated, deterministic | Milestone 1–2 |
| Paper trading | virtual balances seeded from config | fully enforced against virtual state | Milestone 2 (§49) |
| Shadow trading | actual balances via private **read-only** feeds | enforced in decision logic; no orders exist | Milestone 3 (§50) |
| Limited / qualified live | actual balances, reconciled continuously | binding; violation is a P0 defect | Modes 5–6, after §44 qualification |

The same inventory engine code must serve every mode (§36): only the balance
source differs. Paper mode must not use a simplified reservation model.

## 3. Tracked state per (exchange, asset)

For every asset on every venue (and, where applicable, per strategy and per
account — §22), the engine must maintain the following record. All monetary
fields are fixed-point (`Decimal` in the current Python codebase; scaled
integers in a future hot path per §6) — binary floating point is prohibited
for balances (§6).

| Field | Type | Definition |
|---|---|---|
| `exchange` | `VenueName` | Venue identifier (interned, §13). |
| `asset` | `AssetId` | Canonical asset from the asset registry (§10). |
| `actual_balance` | `Decimal` | Last balance reported by the venue (or the virtual ledger in paper mode). |
| `available_balance` | `Decimal` | Derived: spendable now. See invariant I1 below. |
| `reserved_balance` | `Decimal` | Sum of all active reservations (§5 of this doc). |
| `target_balance` | `Decimal` | Strategy-derived target (§7 of this doc). |
| `minimum_balance` | `Decimal` | Floor below which no reservation may take the asset. |
| `maximum_balance` | `Decimal` | Ceiling above which further accumulation is rejected. |
| `inventory_deviation` | `Decimal` | `actual_balance − target_balance`, also tracked in bps of target for the §29 limit. |
| `pending_fills` | `Decimal` | Net quantity acknowledged or partially filled but not yet final — an acknowledgement is never a fill (§18, §40.11). |
| `unreconciled_balance` | `Decimal` | Difference between venue-reported balance and the journal-derived expected balance (§8 of this doc). |
| `as_of_wall_ns` / `as_of_mono_ns` | `int` | Timestamps of the last update (§12: monotonic for durations). |

### Invariants over this record

- **I1** `available_balance = actual_balance − reserved_balance − max(0, unreconciled_balance_hold)` — an unexplained shortfall reduces spendable funds; an unexplained surplus is never spendable until reconciled.
- **I2** `0 ≤ reserved_balance ≤ actual_balance` at all times.
- **I3** `reserved_balance` equals the exact sum of active reservation records; there is no other path that changes it.
- **I4** A spot sell may never exceed available base inventory (§40, invariant 5; property test §39.5).
- **I5** Reserved funds cannot be double spent (§40, invariant 7; property test §39.4).
- **I6** Every mutation is journaled (§34) before it takes effect on decisions.

## 4. Reservation semantics for dual-leg cycles

A cycle may enter `SUBMITTING_BOTH_LEGS` (§19) only from
`INVENTORY_RESERVED`: **both legs' inventory must be reserved before either
order is submitted** (§40, invariant 6). Reserving one leg and hoping the
other clears is prohibited — that is exactly the one-leg-fills failure the
pre-funded model exists to contain.

### 4.1 Reservation record

| Field | Type | Definition |
|---|---|---|
| `reservation_id` | `str` | Unique, deterministic, journaled. |
| `cycle_id` | `str` | The owning execution cycle (§19); also embedded in client order IDs (§31). |
| `leg` | `enum {BUY, SELL, HEDGE}` | Which leg this reservation funds. |
| `exchange` / `asset` | as above | What is being reserved, where. |
| `amount` | `Decimal` | Reserved quantity of `asset`. |
| `risk_snapshot_id` | `str` | The approving risk decision (§28). |
| `created_at` / `expires_at` | timestamps | Expiry is bounded by the risk decision's `expires_at` (§28) and applies **only before submission** (§4.3). |
| `state` | `enum` | `ACTIVE → PARTIALLY_CONSUMED → CONSUMED \| RELEASED` |

### 4.2 Reservation amounts must be conservative

- **Buy leg (quote asset):** reserve the worst-case approved cost —
  `quantity × approved_buy_limit_price` plus the estimated fee if the venue
  charges the fee in quote (§26). Rounding of the *reservation* is always
  upward; rounding of the *order quantity* is always downward to a valid
  `quantity_step` and must never round upward past available inventory (§16).
- **Sell leg (base asset):** reserve the full sell quantity, plus a fee buffer
  when the venue charges fees in base or another asset (§26). When the fee
  asset is uncertain, apply the conservative treatment of §26 (larger reserve,
  opportunity marked estimated).

### 4.3 Reservation lifecycle

```text
ACTIVE
  ├─ released before submission ............ RELEASED   (risk expiry, cycle abort)
  ├─ order submitted, fills arrive ......... PARTIALLY_CONSUMED / CONSUMED
  │    (consumed pro-rata against reconciled fills, including actual fees)
  ├─ order proven cancelled/rejected ....... remainder RELEASED
  └─ order state UNKNOWN (timeout, disconnect)
       ......... reservation is PINNED: it must NOT expire or release until
                 reconciliation proves the terminal order state (§31, §32).
                 A timeout is never proof of rejection (§40, invariant 14).
```

- Release of a consumed reservation's remainder happens only after fills are
  reconciled through the private stream, with REST used only when state
  remains uncertain (§18).
- Every transition is a journal event (§34: balance reservations are a
  first-class journal category).
- On process restart, active reservations must be rebuilt from the journal and
  re-attached to their incomplete cycles (§19; §40, invariant 16). A restart
  must never silently free reserved funds.

### 4.4 No double-spend

Reservation checks and updates for a given (exchange, asset) must be
serialized through a single writer (§13) so that two concurrent cycles cannot
both pass an availability check against the same funds. Property-based tests
must prove that no interleaving of reserve/consume/release operations spends
the same funds twice (§39.4) and that duplicate events cannot create duplicate
reservations (§39.6 analogue).

## 5. How inventory bounds opportunity size (§16)

The sizing engine evaluates candidate quantities against depth, balances,
reservations, exchange rules, and inventory targets, producing:

```text
maximum_theoretical_quantity      depth-only upper bound
maximum_executable_quantity       min(depth bound,
                                      available quote on buy venue / worst-case unit cost,
                                      available base on sell venue,
                                      exchange max quantity / max notional)
maximum_risk_approved_quantity    risk-engine cap (§28, §29)
profit_maximizing_quantity        argmax of expected net profit (§15)
selected_quantity                 min of all of the above, rounded DOWN to a
                                  quantity_step valid on both legs, then
                                  re-checked against min qty / min notional
```

Rules:

- `available_balance` (post-reservation), not `actual_balance`, is the input —
  funds reserved by other in-flight cycles are invisible to sizing.
- Rounding must never round upward in a way that exceeds available inventory
  (§16). The Milestone 1 instrument work already establishes the pattern:
  clip quantities are rounded down to a step valid on both legs, and a clip
  that violates a venue minimum is marked unexecutable (PROGRESS.md,
  Iteration 2 acceptance criteria).
- If `selected_quantity` falls below either venue's `minimum_quantity` or
  `minimum_notional` after rounding, the opportunity is rejected, not resized
  upward.

## 6. Inventory can reject profitable opportunities (§22)

An opportunity with positive expected net profit must still be rejected when
executing it would:

- push either venue's post-trade balance below `minimum_balance` or above
  `maximum_balance`;
- push `inventory_deviation` beyond the maximum inventory deviation risk
  limit (§29);
- create unacceptable inventory concentration on one venue or in one asset
  (§22), including concentration created by the *fee* leg;
- consume inventory that the hedge engine may need for residual-exposure
  recovery (§21: hedge capacity is a sizing input per §16).

Rejections must carry explicit reason codes through the risk response (§28)
and be journaled, so the "rejected-opportunity reasons" metric (§37) can
attribute them.

## 7. Targets and deviation (§22)

- Targets are maintained per exchange, asset, strategy, and account.
- Targets must be **strategy-derived**, not hard-coded. The 40% BTC / 60% USDT
  split in §22 is an illustrative example only.
- `inventory_deviation` is recomputed on every balance or reservation change
  and exported as a metric (§37: inventory deviation, capital utilization,
  unavailable capital).
- Deviation beyond the configured threshold generates a rebalancing
  recommendation (`docs/REBALANCING_POLICY.md`); it must also bias opportunity
  selection toward cycles that *reduce* deviation (natural reverse arbitrage)
  before any transfer is contemplated.

## 8. Reconciliation and `unreconciled_balance` (§34, §28)

After every cycle, and periodically, the engine must compare venue-reported
balances against the journal-derived expectation (initial balance + reconciled
fills ± actual fees ± reservations). Any difference is `unreconciled_balance`.

- A nonzero `unreconciled_balance` reduces `available_balance` (invariant I1),
  never increases it.
- A mismatch beyond the maximum reconciliation mismatch limit (§29) must
  trigger the reconciliation kill switch (§30): new cycles touching that
  (exchange, asset) are blocked while hedge capability is preserved.
- During feed uncertainty or reconnects, the §32 rule applies: stop new
  cycles, preserve hedge capability, reconcile, do not guess.

## 9. Interfaces

| Consumer | What it reads / writes |
|---|---|
| Risk engine (§28) | reads balances, reservations, deviation; its approval is a precondition for any reservation. |
| Execution coordinator (§19) | requests reservations (`INVENTORY_RESERVED` gate), reports fills for consumption, requests release. |
| Sizing engine (§16) | reads `available_balance` and targets; never reads `actual_balance` directly. |
| Hedge engine (§21) | may reserve with `leg=HEDGE`; hedge reservations take priority and are protected by kill-switch rules (§30: kill switches preserve emergency recovery). |
| Journal (§34) | receives every reservation, consumption, release, and reconciliation event, append-only. |
| Metrics (§37) | deviation, utilization, unavailable capital, rebalancing frequency. |

## 10. What exists in code today

Nothing of the above. The nearest existing seams are:

- `arb/signals/spread.py` — sizes by configured clip notionals only; this is
  where `maximum_executable_quantity` inputs will attach.
- `arb/risk/staleness.py` — the only risk component today; the Phase-2 risk
  manager (and with it the reservation gate) attaches at this seam
  (README, "Architecture").
- `arb/accounting/opportunities.py` — JSONL research sink; the append-only
  journal required by §34 is a superset of this pattern, not this file.
- `arb/execution/` — empty by design; the coordinator that consumes
  reservations lands here in Milestone 2.

## 11. Acceptance criteria (Milestone 2 gate)

1. Property tests prove: reserved funds cannot be double spent (§39.4); sold
   quantity never exceeds available base (§39.5); duplicate events cannot
   duplicate reservations (§39.6); rounding cannot create money (§39.3).
2. Automated evidence for invariants §40.5 (no oversell), §40.6 (both legs
   reserved), §40.7 (no double spend), §40.21 (no external transfers in the
   hot path).
3. Chaos test: process crash with active reservations and an in-flight order →
   restart rebuilds reservations from the journal, pins the uncertain one, and
   resumes the cycle (§39 chaos tests; §40.16).
4. Paper-trading simulator uses this engine unmodified (§36).
5. Every reservation transition appears in the journal with timestamps and
   `cycle_id`, and reconciles to virtual (later real) balance changes (§34).
