# Central Risk Engine

> **Status: SPECIFIED — NOT IMPLEMENTED** (Phase-1 precursor only).
> The only risk component in code today is the market-data staleness monitor
> (`arb/risk/staleness.py`), which gates the Phase-1 spread detector on book
> freshness and carries an explicit kill-switch seam comment for Phase 2.
> No order placement, no limit enforcement beyond staleness, and no kill
> switch exist. This document specifies the central risk engine that
> Milestone 2 (§49, paper execution) must implement and enforce against
> simulated cycles, and that Phase 3 (limited live trading) will run against
> real venues. Sources: CLAUDE.md §28 (engine and decision shape), §29
> (limits), §30 (kill switches), §17 (confidence model), with cross-references
> to §21, §32, §40 and §42.

## 1. Governing principle: no bypass

**No strategy and no exchange adapter may bypass the risk engine** (§28).
Every arbitrage cycle, hedge order, and cancellation-with-replacement must
pass through a single risk-evaluation entry point before any order request is
serialized. This is invariant §40.3 ("No order bypasses the risk engine")
and its violation is a P0 condition in the iteration priority order (§42,
"P0 risk-engine bypass").

Two structural consequences the implementation must honor:

1. The execution coordinator (see `docs/EXECUTION_STATE_MACHINE.md`) may only
   transition `VALIDATING → RISK_APPROVED` by holding an unexpired approval
   from this engine. There is no other producer of that transition.
2. The risk engine sits *inside* the decision hot path, so its evaluation
   must be deterministic, allocation-light, and free of blocking I/O (§13).
   It reads pre-aggregated state (balances, exposures, health flags) that
   other components maintain; it never fetches from an exchange during a
   decision.

## 2. What exists today (Phase 1)

Phase 1 places no orders, so the only enforceable risk control is *signal
suppression on bad data*. That control exists and runs:

- **`arb/risk/staleness.py` — `StalenessMonitor`.** Every 100 ms it
  recomputes, per venue, whether **all** of that venue's books are synced and
  younger than `risk.staleness_ms` (default 500 ms in
  `config/config.yaml`). On a usable→unusable transition it calls
  `on_unusable` (wired to `SpreadDetector.on_venue_unusable`), which
  force-closes every open opportunity touching that venue with reason
  `venue_unusable`. A per-book fresh→stale backstop (`on_symbol_degraded`)
  re-evaluates the symbol so a single stale book cannot leave an opportunity
  dangling when its venue was already unusable via another symbol. Gauges
  (`book_age_ms`, `venue_usable`) refresh every tick.
- **Detector-side gate.** `SpreadDetector._usable` independently rejects any
  book that is unsynced or older than `staleness_ms` at evaluation time —
  the Phase-1 form of "an invalid book cannot authorize a trade" (§40.10).
- **Fatal-by-design monitor.** The monitor's task ending takes the whole
  process down (main gathers it), because running blind is worse than not
  running. This posture carries forward: in later phases a dead risk engine
  must halt trading, never default to permissive.
- **The kill-switch seam.** The module docstring in `arb/risk/staleness.py`
  marks `on_unusable` as the Phase-2 hook: the same transition that today
  closes theoretical opportunities will additionally halt signal→execution
  flow, cancel all open orders on every venue, and latch a halted state
  requiring manual reset — within one event-loop cycle of detection.
- **Validated-but-unenforced config.** `risk.max_position_usdt` and
  `risk.max_daily_loss_usdt` are parsed and strictly validated today but
  enforce nothing, because no positions exist in Phase 1. They become live
  limits in Milestone 2 (`max_asset_exposure` and `max_daily_realized_loss`
  in the §29 table below).

Everything else in this document is specification.

## 3. Risk decision interface (§28)

The engine must evaluate a candidate action (arbitrage cycle, hedge, resize)
and return exactly one decision object:

```json
{
  "approved": false,
  "reason_codes": [],
  "approved_quantity": null,
  "approved_buy_limit": null,
  "approved_sell_limit": null,
  "risk_snapshot_id": "",
  "expires_at": ""
}
```

Field semantics:

