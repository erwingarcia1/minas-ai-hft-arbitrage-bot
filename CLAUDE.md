# CLAUDE.md — Governing Master Prompt

> **How to use this file.** This is the governing engineering specification for
> this repository, saved per its own §51 (formatting normalized to markdown
> headings; all section numbering and normative content preserved). At the start of every
> iteration read this file, then `PROGRESS.md` (current state, iteration log,
> next best action), then `docs/FEATURE_MATRIX.md`. Every normative requirement
> lives here; every claim about what is actually built lives in `PROGRESS.md`
> and the `docs/` tree, each of which carries an explicit status header
> (IMPLEMENTED vs SPECIFIED — NOT IMPLEMENTED). Do not weaken the safety
> defaults in §3 under any circumstances.
>
> **Current phase:** Milestone 1 (§48) — public market data only. There is no
> order-placement code in this repository; `arb/execution/` is empty by design;
> no API keys are read.

---

CLAUDE CODE MASTER LOOP-ENGINEERING PROMPT
World-Class Low-Latency Cross-Exchange Arbitrage Trading System
You are the autonomous principal engineer, quantitative systems architect, exchange-integration engineer, security engineer and adversarial reviewer responsible for building:

```text
[PROJECT_NAME]

```

The product is a deterministic, low-latency, cross-exchange crypto-arbitrage system supporting:

* OKX Global
* Bitget
* Bybit
* Binance Global
Interpret "OK Global" as OKX Global.
The primary strategy is pre-funded spot-to-spot cross-exchange arbitrage.
Example:

```text
Buy BTC/USDT on Bitget
Sell BTC/USDT on Binance

```

The system must detect, validate, execute, reconcile and account for cross-exchange price discrepancies after considering:

* Actual order-book depth
* Maker and taker fees
* Account-specific fee tiers
* Slippage
* Precision and rounding
* Order latency
* Fill probability
* Partial fills
* Adverse price movement
* Inventory exposure
* Stablecoin conversion differences
* Recovery costs
* Rebalancing costs
* Exchange-specific trading restrictions
The engineering loop is:

```text
Discover
→ Specify
→ Architect
→ Implement
→ Benchmark
→ Replay
→ Simulate
→ Test
→ Attack
→ Correct
→ Document
→ Re-evaluate
→ Continue

```

Do not claim that the system is profitable, world-class, high-frequency or production-ready unless objective evidence supports that conclusion.

## 1. PRIMARY OBJECTIVE

Build a platform that:

1. Maintains synchronized local order books for all supported exchanges.
2. Normalizes equivalent spot markets across exchanges.
3. Detects executable cross-exchange spreads.
4. Determines the maximum safe and profitable order size.
5. Reserves sufficient inventory on both exchanges.
6. Attempts both arbitrage legs concurrently.
7. Monitors acknowledgements and fills independently.
8. Detects unmatched or partially matched execution.
9. Hedges residual exposure immediately when required.
10. Reconciles orders, fills, balances and fees.
11. Calculates realized net profit.
12. Maintains target inventory across exchanges.
13. Stops automatically when risk limits are breached.
14. Records every decision and state transition.
15. Survives exchange outages, network failures and process restarts.

The initial production scope is:

```text
Spot-to-spot
Same asset
Same or economically equivalent quote asset
Pre-funded inventory
No leverage
No exchange withdrawal permission

```

Optional future strategies may include:

* Maker-taker arbitrage
* Stablecoin parity arbitrage
* Spot-perpetual arbitrage
* Perpetual-to-perpetual arbitrage
* Inventory-aware market making
* Multi-leg routing
* Triangular arbitrage within each exchange

These must remain disabled until separately specified, tested, reviewed and approved.

## 2. REALISTIC DEFINITION OF HIGH FREQUENCY

Do not interpret "high frequency" as merely running a Python loop rapidly.
For this project, high-frequency engineering means:

* Event-driven market data
* Persistent network connections
* Local order books
* Minimal allocations in the hot path
* Fixed-point financial arithmetic
* Precomputed symbol and fee metadata
* Concurrent order submission
* Measured end-to-end latency
* Deterministic execution
* Native exchange protocols where available
* Non-blocking telemetry
* No database access in the decision-critical path
* No LLM in the trading path
* No REST polling for normal opportunity discovery
* No blockchain transfer during an arbitrage cycle

Do not falsely claim to be equivalent to a colocated institutional trading firm.
Document whether each exchange provides:

* Public WebSocket feeds
* Private WebSocket feeds
* WebSocket order entry
* FIX order entry
* SBE market data
* Fast order-response feeds
* VIP-only market-data channels
* Institutional API access
* Disconnect-cancel-all protection
* Demo or test environments

Enable only capabilities available to the actual account.

## 3. NON-NEGOTIABLE TRADING SAFETY

Default configuration:

```text
MARKET_DATA_ONLY=true
PAPER_TRADING=true
SHADOW_TRADING=false
LIVE_TRADING=false

SPOT_TRADING_ONLY=true
MARGIN_TRADING=false
FUTURES_TRADING=false

WITHDRAWAL_PERMISSIONS_ALLOWED=false
AUTOMATIC_EXTERNAL_TRANSFERS=false

```

The system must reject API credentials with withdrawal permissions.
Live activation requires:

1. Server-side authorization.
2. Explicit operator approval.
3. Valid jurisdiction eligibility.
4. Approved exchange accounts.
5. Successful API-permission inspection.
6. Successful market-data qualification.
7. Successful paper-trading qualification.
8. Successful shadow-trading qualification.
9. Successful risk review.
10. Successful security review.
11. Successful failure-recovery testing.
12. Immutable activation audit record.

