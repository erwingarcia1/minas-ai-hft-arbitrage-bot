# Benchmark Strategy

> **Status: PARTIALLY IMPLEMENTED — Phase-1 observation metrics only.**
> What exists in code today (`arb/monitoring/metrics.py`) is the Phase-1
> observation slice: feed-latency histograms (explicitly including local↔
> exchange clock skew), book-age gauges, gross/net spread histograms,
> opportunity counts/durations, and feed/recording health counters. No
> decision-latency measurement, no hot-path benchmarks, no baseline archive,
> and no execution/recovery/financial metrics exist — those are specified here
> for Milestone 2 (paper execution) and Phase 3 (limited live). Sources:
> CLAUDE.md §12 (event timestamp ladder), §13 (hot-path requirements), §37
> (metric families), §42 step 6 (baseline discipline), §2 (honesty about
> "high frequency").

## 1. Honesty rules (bind every benchmark and every report)

1. **The Phase-1 Python observer is not claimed to be HFT-grade.** §2 defines
   what high-frequency engineering means for this project; a Python asyncio
   process does not meet the hot-path requirements of §13 and is not presented
   as doing so. Its job is measurement, not speed.
2. **Never use exchange timestamps alone to claim latency performance**
   (§12). Any metric derived from an exchange-supplied timestamp minus a local
   wall clock includes unbounded clock skew and must be labelled as such — the
   existing `arb_feed_latency_ms` help string does exactly this, and PROGRESS.md
   records it as "trend signal, not absolute".
3. **Do not publish performance claims without explicit human authorization**
   (§46), and never claim the system is high-frequency, world-class, or
   production-ready without objective evidence (preamble).
4. **Monotonic clocks for all internal durations**; wall clocks only for
   correlation with external events, and only under monitored time
   synchronization (§12).

## 2. The timestamp ladder (§12)

Every event must eventually carry these timestamps. Status is per today's code.

| # | Timestamp | Phase | Status today |
|---|---|---|---|
| 1 | Exchange event timestamp | 1 | **Exists** — `exchange_ts_ms` stamped on every applied book update (all four adapters) |
| 2 | Network receive timestamp | 1 | **Exists** — `wall_ns` + `mono_ns` captured at frame receipt in `WsFeed` and passed to `handle_raw(raw, wall_ns, mono_ns)` |
| 3 | Decoder completion timestamp | 2+ | Not recorded (decode and apply are one step in the observer) |
| 4 | Order-book application timestamp | 1 | **Exists** — books store `mono_ns`/`wall_ns` of the applied update; the replay journal records all three domains per mutation (`arb/recording/recorder.py`) |
| 5 | Opportunity detection timestamp | 1 | **Exists** — `mono_ns` on each detector result; opportunity records carry open/close times and duration (`arb/signals/spread.py`) |
| 6 | Risk-decision timestamp | 2 | Not implemented (no risk engine beyond the staleness monitor) |
| 7 | Order-send timestamp | 2 (paper) / 3 (live) | Not implemented — no order code exists |
| 8 | Exchange acknowledgement timestamp | 3 | Not implemented |
| 9 | First-fill timestamp | 3 | Not implemented |
| 10 | Final-fill timestamp | 3 | Not implemented |

Rules for rungs 3 and 6–10 when they are built: stamp with the same dual
`(wall_ns, mono_ns)` pair the feeds already use; durations are always
mono−mono; wall values exist only for journal correlation. The replay clock
(`arb/replay/clock.py`) already models this two-domain discipline and must be
the time source in replay so recorded and replayed durations agree.

## 3. What to measure

### 3.1 Latency chain (§12)

Feed latency, decode latency, book-update latency, decision latency,
risk-check latency, order-serialization latency, network round-trip time,
acknowledgement latency, fill latency, complete-cycle latency. Each is a
difference of two rungs of the ladder above and inherits their status: today
only feed latency (skew-including, rung 1→2) is measured; book-update and
detection durations are recordable from the journal but not yet exported as
histograms; everything from decision latency onward is specified, not built.

### 3.2 Metric families (§37)