| Field | Type | Meaning |
|---|---|---|
| `approved` | bool | Sole authorization flag. Anything other than `true` blocks the action. |
| `reason_codes` | list of strings | Machine-readable denial (or constraint) reasons; never empty on denial. Deterministic and enumerable — no free text in the hot path. |
| `approved_quantity` | Decimal / null | Quantity ceiling the engine authorizes; may be lower than requested (§16 `maximum_risk-approved_quantity`). `null` on denial. |
| `approved_buy_limit` | Decimal / null | Worst acceptable buy price (price-protection ceiling for the buy leg). |
| `approved_sell_limit` | Decimal / null | Worst acceptable sell price (price-protection floor for the sell leg). |
| `risk_snapshot_id` | string | Identifier of the immutable snapshot of inputs the decision was computed from; journaled so every decision is auditable and replayable (§34, §35). |
| `expires_at` | timestamp | Hard expiry. **An expired decision cannot authorize a trade** — the coordinator must re-request, not reuse. |

Expiry rules:

- Decisions must expire **rapidly** (§28) — on the order of the data they
  were computed from. A decision must not outlive the book staleness bound
  (`risk.staleness_ms`) that made its inputs trustworthy.
- Expiry is checked at order-send time against a monotonic deadline, not
  only at approval time.
- A denied decision is never cached as reusable; conditions are re-evaluated
  per request.

Suggested `reason_codes` naming (illustrative, derived from the §28 input
list — the implementation must keep one enumerated, tested registry):
`MARKET_DATA_UNHEALTHY`, `BOOK_STALE`, `BOOK_INTEGRITY_FAILED`,
`CROSS_BOOK_SKEW`, `CLOCK_UNSYNCED`, `INSUFFICIENT_BALANCE`,
`FUNDS_RESERVED`, `INVENTORY_DEVIATION`, `NOTIONAL_LIMIT`,
`EXPOSURE_LIMIT`, `DAILY_LOSS_LIMIT`, `DAILY_DRAWDOWN_LIMIT`,
`TURNOVER_LIMIT`, `CONCURRENCY_LIMIT`, `RECENT_MISMATCH`,
`REJECTION_RATE`, `HEDGE_CAPACITY_UNAVAILABLE`, `LATENCY_DEGRADED`,
`STABLECOIN_RISK`, `KILL_SWITCH_ACTIVE`, `DECISION_EXPIRED`.

## 4. Evaluation inputs (§28)

The engine must evaluate **all** of the following before approving any
action. Absence or uncertainty of an input is grounds for denial, never for
assuming a safe value.

| Category | Inputs |
|---|---|
| Data health | market-data health, exchange health, private-stream health, clock synchronization, book freshness, book integrity, cross-exchange book skew |
| Funds | current balances, reserved balances, inventory targets |
| Exposure | order notional, asset exposure, exchange exposure, residual exposure |
| Running totals | daily turnover, daily realized loss, daily drawdown |
| Recent behavior | recent fill mismatch, recent rejection rate, recent hedge losses, recent latency |
| Systemic | stablecoin health, recovery capacity |

Notes:

- *Reserved balances* means the engine sees inventory net of reservations
  held by in-flight cycles, so reserved funds cannot be double-spent
  (§40.7). Approval and reservation must be atomic with respect to
  concurrent cycles.
- *Recovery capacity* means: can the system still cancel and hedge if this
  action goes wrong — rate-limit headroom reserved for emergencies (§33),
  hedge-venue liquidity (§21), and a live private stream. If recovery
  capacity is absent, no new cycle is approved regardless of expected
  profit.
- In Milestone 2 (paper), balances/exposures/loss totals come from the
  virtual-balance engine (§49); the evaluation code path must be the same
  one Phase 3 runs live (§36: only exchange execution is simulated).

## 5. Relationship to the opportunity confidence model (§17)

Upstream of the risk engine, the opportunity engine must compute a
**deterministic confidence score** per opportunity from: book freshness,
cross-exchange timestamp skew, spread stability, recent opportunity
lifetime, depth stability, recent fill ratio, recent rejection rate, network
latency, exchange health, volatility, adverse-selection history, and
residual hedge liquidity.

Constraints the spec fixes:

- The score must be explainable and backtestable. **No opaque LLM score**
  (§17), and no LLM anywhere in the trading path (§2, §40.4).
- An opportunity may be rejected even when its raw spread is positive.
- The confidence model filters candidates; it does not authorize anything.
  A high-confidence opportunity still requires a fresh, unexpired risk
  approval. The two share several inputs (freshness, skew, health, latency)
  but the risk engine re-checks them — confidence is advisory, risk is
  binding.

## 6. Risk limits (§29)