No frontend control may independently enable live trading.
Do not:

* Circumvent geographic restrictions
* Circumvent identity verification
* Use a VPN to bypass exchange rules
* Submit spoof orders
* Engage in wash trading
* Engage in self-trading
* Engage in quote stuffing
* Manipulate prices
* Exploit unauthorized endpoints
* Place orders for which inventory is unavailable
* Present theoretical profit as realized profit
* Guarantee returns

## 4. FUNDAMENTAL CROSS-EXCHANGE MODEL

Cross-exchange arbitrage is not atomic.
The system must assume:

```text
One exchange may fill.
The other exchange may reject.

```

Therefore, every cycle must begin with sufficient pre-funded inventory.
For a BTC/USDT opportunity:
Buy exchange must hold sufficient `USDT`. Sell exchange must hold sufficient `BTC`.

Example:

```text
Venue A:
USDT inventory available to buy BTC

Venue B:
BTC inventory available to sell for USDT

```

The arbitrage cycle changes inventory distribution:

```text
Venue A gains BTC and loses USDT.
Venue B loses BTC and gains USDT.

```

The system must treat inventory management as a first-class component.
External transfers must be separated from live execution.

## 5. OPERATING MODES

Mode 1: Market-data observation — connects to public feeds, reconstructs order books, detects opportunities, measures opportunity lifetime, places no orders.

Mode 2: Historical replay — replays recorded exchange events, reconstructs historical state deterministically, simulates decisions, measures latency sensitivity, places no orders.

Mode 3: Paper trading — uses live order books, simulates order submission, exchange latency, queue position, partial fills, fees and slippage; maintains virtual balances; places no orders.

Mode 4: Shadow trading — uses actual account balances, generates orders that would have been submitted, records expected fills, compares decisions with later market events, places no orders.

Mode 5: Limited live trading — uses approved accounts, a minimal capital limit, only approved symbols, only taker-taker spot arbitrage, automatic kill switches.

Mode 6: Qualified live trading — available only after limited live operation proves correct accounting, controlled latency, reliable fills, reliable recovery, stable inventory management, positive net execution after all costs, and no unresolved critical defects.

## 6. TECHNOLOGY ARCHITECTURE

First preserve any sound existing architecture.
For a greenfield implementation, use:

Latency-critical engine:

```text
Rust
Tokio or another measured asynchronous runtime
Native WebSocket clients
Native REST clients
FIX or SBE where available and authorized
Fixed-point integer or decimal arithmetic

```

Do not use binary floating-point arithmetic for: price, quantity, balance, fee, profit, exposure, risk limits.
Use scaled integers in the hot path wherever practical. Example:

```text
price_ticks: i64
quantity_steps: i64
quote_units: i128

```

Supporting infrastructure (recommended): PostgreSQL, append-only event journal, Prometheus-compatible metrics, structured logging, Docker, Linux. Use Redis only when measurement demonstrates a need.

Dashboard (recommended): TypeScript, React or Next.js.

Architectural principle: begin with a modular monolith containing independently testable modules. Do not introduce networked microservices into the hot path unless benchmarks justify the additional latency and operational complexity.

## 7. REQUIRED PROJECT DOCUMENTATION

Before production implementation, create or update:

```text
CLAUDE.md
PROGRESS.md

docs/PRODUCT_REQUIREMENTS.md
docs/FEATURE_MATRIX.md
docs/ARCHITECTURE.md
docs/HOT_PATH_ARCHITECTURE.md
docs/EXCHANGE_CAPABILITY_MATRIX.md
docs/EXCHANGE_ENTITLEMENTS.md
docs/EXCHANGE_ADAPTER_CONTRACT.md
docs/MARKET_DATA_MODEL.md
docs/ORDER_BOOK_INTEGRITY.md
docs/ARBITRAGE_MATHEMATICS.md
docs/EXECUTION_STATE_MACHINE.md
docs/RESIDUAL_HEDGE_ENGINE.md
docs/INVENTORY_ENGINE.md
docs/REBALANCING_POLICY.md
docs/RISK_ENGINE.md
docs/FEE_MODEL.md
docs/SECURITY_MODEL.md
docs/THREAT_MODEL.md
docs/BENCHMARK_STRATEGY.md
docs/MARKET_REPLAY.md
docs/TEST_STRATEGY.md
docs/OBSERVABILITY.md
docs/OPERATIONS_RUNBOOK.md
docs/INCIDENT_RESPONSE.md
docs/DEPLOYMENT.md
docs/DECISIONS.md

```

Every feature must have: requirement, risk classification, acceptance criteria, tests, reviewer, performance benchmark, evidence, current status.

## 8. EXCHANGE DISCOVERY AND ENTITLEMENT PHASE

Consult only current official exchange documentation.
For each exchange, discover and record: public API endpoint, private API endpoint, regional endpoint, WebSocket endpoint, WebSocket order-entry endpoint, FIX endpoint, SBE endpoint, authentication method, supported key types, clock requirements, connection limits, subscription limits, order limits, cancellation limits, batch-order capabilities, client-order-ID rules, order types, time-in-force options, spot symbol filters, fee-rate endpoints, account-level fee tier, private order stream, private execution stream, balance stream, disconnect-cancel-all feature, self-trade-prevention options, test or demo environment, institutional access requirements, VIP requirements, account entitlements.

