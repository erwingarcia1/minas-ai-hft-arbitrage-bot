# HOT_PATH_ARCHITECTURE.md — The Decision Path, Honestly

> **Status: IMPLEMENTED (Phase 1 observer decision path) / SPECIFIED — NOT
> IMPLEMENTED (the §13-compliant execution hot path).** This document
> describes what the frame→decision path *is* in the current Python observer,
> audits it line by line against CLAUDE.md §13, and states plainly which §13
> requirements this build does not meet and why that is acceptable for
> Modes 1–2 (observation, replay) and **not** acceptable for execution.
> There is no execution hot path to document because there is no execution
> code (`arb/execution/` is empty by design).

## 1. What "hot path" means in this build

There are no orders, so the latency-critical path in the §13 sense does not
exist yet. What exists is the **frame→decision path**: the synchronous work
performed on the event loop between a WebSocket frame arriving and the spread
detector finishing its re-evaluation. Its correctness and determinism are the
Phase-1 product; its latency merely needs to be small relative to what it
measures (opportunity persistence at the 100 ms–1 s scale, against a
150–300 ms non-colocated round-trip floor — README "Latency reality").

The path, end to end (all on one asyncio loop, no awaits between stamp and
decision other than the venue protocol's own parsing):

```
frame arrives            WsFeed.run              arb/feeds/base.py
  time.monotonic_ns() + time.time_ns() stamped; counter inc
parse + integrity        <venue>.handle_raw      arb/feeds/*.py
  orjson.loads; sequence chain / CRC32 checksum verification
book apply               OrderBook.apply_*       arb/feeds/book.py
  Decimal levels into SortedDict; timestamps stamped
journal tap              BookEventRecorder       arb/recording/recorder.py
  orjson.dumps appended to an in-memory buffer;
  file write+flush only every 500 events or 1 s
evaluate                 SpreadDetector.evaluate arb/signals/spread.py
  staleness gate → venue permutations × clips →
  round_qty_for_pair → vwap_for_qty × 2 → net_edge_bps →
  open/extend/close; Prometheus histogram observes
research record          OpportunityLog.write    arb/accounting/opportunities.py
  only on open→close transitions and sampler sweeps:
  one <1 KB JSONL line, written and flushed synchronously
```

What the path does **not** contain: database calls (no database exists),
REST calls (the Binance snapshot fetch runs in a separate per-symbol resync
task while the book is `synced=False` and therefore invisible to signals —
`arb/feeds/binance.py`), locks (single loop, single writer per book), thread
handoffs (the only thread is the `prometheus_client` scrape server, which
never touches the loop — `Metrics.start_server`), and LLM calls (none exist
anywhere in the repository).

## 2. §13 audit — item by item

§13 forbids the following in the decision/execution path. Current standing:

| §13 forbids | This build | Verdict |
|---|---|---|
| Blocking database calls | No database anywhere | **meets** |
| Filesystem writes | Two, deliberate: (a) recorder buffer flush every `_FLUSH_EVERY`=500 events or 1 s (`arb/recording/recorder.py`); (b) research-log append+flush per opportunity transition/sample — "<1 KB, low rate, OS page cache — microseconds, not a latency hazard at Phase 1 rates. If Phase 2 telemetry rates grow, this becomes a queue + writer task" (`arb/accounting/opportunities.py` docstring) | **deviates, documented** |
| Synchronous logging | stdlib `logging` is synchronous. Per-frame path logs only on errors; opportunity open/close INFO lines are on the transition path | **deviates, documented** |
| Memory allocation where avoidable | Python allocates per event: parsed dicts from `orjson.loads`, `Decimal` objects per level, `EvalResult`/`_OpenOpp` instances (slotted, but still heap), list comprehensions in `evaluate` | **does not meet** |
| String parsing after normalization | Levels are parsed to `Decimal` once on apply, but float views (`iter_bids`/`iter_asks`) re-convert `Decimal`→`float` on every evaluation | **does not meet** |
| Repeated symbol lookup | Dict lookups per event (`BookRegistry.get`, `instruments.get` inside `_price`); suspended-instrument set, threshold, clips, and fee accessors are precomputed/cached at detector construction | **partial** |
| Repeated fee lookup | `settings.taker_bps(venue)` is a dict hit per pricing call | **partial** |
| Global locks | None in code; CPython's GIL exists but is uncontended (no competing data-path threads) | **meets (with GIL caveat)** |
| GC-runtime pauses | CPython refcounting + cyclic GC — inherent to the language choice | **does not meet** |
| LLM calls | None, verifiable by inspection; no LLM dependency in `pyproject.toml` | **meets** |
| HTTP calls unrelated to execution | Metrics served from a daemon thread off-loop; Binance REST only inside the resync task (book unusable meanwhile); `arb instruments fetch` is an offline command | **meets** |
| REST polling for opportunity discovery (§2) | None — discovery is push-only WS | **meets** |
| External message brokers unless benchmarked | None used | **meets** |

And what §13 prefers, honestly scored: preallocated objects — no; fixed-size
messages — no; interned identifiers — partial (`VenueName` enum members are
singletons); lock-free/bounded channels — the `websockets` receive queue is
bounded (`max_queue=1024`, `arb/feeds/base.py`); single-writer ownership —
**yes**, per book, structurally; cache-friendly structures — no
(`SortedDict` of `Decimal`); CPU affinity / NUMA — no; batch telemetry
outside the hot path — yes for the recorder (buffered) and metrics scrape
(off-thread), no for the research log (by documented choice); append-only
asynchronous journaling — append-only yes, asynchronous no.

## 3. Where the allocations are

For a future benchmark pass (per `docs/BENCHMARK_STRATEGY.md`), the known
allocation sites on the frame path, in order of frequency:

1. `orjson.loads(raw)` — one dict tree per frame, every venue adapter.
2. `Decimal(price_s)` / `Decimal(size_s)` per changed level
   (`OrderBook._set_level`).
3. `list(bids)` / `list(asks)` copies in `apply_snapshot`/`apply_deltas`
   (needed so the tap sees exactly what was applied).
4. Tap record dict + `orjson.dumps` bytes per journaled event
   (`BookEventRecorder.on_book_event`).
5. `evaluate`: usable-books dicts, `permutations`, per-clip `EvalResult`
   (slotted dataclass), float conversions inside `vwap_for_qty`'s generator
   walk.
6. `Decimal(str(qty))` and `Fraction` math in `round_qty_for_pair` — exact
   arithmetic bought with allocations, deliberately.

None of these are accidental; all of them are unacceptable in a §13-compliant
execution engine and all are within the seams listed in §5 below.

## 4. What deliberately does NOT meet HFT standards — and why that is fine here

**Not met, knowingly:** interpreted CPython dispatch; per-event heap
allocation and GC; `Decimal` (software decimal) book storage; float views
recomputed per evaluation; `SortedDict` with Python-level comparisons;
asyncio scheduling jitter (the staleness monitor ticks at 100 ms, the sampler
at ~30 s — both coarse by design); no core pinning, no preallocation, no
binary wire decoding.

**Why acceptable for Mode 1 (observe) and Mode 2 (replay):**

- The measurement target is *opportunity persistence versus a round-trip
  floor of 150–300 ms* (README). Millisecond-scale Python overhead does not
  change the answer to "did this spread live longer than 250 ms" — the
  viability report's persistence floor (`--min-duration-ms`, default 250,
  `arb/__main__.py`) is orders of magnitude above interpreter noise.