Every limit below must be implemented, config-driven, and enforced by the
central engine. Suggested keys extend the existing strict-validated `risk:`
block in `config/config.yaml` (unknown keys are fatal, which prevents a
misspelled limit from silently not existing).

| # | Limit (§29) | Suggested config key | Unit / shape |
|---|---|---|---|
| 1 | Maximum notional per cycle | `risk.max_cycle_notional_usdt` | quote notional |
| 2 | Maximum notional per exchange | `risk.max_exchange_notional_usdt` | quote notional, per venue |
| 3 | Maximum asset exposure | `risk.max_asset_exposure_usdt` | quote-valued, per asset (Phase-1 `risk.max_position_usdt` folds into this) |
| 4 | Maximum quote exposure | `risk.max_quote_exposure_usdt` | quote notional, per quote asset |
| 5 | Maximum inventory deviation | `risk.max_inventory_deviation_bps` | bps from target (§22) |
| 6 | Maximum concurrent cycles | `risk.max_concurrent_cycles` | count |
| 7 | Maximum cycles per symbol | `risk.max_cycles_per_symbol` | count, concurrent |
| 8 | Maximum daily turnover | `risk.max_daily_turnover_usdt` | quote notional per UTC day |
| 9 | Maximum daily realized loss | `risk.max_daily_realized_loss_usdt` | exists today as `risk.max_daily_loss_usdt` (validated, unenforced) |
| 10 | Maximum daily drawdown | `risk.max_daily_drawdown_usdt` | peak-to-trough per day |
| 11 | Maximum consecutive failed cycles | `risk.max_consecutive_failed_cycles` | count |
| 12 | Maximum fill mismatch | `risk.max_fill_mismatch_bps` | mismatch ratio per cycle |
| 13 | Maximum residual exposure | `risk.max_residual_exposure_usdt` | = §21 `MAX_RESIDUAL_EXPOSURE` |
| 14 | Maximum residual duration | `risk.max_residual_duration_ms` | = §21 `MAX_RESIDUAL_DURATION_MS` |
| 15 | Maximum hedge loss | `risk.max_hedge_loss_usdt` | = §21 `MAX_HEDGE_LOSS`; see also `MAX_HEDGE_SLIPPAGE_BPS` |
| 16 | Maximum slippage | `risk.max_slippage_bps` | bps vs decision price |
| 17 | Maximum order latency | `risk.max_order_latency_ms` | send→wire, monotonic clock (§12) |
| 18 | Maximum acknowledgement latency | `risk.max_ack_latency_ms` | send→exchange ack |
| 19 | Maximum fill latency | `risk.max_fill_latency_ms` | send→final fill |
| 20 | Maximum book age | `risk.staleness_ms` | **exists and is enforced today** (signal gate) |
| 21 | Maximum cross-book skew | `risk.max_cross_book_skew_ms` | receive-timestamp skew between the two legs' books |
| 22 | Maximum reconciliation mismatch | `risk.max_reconciliation_mismatch_usdt` | journal vs exchange state |
| 23 | Maximum API error rate | `risk.max_api_error_rate` | errors per window, per venue |

Enforcement rules:

- Breach of a limit denies the action (with the matching reason code); a
  sustained or severe breach triggers the corresponding kill switch (§7
  below).
- Every cycle must have a maximum approved worst-case loss before
  submission (§15, §40.10 evidence set) — derived from limits 1, 13, 15, 16.
- Latency limits (17–19) are measured with monotonic clocks and never from
  exchange timestamps alone (§12).

### Limit-change audit (§29)

The system must **never automatically increase a risk limit**. Every limit
change — including operator changes via config — requires an audit record
containing all of:

| Field | Content |
|---|---|
| authorization | who/what approved the change |
| before / after | both values, exactly |
| reason | why the change was made |
| identity | operator identity making the change |
| timestamp | when it took effect |

Records go to the append-only journal (§34, "administrative changes") and
must be tamper-evident (§38). Invariant §40.24: risk limits cannot change
without an audit record. A limit change observed at startup (config diff vs
last journaled values) must itself be journaled before trading resumes.

## 7. Kill switches (§30)

Each kill switch is a latched halt at a defined scope. Triggers below map
the §29 limits and §28 health inputs onto the §30 list; the effect column is
what *stops* — recovery capability never stops (see §7.2).