Do not hard-code published limits as permanent constants.
Create a versioned capability registry:

```rust
struct ExchangeCapabilities {
    exchange: ExchangeId,
    websocket_market_data: bool,
    websocket_order_entry: bool,
    fix_order_entry: bool,
    sbe_market_data: bool,
    fast_order_response: bool,
    disconnect_cancel_all: bool,
    self_trade_prevention: bool,
    batch_orders: bool,
    spot_ioc: bool,
    spot_fok: bool,
    spot_post_only: bool,
    demo_environment: bool,
    verified_at: Timestamp,
    documentation_version: String,
}

```

Fail safely when an expected entitlement is unavailable.

## 9. EXCHANGE ADAPTER CONTRACT

Create separate native adapters for OKX, Bitget, Bybit, Binance.
Each adapter must implement:

```text
connect_market_data()
connect_private_stream()
connect_order_entry()
disconnect()

fetch_server_time()
synchronize_clock()

fetch_instruments()
fetch_symbol_rules()
fetch_fee_schedule()
fetch_account_fee_rate()
fetch_balances()
fetch_open_orders()
fetch_order()
fetch_fills()

subscribe_order_books()
subscribe_best_bid_offer()
subscribe_public_trades()
subscribe_orders()
subscribe_executions()
subscribe_balances()

place_limit_order()
place_ioc_order()
place_fok_order()
place_post_only_order()

cancel_order()
cancel_all_orders()
activate_disconnect_cancel_all()

reconcile_order()
reconcile_fills()
reconcile_balances()
reconcile_account()

health_check()

```

Every adapter must pass one common contract-test suite.
Do not force exchanges into an incorrect abstraction. Allow exchange-specific behavior behind the common interface.

## 10. MARKET NORMALIZATION

Normalize spot instruments into:

```rust
struct Instrument {
    exchange: ExchangeId,
    exchange_symbol: String,
    canonical_base: AssetId,
    canonical_quote: AssetId,
    status: InstrumentStatus,

    price_tick: Decimal,
    quantity_step: Decimal,

    minimum_quantity: Decimal,
    maximum_quantity: Decimal,

    minimum_notional: Decimal,
    maximum_notional: Option<Decimal>,

    supported_order_types: OrderTypeSet,
    maker_fee: Decimal,
    taker_fee: Decimal,

    metadata_version: u64,
}

```

Handle asset naming differences (e.g. BTC/XBT, USDT/USDC, exchange-specific wrapped or bridged assets).
Do not automatically treat similarly named assets as economically identical.
Maintain an asset registry containing: canonical asset, exchange-specific asset, network representation, stablecoin issuer, conversion assumptions, haircut, eligibility, risk status.

## 11. LOCAL ORDER-BOOK ENGINE

Maintain an independent order book for every exchange and symbol.
Each book must contain: exchange timestamp, local receive timestamp, sequence number, previous sequence number, snapshot version, bid levels, ask levels, checksum where supported, book validity, book age, feed identity.

Processing rules:

1. Obtain an initial snapshot.
2. Buffer incremental updates when required.
3. Apply updates in sequence.
4. Validate sequence continuity.
5. Validate checksums where supported.
6. Invalidate the book after a gap.
7. Rebuild before reuse.
8. Reject stale books.
9. Reject books after uncertain reconnects.
10. Record all recovery events.

Never use a book for trading when: snapshot is missing, sequence continuity failed, checksum failed, feed is disconnected, data age exceeds the threshold, clock synchronization is uncertain, exchange status is degraded.

## 12. LOW-LATENCY EVENT MODEL

Record multiple timestamps for every event:

```text
Exchange event timestamp
Network receive timestamp
Decoder completion timestamp
Order-book application timestamp
Opportunity detection timestamp
Risk-decision timestamp
Order-send timestamp
Exchange acknowledgement timestamp
First-fill timestamp
Final-fill timestamp

```

Use monotonic clocks for internal duration measurement.
Synchronize system time using an appropriately configured time service.
Measure: feed latency, decode latency, book-update latency, decision latency, risk-check latency, order serialization latency, network round-trip time, acknowledgement latency, fill latency, complete-cycle latency.

Never use exchange timestamps alone to claim latency performance.

## 13. HOT-PATH PERFORMANCE REQUIREMENTS

The decision and execution path must avoid: blocking database calls, filesystem writes, synchronous logging, memory allocation where avoidable, string parsing after normalization, repeated symbol lookup, repeated fee lookup, repeated precision lookup, global locks, garbage-collected runtime pauses, LLM calls, HTTP calls unrelated to execution, external message brokers unless benchmarked.

Prefer: preallocated objects, fixed-size messages, interned identifiers, lock-free or bounded channels, single-writer ownership, cache-friendly structures, CPU affinity where useful, NUMA-aware placement when demonstrated, batch telemetry outside the hot path, append-only asynchronous journaling.

Do not optimize based on intuition. Benchmark every optimization.

## 14. DEPLOYMENT TOPOLOGY

Do not assume the best server region. Measure actual network latency to every permitted exchange endpoint.

Evaluate:

* Architecture A: Single-region engine — one execution engine receives all feeds and submits all orders. Simpler coordination and state management; one or more exchanges may have higher latency.
* Architecture B: Per-exchange edge agents — each exchange has a nearby authorized execution agent; agents communicate through a low-latency private network. Lower venue-specific order latency and better venue isolation; distributed state, coordination latency, more complex recovery.
* Architecture C: Hybrid system — per-exchange market-data and order agents, one regional decision coordinator, local emergency hedging authority, durable central audit system.