- Determinism matters more than speed here, and the build has it: the same
  recorded stream and config replay to byte-identical records
  (`arb/replay/engine.py`, verified in `tests/test_replay.py`).
- §2 explicitly warns against interpreting "high frequency" as "running a
  Python loop rapidly" and against claiming colocated-firm equivalence. This
  document is that non-claim in writing: **no latency-performance claims are
  made for this build**, and the feed-latency histograms are labelled
  skew-contaminated trend signals (`WsFeed.record_feed_latency`).

**Why NOT acceptable for execution (Modes 3+):** once orders exist, decision
latency is money (adverse selection during the decide→ack window) and
allocation/GC pauses become fill-probability variance. §6 prescribes the
answer — a Rust latency-critical engine with fixed-point scaled integers in
the hot path — and `docs/DECISIONS.md` D-002 records the decision to build it
*when observation data justifies execution work*, not before. No execution
work may begin until Milestone 1 passes independent review (§48).

## 5. The measured seams a future engine replaces

The Python observer is structured so the eventual §13-compliant engine
replaces components at seams that already exist, each with a recorded-data
benchmark available (the replay journal supplies identical inputs to both
implementations):

| Seam | Today | Replacement candidate |
|---|---|---|
| Decode + protocol integrity | `<venue>.handle_raw` per adapter (`arb/feeds/*.py`) | native decoder; SBE where a venue offers it and the account is entitled (§8 — unverified from this sandbox) |
| Book engine | `OrderBook` on `SortedDict`+`Decimal` (`arb/feeds/book.py`) | fixed-point ladder (`price_ticks: i64`, `quantity_steps: i64` per §6) |
| Feed→decision boundary | `UpdateCallback` — the REGION-SPLITTING SEAM (`arb/feeds/base.py`) | in-process call in Architecture A; bus publish in B/C (§14, `docs/DEPLOYMENT.md` §4) |
| Decision | `SpreadDetector.evaluate` (`arb/signals/spread.py`) | same math, fixed-point, preallocated; must reproduce replay output before it may replace anything |
| Journaling | tap → buffered `BookEventRecorder` | append-only async journal outside the hot path (§13) |
| Time | `Clock` protocol (`arb/replay/clock.py`) | already the right abstraction — the engine keeps it so replay determinism survives the rewrite |

Acceptance rule for any replacement (per §13 "do not optimize based on
intuition" and §42 step 6): it must (a) replay a recorded journal to
byte-identical opportunity records against the Python reference, and (b) show
the claimed improvement in a benchmark against the prior accepted baseline —
neither of which has been run, because the engine does not exist.
**SPECIFIED — NOT IMPLEMENTED.**

## 6. Timestamps available today (§12 subset)

Of the §12 event timestamps, the observer records: exchange event time
(`ets`, ms, where the venue provides it), local wall receive (`wall_ns`) and
local monotonic receive (`mono_ns`) — stamped in `WsFeed.run` at frame
arrival and carried through book application into the journal
(`arb/recording/recorder.py`) and into every staleness/duration computation
(`OrderBook.age_ms`, `SpreadDetector` via `Clock`). Decoder-completion,
book-application, and risk-decision stamps as *separate* fields, and all
order-side stamps, are **SPECIFIED — NOT IMPLEMENTED** (there are no orders).
Monotonic clocks are used for every internal duration, per §12; wall time
appears only in human-facing record fields.