| Kill switch | Scope | Trigger (examples) | Effect on new cycles |
|---|---|---|---|
| Global | entire system | operator action; security event; risk-engine failure | all venues, all symbols, all strategies halt |
| Exchange | one venue | venue health failure, API error rate, maintenance | no cycle may include that venue as either leg |
| Account | one account | entitlement/auth failure, balance reconciliation failure | no cycle may use that account |
| Asset | one asset, all venues | asset-specific event (delisting risk, network halt) | no cycle in any symbol containing the asset |
| Symbol | one symbol | symbol suspension, price band, repeated symbol-local failures | no cycle in that symbol |
| Strategy | one strategy | strategy-local loss/mismatch pattern | strategy stops generating cycles |
| Stablecoin | quote-conversion graph | depeg / stablecoin health breach (§27) | no cycle relying on the affected conversion |
| Market-data | data-dependent scope | staleness, sequence gaps, integrity failure (`risk.staleness_ms` breach — the seam in `arb/risk/staleness.py`) | affected books cannot authorize anything |
| Private-stream | venue's private feeds | order/execution/balance stream loss | venue unusable for execution; state uncertain ⇒ reconcile |
| Latency | measured path | order/ack/fill latency limit breach (limits 17–19) | halt until latency recovers and restart is authorized |
| Daily-loss | system or strategy day | `max_daily_realized_loss` / `max_daily_drawdown` breach | no new cycles until authorized restart (next day is not automatic) |
| Residual-exposure | affected asset first, escalating | `max_residual_exposure` or `max_residual_duration` breach; hedge failure (§21) | freeze cycles in the asset; escalate to manual intervention |
| Reconciliation | affected account/venue | journal vs exchange mismatch beyond limit 22 | halt venue until reconciled |
| Repeated-rejection | venue or symbol | rejection-rate breach (limit 23 pattern) | stop submitting; back off per §33 |
| Security-event | global | credential anomaly, unauthorized config/limit change, audit-tamper signal | global halt; incident process (§38) |

### 7.1 Mandatory behaviors (§30)

Every kill switch, at every scope, must:

1. **Block new arbitrage cycles** (within its scope; wider scopes block more).
2. **Preserve emergency hedging capability.**
3. **Cancel appropriate open orders** (safe-to-cancel remainders in scope).
4. **Reconcile exchange state** (orders, fills, balances — §32: do not guess).
5. **Record the cause** (journaled, with the triggering snapshot/reason codes).
6. **Notify operators.**
7. **Require authorized restart** — a latched switch never self-resets;
   re-arming is an authorized, audited action (§29-style audit record).

### 7.2 Recovery survives every kill switch

**Do not disable recovery merely because normal trading has stopped**
(§30). Concretely:

- Emergency hedging, order cancellation, order-state reconciliation and
  balance reconciliation must remain executable under any combination of
  active kill switches (invariant §40.18; §42 "P1 missing
  failure-recovery test" class).
- The rate-limit engine must hold reserved token-bucket capacity for
  exactly these actions (§33), so a halt storm cannot exhaust the budget
  recovery needs.
- The engine distinguishes *cycle-originating* orders (blocked) from
  *recovery-originating* orders (hedges per §21, cancels, reconciliation
  queries), which pass a restricted risk check (price protection and hedge
  loss limits still apply) even while switches are latched.
- If recovery itself cannot proceed inside approved limits, the system
  preserves state and escalates to `MANUAL_INTERVENTION` (§19, §21) — it
  does not widen limits to force a hedge through.

## 8. Phase mapping and evidence

| Phase | Risk-engine scope |
|---|---|
| Phase 1 (today) | Staleness monitor + detector freshness gate only. No approvals, no limits enforced except `staleness_ms` as a signal gate. |
| Milestone 2 (§49, paper) | Full engine implemented and **enforced for simulated cycles**: decision object, all §29 limits against virtual balances, kill switches latching against the paper coordinator, journaled decisions and audit records. Same code path later used live (§36). |
| Phase 3 (limited live) | Same engine against real accounts, minimal capital, strict daily-loss and residual limits, kill switch deliberately test-fired before first live cycle (README phase plan). |

Required evidence before live qualification (§39, §40, §44): unit tests for
every limit and state transition; property tests that an invalid book cannot
authorize a trade, reserved funds cannot double-spend, and a timeout cannot
authorize resubmission; chaos tests that fire each kill switch and prove
new-cycle blocking plus surviving recovery capability; restart tests proving
latched switches persist across process restarts.