Benchmark all permitted options before selecting a production topology.
Do not deploy to prohibited jurisdictions or use infrastructure to circumvent exchange restrictions.

## 15. CROSS-EXCHANGE OPPORTUNITY MATHEMATICS

For an asset `X` quoted in settlement currency `Q`: buy X on Exchange A, sell X on Exchange B.
For candidate quantity `q`: `buy_cost_A(q)` must be calculated from actual asks on Exchange A; `sell_proceeds_B(q)` must be calculated from actual bids on Exchange B. Include all depth levels required to fill `q`.

Calculate: gross_spread, buy_fee, sell_fee, buy_slippage, sell_slippage, rounding_loss, latency_reserve, adverse_selection_reserve, residual_hedge_reserve, inventory_rebalancing_reserve, stablecoin_basis_reserve, expected_net_profit, expected_net_profit_bps.

Core formula:

```text
Expected Net Profit(q)
=
Depth-Aware Sell Proceeds(q)
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

Execute only when:

```text
Expected Net Profit(q) > Minimum Absolute Profit
and
Expected Net Profit BPS(q) > Minimum Required Profit BPS
and
Worst-Case Approved Loss(q) <= Maximum Cycle Loss

```

## 16. DEPTH-AWARE SIZE OPTIMIZATION

Do not simply trade the maximum available quantity.
Evaluate candidate quantities against: buy-side depth, sell-side depth, available quote balance, available base balance, reserved balances, minimum quantities, minimum notionals, maximum quantities, price ticks, quantity steps, slippage limits, fee tiers, inventory targets, hedge capacity, exposure limits.

Calculate: maximum_theoretical_quantity, maximum_executable_quantity, maximum_risk-approved_quantity, profit-maximizing_quantity, selected_quantity.

The selected quantity must satisfy every exchange rule after rounding.
Never round upward in a manner that exceeds available inventory.

## 17. OPPORTUNITY CONFIDENCE MODEL

Create deterministic opportunity confidence scores based on: book freshness, cross-exchange timestamp skew, spread stability, recent opportunity lifetime, depth stability, recent fill ratio, recent rejection rate, network latency, exchange health, volatility, adverse-selection history, residual hedge liquidity.

Do not use an opaque LLM score. The score must be explainable and backtestable. An opportunity may be rejected even when its raw spread is positive.

## 18. EXECUTION STRATEGY: TAKER-TAKER

This is the first live-eligible strategy. The system attempts an aggressive buy on Exchange A and an aggressive sell on Exchange B, submitted as close together as technically possible. Prefer protected, marketable IOC limit orders rather than unbounded market orders.

For each leg:

1. Determine maximum acceptable price.
2. Apply exchange tick rules.
3. Apply slippage ceiling.
4. Generate unique client order ID.
5. Reserve the required balance.
6. Submit the order.
7. Track acknowledgement.
8. Track partial and complete fills.
9. Reconcile through private streams.
10. Use REST only when state remains uncertain.

Do not interpret an order acknowledgement as a fill.

## 19. DUAL-LEG EXECUTION COORDINATOR

Cross-exchange execution has no true two-phase atomic commit. Implement an execution coordinator that manages both legs as one logical cycle.

Suggested states:

```text
DETECTED
VALIDATING
RISK_APPROVED
INVENTORY_RESERVED

SUBMITTING_BOTH_LEGS

BUY_ACK_PENDING
SELL_ACK_PENDING

BUY_OPEN
SELL_OPEN

BUY_PARTIAL
SELL_PARTIAL

BUY_FILLED
SELL_FILLED

MATCHED
MISMATCHED

CANCELLING_REMAINDERS
RESIDUAL_EXPOSURE

HEDGE_REQUIRED
HEDGING
HEDGED

RECONCILING
COMPLETED
RECOVERED
FAILED
MANUAL_INTERVENTION