| Family | Required metrics (§37) | Status today |
|---|---|---|
| Market data | feed latency p50/p95/p99/p99.9, message rate, sequence gaps, reconnects, book rebuild time, stale-book incidents | **Partial**: `arb_feed_latency_ms` (histogram), `arb_ws_messages_total`, `arb_book_resyncs_total{reason}` (covers gaps/checksum failures), `arb_ws_reconnects_total`, `arb_book_age_ms`, `arb_venue_usable`. Book rebuild *time* not yet measured |
| Opportunity engine | opportunities detected, depth-qualified, risk-approved, half-life, expected-profit distribution, rejected-opportunity reasons | **Partial**: `arb_spread_gross_bps`/`arb_spread_net_bps` histograms, `arb_opportunities_total`, `arb_opportunity_duration_ms`; risk-approval and rejection-reason counters await the Phase-2 risk engine |
| Execution | order-send/ack/fill latency, dual-leg skew, full-cycle latency, fill/partial-fill/mismatch/rejection rates, cancel success rate | **Not implemented** — Phase 2 (simulated) / Phase 3 (live) |
| Recovery | residual-exposure duration, hedge latency, hedge slippage, recovery loss, manual-intervention rate | **Not implemented** — Phase 2+ |
| Financial | gross spread captured, fees, slippage, adverse selection, hedge loss, rebalancing cost, net realized profit (by pair/symbol/period) | **Not implemented**; Phase-1 JSONL log records *theoretical* net edge only, never labelled realized (§34, §40.22) |
| Inventory | inventory deviation, capital utilization, unavailable capital, rebalancing frequency/cost | **Not implemented** — Phase 2+ |

Supporting counters that exist today and feed the families above:
`arb_message_errors_total`, `arb_records_written_total`,
`arb_record_write_errors_total`, `arb_recording_events_total`,
`arb_recording_errors_total`.

## 4. Percentile reporting

- Report **p50 / p95 / p99 / p99.9** for every latency metric, per venue (and
  per symbol where labelled), per §37 and §42 step 6.
- Current implementation uses Prometheus histograms with fixed buckets
  (`arb/monitoring/metrics.py`: latency 5→5000 ms, spreads −50→+50 bps,
  durations 50 ms→60 s), so quantiles are bucket-interpolated. That is
  adequate for Phase-1 trend observation; it is **not** adequate for hot-path
  claims, where p99.9 demands exact recording (e.g. HDR-style histograms or
  raw sample capture in benchmark runs) and enough samples that the tail is
  populated — a p99.9 quoted from hundreds of samples is noise and must not
  be reported.
- Every published percentile must state: measurement window, sample count,
  clock domain (mono vs wall), and whether skew is included.

## 5. Baseline comparison discipline (§42 step 6, §13)

1. **Benchmark every optimization** (§13: "Do not optimize based on
   intuition"). A performance change without a before/after measurement is
   rejected in review.
2. Each accepted benchmark run becomes the **prior accepted baseline**:
   committed to the repository (machine-readable results plus environment
   description — host, region, Python/Rust version, config hash, git rev).
3. Step 6 of every iteration compares CPU, allocations, throughput, and
   p50/p95/p99/p99.9 latency against that baseline. **Do not accept a
   material regression without documented justification** recorded in
   PROGRESS.md and, when it changes a decision, docs/DECISIONS.md.
4. Comparisons are only valid like-for-like: same hardware class, same
   dataset (use the deterministic replay journal — same recorded stream and
   config must produce the same result, §35), same measurement window.
5. No baseline archive exists yet; establishing one (starting with replay
   throughput and detector-evaluation cost on recorded data) is the first
   deliverable of this strategy once Iteration 2's recorder/replay work lands.

## 6. Hot-path benchmarks (specified for the execution phases)

When the latency-critical engine is built (Rust per §6; deferred per
docs/DECISIONS.md D-002), the following must be benchmarked continuously, not
once:

- **Allocation counts** in the decode→book→detect→risk→serialize path (§13
  requires minimal allocations; the benchmark proves it).
- **Decision latency** (rung 4→6) and **order-serialization latency** under
  recorded-burst load, reported at all four percentiles.
- **Throughput**: messages/sec sustained per venue without book-age
  violations.
- **Network RTT** to each permitted venue endpoint from each candidate
  region — this is the deployment-topology input; procedure and decision
  table live in `docs/DEPLOYMENT.md` §6.
- **Regression detection** wired into the loop (§41 Low-Latency Systems
  Engineer owns it; §42 step 6 gates on it).

Microbenchmarks alone are insufficient: full-cycle latency (rung 1→10) must
be measured end-to-end in paper mode against the same replay data, because
§13's prohibitions (blocking I/O, sync logging, global locks) fail in
composition, not in isolation. Chaos and soak runs (§39) must record the same
metric families so degradation under failure is quantified, not narrated.

## 7. Phase-1 measurement caveats (recorded, not hidden)

- `arb_feed_latency_ms` = exchange event time → local wall arrival; includes
  clock skew (help string says so). Use it for trend and cross-venue
  comparison over short windows only.
- Book age (`arb_book_age_ms`) is pure-monotonic and skew-free; it is the
  staleness gate's input and trustworthy as an absolute number.
- Opportunity duration is mono−mono and skew-free, but the detector is
  event-driven (it evaluates on each applied book update), so open/close
  resolution is bounded by update arrival and Python event-loop scheduling;
  durations near that granularity are lower bounds, not precise lifetimes.
- All Phase-1 numbers were produced by a non-colocated, GC'd, single-process
  observer; none of them describe what an execution engine would achieve, and
  no document may cite them as if they do.