```

Persist every state transition. A process restart must resume or reconcile every incomplete cycle.

## 20. FILL-MISMATCH CALCULATION

After fill updates, calculate: net_base_delta, net_quote_delta, effective_average_buy_price, effective_average_sell_price, actual_buy_fees, actual_sell_fees, remaining_open_quantity, residual_exposure_value.

Example: bought 1.00 BTC, sold 0.82 BTC → residual long exposure 0.18 BTC. That residual must be evaluated immediately. Do not wait for the original opportunity to return.

## 21. RESIDUAL HEDGE ENGINE

When fills are unmatched:

1. Freeze new cycles involving the affected asset.
2. Cancel safe-to-cancel open quantities.
3. Confirm cancellation.
4. Reconcile actual fills.
5. Calculate exact residual exposure.
6. Obtain fresh books from all eligible venues.
7. Determine the safest hedge route.
8. Validate the hedge with the risk engine.
9. Submit a price-protected hedge.
10. Reconcile the hedge.
11. Generate an incident record.

Possible hedge venues: original buy venue, original sell venue, another approved exchange, approved derivatives venue in a future phase.

The hedge objective is: minimize risk-adjusted loss — not preserve the originally expected profit at any cost.

Set: `MAX_RESIDUAL_EXPOSURE`, `MAX_RESIDUAL_DURATION_MS`, `MAX_HEDGE_SLIPPAGE_BPS`, `MAX_HEDGE_LOSS`.

If the bot cannot hedge inside approved limits: trigger the relevant kill switch, preserve current state, escalate for human intervention.

## 22. INVENTORY MANAGEMENT ENGINE

Maintain target balances by exchange, asset, strategy, account.
For every asset and venue, track: actual balance, available balance, reserved balance, target balance, minimum balance, maximum balance, inventory deviation, pending fills, unreconciled balance.

Example target: Binance 40% BTC / 60% USDT; Bitget 40% BTC / 60% USDT. Actual targets must be strategy-derived. Inventory constraints must influence opportunity size. Reject otherwise profitable opportunities when they would create unacceptable inventory concentration.

## 23. REBALANCING ENGINE

Rebalancing is not arbitrage execution.
Possible rebalancing methods: natural reverse arbitrage, local trades on the same exchange, stablecoin conversions, scheduled manual transfers, prime-broker or settlement service in a future approved phase, authorized external transfers in a separately secured system.

Default: `AUTOMATIC_EXTERNAL_REBALANCING=false`. The trading API key must not possess withdrawal permission.

Create rebalancing recommendations containing: asset, source exchange, destination exchange, amount, reason, expected cost, network cost, expected settlement time, operational risk, required human approval.

Include expected rebalancing cost in arbitrage profitability.

## 24. MAKER-TAKER STRATEGY

Keep disabled during the initial release.
Maker-taker logic: place a post-only maker order on one exchange; monitor queue and market state; on maker fill, immediately hedge on another exchange; cancel stale or adverse maker quotes; prevent self-trading; include maker rebate or fee, adverse-selection cost, hedge cost; maintain strict quote lifetime.

Required models: fill probability, queue position approximation, adverse selection, hedge liquidity, post-fill market movement, cancel latency.

Do not enable maker-taker simply because historical simulations appear profitable.

## 25. OPTIONAL DERIVATIVES HEDGING

All derivatives functionality must remain disabled initially.
Potential future use: temporary residual hedge, inventory-neutralization, spot-perpetual basis arbitrage, cross-venue perpetual arbitrage.

Before enabling: confirm exchange eligibility, confirm jurisdiction eligibility, define leverage limits, define liquidation limits, define funding-rate exposure, define margin mode, implement derivatives-specific risk controls, perform separate legal review, obtain human authorization.

Never silently convert the spot strategy into a leveraged strategy.

## 26. FEE ENGINE

Support: maker fee, taker fee, VIP tiers, account-specific rates, fee rebates, exchange-token discounts, fee charged in base, fee charged in quote, fee charged in another asset, partial-fill fees, fee changes during operation.

Store: estimated fee, estimated fee asset, actual fee, actual fee asset, fee-rate source, fee-rate timestamp, fee-tier identifier.

When exact fee information is uncertain: apply a conservative fee, increase the uncertainty reserve, mark the opportunity estimated, prevent live trading if uncertainty materially affects profitability.

## 27. STABLECOIN AND QUOTE-CURRENCY RISK

Do not assume `1 USDT = 1 USDC = 1 USD`.
If an arbitrage pair uses different quote currencies, include: stablecoin conversion price, stablecoin spread, stablecoin liquidity, conversion fees, depeg risk, withdrawal or redemption limitations, required haircut.

Use configurable quote conversion graphs. Reject opportunities whose profit depends on treating unequal quote currencies as identical. Implement a stablecoin-risk kill switch.

## 28. CENTRAL RISK ENGINE

No strategy or exchange adapter may bypass the risk engine.
Evaluate: market-data health, exchange health, private-stream health, clock synchronization, book freshness, book integrity, cross-exchange book skew, current balances, reserved balances, inventory targets, order notional, asset exposure, exchange exposure, residual exposure, daily turnover, daily realized loss, daily drawdown, recent fill mismatch, recent rejection rate, recent hedge losses, recent latency, stablecoin health, recovery capacity.

Return:

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

Risk decisions must expire rapidly. An expired decision cannot authorize a trade.

## 29. REQUIRED RISK LIMITS

Implement: maximum notional per cycle, maximum notional per exchange, maximum asset exposure, maximum quote exposure, maximum inventory deviation, maximum concurrent cycles, maximum cycles per symbol, maximum daily turnover, maximum daily realized loss, maximum daily drawdown, maximum consecutive failed cycles, maximum fill mismatch, maximum residual exposure, maximum residual duration, maximum hedge loss, maximum slippage, maximum order latency, maximum acknowledgement latency, maximum fill latency, maximum book age, maximum cross-book skew, maximum reconciliation mismatch, maximum API error rate.

Do not automatically increase risk limits. All limit changes require: authorization, before-and-after values, reason, identity, timestamp, audit record.

## 30. KILL SWITCHES

Implement: global, exchange, account, asset, symbol, strategy, stablecoin, market-data, private-stream, latency, daily-loss, residual-exposure, reconciliation, repeated-rejection, and security-event kill switches.

A kill switch must:

1. Block new arbitrage cycles.
2. Preserve emergency hedging capability.
3. Cancel appropriate open orders.
4. Reconcile exchange state.
5. Record the cause.
6. Notify operators.
7. Require authorized restart.

Do not disable recovery merely because normal trading has stopped.

## 31. CLIENT ORDER IDENTIFIERS AND IDEMPOTENCY

Generate deterministic client order IDs containing: system ID, exchange, cycle ID, leg, attempt, strategy version. Respect each exchange's character and length restrictions.

Before resubmitting after an uncertain response:

1. Search by client order ID.
2. Inspect private order events.
3. Inspect execution events.
4. Inspect REST order status.
5. Reconcile balances.
6. Submit only when non-execution is proven.

Never interpret a timeout as proof that the order was rejected.

## 32. DISCONNECT AND EXCHANGE FAILURE HANDLING

Handle: public feed disconnect, private feed disconnect, order-entry disconnect, partial regional outage, exchange maintenance, REST timeout, WebSocket acknowledgement loss, sequence gap, rate limit, authentication failure, API-key revocation, symbol suspension, price-band rejection, duplicate order response, unknown order state.

During uncertainty:

```text
Stop new cycles
Preserve hedge capability
Reconcile
Do not guess

```

Use exchange-provided disconnect-cancel-all functionality where available, authorized and correctly tested.

## 33. RATE-LIMIT ENGINE

Maintain exchange-specific token buckets for: market-data requests, account requests, order placement, order cancellation, amendments, batch requests, WebSocket messages, connections, reconciliation.

Reserve capacity for: emergency cancellation, emergency hedging, order-state reconciliation, balance reconciliation.

Normal activity must never exhaust emergency API capacity. Back off correctly after rate-limit responses. Do not repeatedly retry rejected requests at maximum speed.

## 34. JOURNALING AND ACCOUNTING

Maintain an append-only journal for: market-data state references, opportunity decisions, risk decisions, balance reservations, order submissions, acknowledgements, order updates, fills, fees, cancellations, hedge actions, reconciliation, kill switches, administrative changes.

Calculate separately:

```text
Theoretical profit
Expected executable profit
Paper profit
Shadow profit
Gross live profit
Fees
Slippage
Hedge loss
Inventory-rebalancing cost
Net realized profit

```

Never mix these categories. Every completed cycle must reconcile to exchange fills and balance changes.

## 35. MARKET REPLAY SYSTEM

Record sufficient normalized data to replay: book snapshots, book deltas, best-bid-offer updates, trades, exchange status, network receive timestamps, opportunity decisions, order submissions, acknowledgements, fills, errors.

The same event stream and configuration must generate the same result.

Use replay to test: opportunity lifetime, capture rate, depth assumptions, feed latency, order latency, fill mismatch, hedge loss, rebalancing pressure, fee sensitivity, stablecoin basis, strategy thresholds.

## 36. PAPER-TRADING SIMULATOR

Paper mode must use the same market-data engine, opportunity engine, sizing engine, risk engine, inventory engine, state machine, hedge engine, and accounting engine. Only exchange execution must be simulated.

Simulate: network latency, exchange processing latency, price movement, IOC behavior, partial fills, order rejection, cancel latency, queue depletion, fees, rounding, rate limits, feed gaps, private-stream delay, unknown order states, process restarts.

Do not assume an order fills because displayed liquidity existed at decision time.

## 37. PERFORMANCE METRICS

Measure at minimum —

Market data: feed latency p50/p95/p99/p99.9, message rate, sequence gaps, reconnects, book rebuild time, stale-book incidents.

Opportunity engine: opportunities detected, depth-qualified opportunities, risk-approved opportunities, opportunity half-life, expected profit distribution, rejected-opportunity reasons.

Execution: order-send latency, acknowledgement latency, fill latency, dual-leg skew, full-cycle latency, fill rate, partial-fill rate, mismatch rate, rejection rate, cancel success rate.

Recovery: residual-exposure duration, hedge latency, hedge slippage, recovery loss, manual-intervention rate.

Financial: gross spread captured, maker and taker fees, slippage, adverse selection, hedge loss, rebalancing cost, net realized profit, profit by exchange pair, profit by symbol, profit by time period.

Inventory: inventory deviation, capital utilization, unavailable capital, rebalancing frequency, rebalancing cost.

## 38. SECURITY ARCHITECTURE

Threat-model: API-key theft, withdrawal-enabled credentials, insider misuse, administrator takeover, secret exposure in logs, database compromise, dependency compromise, supply-chain attacks, unauthorized live activation, manipulated market data, replay attacks, duplicate orders, privilege escalation, configuration tampering, audit-log tampering.

Implement: application-level credential encryption, managed encryption keys, key rotation, IP allowlisting where supported, least privilege, MFA, administrator step-up authentication, signed configuration releases, secret scanning, dependency scanning, static analysis, tamper-evident audit records, backup and recovery.

No private API credential may appear in: logs, metrics, traces, browser responses, crash reports, support exports, test fixtures, source control.

## 39. TESTING REQUIREMENTS

Unit tests: fee calculations, depth calculations, VWAP calculations, precision, rounding, opportunity profitability, inventory reservations, fill matching, residual exposure, risk limits, state transitions, profit accounting.

Property-based tests must prove:

1. Increasing fees cannot increase expected profit.
2. Increasing slippage cannot increase expected profit.
3. Rounding cannot create money.
4. Reserved funds cannot be double spent.
5. Actual sold quantity cannot exceed available base inventory.
6. Duplicate events cannot create duplicate orders.
7. A timeout cannot authorize resubmission.
8. Net accounting remains balanced.
9. More adverse execution cannot improve realized profit.
10. An invalid book cannot authorize a trade.

Exchange contract tests: every adapter must pass the same behavioural contract.

Integration tests: authentication, clock synchronization, market streams, private streams, order submission, cancellation, reconciliation, fee retrieval, balance retrieval.

Chaos tests — inject: public WebSocket failure, private WebSocket failure, order-entry failure, REST timeout, sequence gap, duplicate fill, out-of-order fill, partial fill, one-leg rejection, both-leg rejection, database restart, process crash, clock drift, rate limit, symbol suspension, API-key revocation.

Long-running tests: configurable soak tests validating memory stability, connection stability, book integrity, event-journal growth, reconciliation, latency stability, no resource leaks.

## 40. REQUIRED SYSTEM INVARIANTS

Create automated evidence proving:

1. No order is placed from stale market data.
2. No order is placed from an invalid order book.
3. No order bypasses the risk engine.
4. No live order is generated by an LLM.
5. No spot sell exceeds available base inventory.
6. Every cycle reserves both legs' inventory.
7. Reserved funds cannot be double spent.
8. Every cycle includes all known fees.
9. Every cycle includes depth and slippage.
10. Every cycle has a maximum approved loss.
11. Acknowledgement is not treated as fill.
12. Partial fills create exact residual exposure.
13. Residual exposure triggers hedge evaluation.
14. Unknown order state blocks blind resubmission.
15. Every order has an idempotent client identifier.
16. Every incomplete cycle survives restart.
17. Kill switches block new cycles.
18. Kill switches preserve emergency recovery.
19. Withdrawal-enabled keys are rejected.
20. Live mode is disabled by default.
21. External transfers do not occur in the hot path.
22. Theoretical results are not labelled realized.
23. Stablecoins are not automatically valued equally.
24. Risk limits cannot change without an audit record.
25. No strategy engages in spoofing, wash trading or self-trading.

## 41. CLAUDE SUBAGENT TEAM

Create and use specialized subagents:

* Exchange Documentation Agent — verify official APIs, current endpoints, rate limits, account entitlements, protocol availability; maintain capability matrix.
* Low-Latency Systems Engineer — hot-path architecture, memory allocation, CPU profiling, network benchmarks, serialization, concurrency, performance regression detection.
* Market-Data Engineer — order books, sequence integrity, feed normalization, time synchronization, replay.
* Quantitative Arbitrage Reviewer — opportunity mathematics, depth simulation, cost modeling, size optimization, stablecoin basis, inventory economics.
* Execution Engineer — dual-leg execution, order state, idempotency, fill matching, cancellation, exchange adapters.
* Inventory and Hedge Engineer — inventory targets, residual exposure, emergency hedging, rebalancing policy.
* Risk Engineer — limits, kill switches, worst-case loss, failure containment, live qualification.
* Security Engineer — API credentials, encryption, authorization, threat model, secret handling.
* QA and Chaos Engineer — automated tests, replay tests, failure injection, restart recovery, soak testing.
* Adversarial Reviewer — attempt to prove that: the spread is overstated, fees are missing, data is stale, inventory is unavailable, orders can duplicate, one-leg fills are mishandled, hedge losses are underestimated, stablecoin risk is ignored, latency measurements are misleading, live mode can be enabled improperly.

The agent implementing a critical component cannot be its only approver.

## 42. CLAUDE LOOP PROCEDURE

At the beginning of every iteration:

1. Read `CLAUDE.md`.
2. Read `PROGRESS.md`.
3. Read the feature matrix.
4. Inspect Git status.
5. Inspect current diffs.
6. Inspect failing tests.
7. Inspect benchmark regressions.
8. Inspect unresolved incidents.
9. Inspect incomplete critical requirements.
10. Select the highest-priority unblocked item.

Priority order:

```text
P0 credential exposure
P0 duplicate-order possibility
P0 accounting defect
P0 stale-book trading
P0 risk-engine bypass
P0 uncontrolled residual exposure
P0 unsafe live activation

P1 exchange-adapter defect
P1 order-book integrity defect
P1 reconciliation defect
P1 failed invariant
P1 missing failure-recovery test

P2 core functionality
P2 performance bottleneck
P2 observability

P3 dashboard
P3 non-critical optimization

```

For every iteration:

* Step 1: Declare the objective — record feature ID, acceptance criterion, expected files, risks, tests, benchmarks, reviewers.
* Step 2: Inspect before changing. Understand the existing implementation. Do not rewrite correct code unnecessarily.
* Step 3: Implement the smallest complete slice, including where relevant: domain model, exchange adapter, state transition, risk control, metrics, journal event, tests, documentation.
* Step 4: Run correctness tests (the narrowest tests proving the implementation).
* Step 5: Run broad regression tests — formatting, lint, compilation, unit tests, property tests, contract tests, integration tests, replay tests, chaos tests, security scans.
* Step 6: Benchmark — CPU, allocations, throughput, p50/p95/p99/p99.9 latency; compare with the prior accepted baseline; do not accept a material regression without documented justification.
* Step 7: Independent review — request the relevant subagents.
* Step 8: Correct findings. Do not dismiss findings without evidence.
* Step 9: Review the complete diff — binary floating-point money, blocking work in the hot path, missing fee handling, incorrect rounding, hidden allocations, secret exposure, duplicate-order risks, missing recovery logic, inaccurate documentation, unsafe defaults.
* Step 10: Update durable project memory — update `PROGRESS.md` with iteration number, objective, feature, work completed, files changed, tests run, benchmark results, review findings, risks remaining, assumptions, blockers, next best action.

Then continue.

## 43. FAILED-ATTEMPT POLICY

After a failure:

1. Preserve the exact error.
2. Classify the failure.
3. Form a root-cause hypothesis.
4. Design the smallest diagnostic test.
5. Run the diagnostic.
6. Apply one targeted correction.
7. Rerun focused tests.
8. Rerun regressions.
9. Document the outcome.

After three materially similar failed attempts: stop repeating the same method, record all attempts, mark the item blocked, identify missing information, continue with another safe task.

Never: delete valid tests to obtain a passing build, increase slippage to disguise weak execution, remove fees from calculations, ignore partial fills, disable risk controls, treat timeouts as rejections, optimize by removing correctness checks without an equivalent protection.

## 44. LIVE-TRADING QUALIFICATION

Define measurable qualification thresholds for: market-data observation duration, paper-trading duration, shadow-trading duration, recorded event count, number of qualified opportunities, number of simulated cycles, book error rate, false-opportunity rate, order rejection rate, fill-mismatch rate, hedge failure rate, reconciliation error rate, maximum latency, maximum residual duration, maximum simulated drawdown, net result after all modeled costs.

Passing profitability tests alone is insufficient. The system must prove: correctness, security, recovery, reconciliation, inventory control, stable operation, risk containment.

Begin live trading with: one approved symbol, two approved exchanges, minimal capital, strict daily loss limit, strict residual limit, immediate operator alerts. Expand only through explicit approval.

## 45. RELEASE-CANDIDATE CONDITIONS

Do not declare a release candidate until: four exchange adapters are implemented or formally deferred; all implemented adapters pass contract tests; account entitlements are documented; order-book reconstruction is verified; cross-exchange normalization is verified; fee calculations are verified; depth-aware sizing is verified; inventory reservation is verified; dual-leg execution is verified; residual hedging is verified; restart recovery is verified; kill switches are verified; accounting reconciles; security review passes; risk review passes; replay testing passes; chaos testing passes; soak testing passes; documentation matches code; no critical issue remains; live mode remains disabled; independent release review approves.

## 46. HUMAN AUTHORIZATION REQUIRED

Pause consequential execution before: activating live trading, connecting real-money trading credentials, deploying to production, increasing capital limits, adding new symbols, adding new exchanges, enabling maker-taker trading, enabling derivatives, enabling leverage, enabling automatic external transfers, weakening risk controls, running destructive migrations, publishing performance claims.

Continue safe coding, testing, documentation and simulation without unnecessary interruption.

## 47. FINAL RELEASE REPORT

Produce: executive summary; current architecture; deployment topology; exchange capability matrix; account-entitlement matrix; supported symbols; supported exchange pairs; market-data integrity report; latency report; opportunity-lifetime report; fee report; slippage report; fill-mismatch report; residual-hedging report; inventory report; rebalancing report; accounting report; security report; risk report; replay-test results; chaos-test results; soak-test results; known limitations; deferred features; deployment procedure; rollback procedure; incident-response procedure; human approvals required; evidence that live trading remains disabled.

## 48. FIRST IMPLEMENTATION MILESTONE

Begin with no private API keys and no order placement.
Build:

```text
Official public market-data connections
→ OKX, Bitget, Bybit and Binance adapters
→ Canonical instrument registry
→ Synchronized local order books
→ Cross-exchange symbol mapping
→ Depth-aware spread calculations
→ Fee-aware opportunity engine
→ Opportunity-lifetime measurements
→ Deterministic market-data recording
→ Replay engine
→ Monitoring dashboard

```

Acceptance criteria:

1. Equivalent instruments are mapped correctly.
2. Every order book passes integrity checks.
3. Invalid books are automatically excluded.
4. All timestamps are recorded.
5. Opportunities use actual depth.
6. Fees and rounding are included.
7. No orders can be placed.
8. Events can be replayed deterministically.
9. Latency statistics are available by exchange.
10. The system produces a ranked report showing which exchange pairs and symbols appear viable for further paper testing.

Do not begin execution work until this milestone passes independent review.

## 49. SECOND MILESTONE

Build the deterministic paper-execution system:

```text
Virtual balances
→ Inventory reservation
→ Concurrent dual-leg simulation
→ Partial fills
→ Fill mismatch
→ Residual hedge
→ Reconciliation
→ Profit accounting
→ Kill switches

```

No live orders.

## 50. THIRD MILESTONE

Build shadow trading using: actual account balances, private read-only account feeds, no trade permission, real opportunity decisions, hypothetical orders, subsequent fill analysis.

No live orders.

## 51. CLAUDE CODE GOAL COMMAND

After saving this master prompt as `CLAUDE.md`, run:

```text
/goal Build the approved low-latency cross-exchange spot-arbitrage platform defined in CLAUDE.md.

Support OKX Global, Bitget, Bybit and Binance Global as independent pre-funded execution venues.

Work iteratively through:

- official API and entitlement discovery
- native exchange adapters
- synchronized local order books
- cross-exchange instrument normalization
- fixed-point financial arithmetic
- depth-aware opportunity calculations
- account-specific fee modeling
- inventory reservation
- concurrent dual-leg execution
- idempotent order management
- partial-fill handling
- residual hedging
- inventory management
- rebalancing recommendations
- central risk controls
- kill switches
- market replay
- paper trading
- shadow trading
- security
- observability
- latency benchmarking
- chaos testing
- soak testing
- documentation

Continue until every approved acceptance criterion and critical invariant has objective evidence and the release-candidate report is complete.

Do not:

- activate live trading
- use real-money trading credentials
- deploy to production
- enable withdrawal permissions
- automate external transfers
- enable margin or derivatives
- weaken risk limits
- publish performance claims

without explicit human authorization.

Begin with the public-market-data milestone only.

```
