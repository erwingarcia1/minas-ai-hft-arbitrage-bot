# Decision Log (ADR)

> **Status: IMPLEMENTED decisions about implemented code.** Every decision
> below is reflected in the current source and cited by file. Required by
> CLAUDE.md §7; format: context → decision → consequences. Decisions are
> immutable once accepted — a reversal gets a new numbered entry that
> supersedes, never an edit. Recorded 2026-07-11 (D-001 and D-002 originate
> in iteration 1, commit e5d762d; the rest in iteration 2 — see
> `PROGRESS.md`).

---

## D-001 — Native WebSocket protocol implementations instead of ccxt.pro

- **Date:** 2026-07-11 · **Status:** accepted

**Context.** Four venues need synchronized local L2 books (§11, §48). The
obvious shortcut is ccxt.pro, which maintains books behind a uniform API.
But every venue's book-sync protocol differs in exactly the places that
matter: Binance's `U`/`u`/`lastUpdateId` snapshot-merge algorithm, Bybit's
`u`-continuity with the `u==1` restart rule, OKX's `seqId`/`prevSeqId`
chain, Bitget's per-message CRC32 checksum (its only integrity mechanism —
the v2 books channel has no documented sequence chain).

**Decision.** Implement each venue's protocol natively (`arb/feeds/binance.py`,
`arb/feeds/bybit.py`, `arb/feeds/okx.py`, `arb/feeds/bitget.py`) on a shared
supervision base (`arb/feeds/base.py`). Per the README: "gap detection and
resync behaviour *are* the product in an arb system. ccxt.pro abstracts
exactly the failure modes we need to observe."

**Consequences.** We own protocol maintenance (venue spec changes land on
us — e.g. OKX's books-checksum deprecation is handled explicitly in
`arb/feeds/okx.py`). In exchange: sequence gaps, checksum mismatches, resync
storms and snapshot deadlines are first-class, tested, metric-visible events
(`arb_book_resyncs_total{reason=…}`, `tests/test_feeds_sync.py`), and §9's
"do not force exchanges into an incorrect abstraction" holds. ccxt.pro
remains an acceptable fallback behind the same `WsFeed` interface if a
venue's protocol churns faster than we maintain it.

---

## D-002 — Keep the Python asyncio observer for Modes 1–2; defer the Rust engine

- **Date:** 2026-07-11 · **Status:** accepted

**Context.** §6 prescribes Rust for the latency-critical engine — and also
opens with "first preserve any sound existing architecture." A sound,
tested Python 3.11 asyncio observer already existed (iteration 1). Milestone
1 places no orders; its product is *data* — book integrity, opportunity
lifetimes, net-of-fees spread distributions — not execution latency.

**Decision.** The observer (Modes 1–2, §5) stays Python. It is research
tooling, not the trading hot path; §13's hot-path requirements attach to a
decision-and-execution path that does not exist yet. The Rust
latency-critical engine is deferred until observation data proves there is
edge worth executing and Milestone 2+ is specified and approved.

**Consequences.** Faster, safer iteration on the part of the system whose
correctness matters now, at the cost of a GC'd runtime and higher processing
latency — acceptable because nothing here trades, and the persistence-floor
analysis (D-007) explicitly discounts opportunities too short-lived for a
slow observer to matter. Rewriting later risks divergence; mitigated by the
replay journal, which is language-neutral JSONL a future engine can be
validated against (same events + config ⇒ same decisions). No
binary-float-money rule is violated in stored state: books are Decimal-exact
(D-005).

---

## D-003 — Record normalized book events at the `OrderBook` tap seam, not raw venue frames

- **Date:** 2026-07-11 · **Status:** accepted

**Context.** §35 demands deterministic replay. Recording raw WS frames would
force the replay engine to re-run four venue protocols (snapshot merges,
sequence validation, checksum verification) — every protocol bug would be a
replay-divergence bug, and per-feed recording hooks could silently miss a
mutation path.

**Decision.** Record *applied* book events at the single seam all mutations
flow through: `OrderBook.tap` (`arb/feeds/book.py`), fired by
`apply_snapshot`/`apply_deltas`/`clear` and the `synced` setter. The
recorder (`arb/recording/recorder.py`) attaches to every book in the
registry; replay (`arb/replay/engine.py`) re-applies events to fresh books
and drives the same `SpreadDetector` — no protocol logic runs in replay.

**Consequences.** Recording completeness is **structural**: a mutation that
bypassed the tap cannot exist, because the tap fires inside the mutation
methods themselves (verified by `tests/test_recording.py`). The journal
captures post-integrity-check state, so replay cannot re-test venue protocol
handling — that is what the fixture-based feed tests are for. File order
equals application order (single event loop, single writer), which is the
replay order. A raising tap is contained and logged (`_emit` guard):
recording failures degrade recording, never market-data processing.

---

## D-004 — Control-flow events journaled at last data-frame stamps; one time source per journal

- **Date:** 2026-07-11 · **Status:** accepted

**Context.** Sync transitions and clears happen in feed control flow
(reconnects, resyncs), not on a data frame, so they carry no exchange
timestamp of their own. Stamping them with `time.monotonic_ns()` at emission
would mix two time sources in one journal — replay determinism (§35) would
then depend on when the recording machine's control flow happened to run
relative to data frames.

**Decision.** `sync`/`unsync`/`clear` events are journaled at the book's
**last data-frame stamps** (`OrderBook.synced` setter and `clear()` in
`arb/feeds/book.py`). Every recorded timestamp therefore originates from
feed data — a single time source per journal — and the `ReplayClock` is
never advanced by a control-flow event. File order still records the true
application order, so replay applies them in the right sequence.

**Consequences.** Deterministic clocks in replay: durations, staleness
decisions and record timestamps derive only from recorded data stamps
(`arb/replay/clock.py`; `tests/test_replay.py::test_durations_come_from_recorded_clock`).
The trade-off is documented fidelity skew: live unusable-book closes can lag
up to one staleness-monitor tick (100 ms); replay closes at the next event
for that symbol. Replay-to-replay output is exact; live-to-replay matches
within those scheduling skews (`arb/replay/engine.py` fidelity notes, and
the live-vs-replay equivalence test).

---

## D-005 — Decimal-exact book storage with float views for signal math

- **Date:** 2026-07-11 · **Status:** accepted

**Context.** OKX and Bitget CRC32 checksums are computed over the *literal*
price/size strings the exchange sent — trailing zeros included. Float
round-tripping destroys that formatting (and `str(Decimal("0.0000001"))`
yields `"1E-7"`, which would also corrupt payloads — hence `_plain()` using
`format(d, "f")`). Meanwhile bps-level spread math does not need exact
decimal arithmetic, and §6 requires only that money-bearing *state* avoid
binary floats.

**Decision.** Books store `Decimal` keyed exactly as received
(`arb/feeds/book.py`: `_set_level` parses the literal string; checksums
reconstruct it). Signal math reads float views (`iter_bids`/`iter_asks`/
`vwap_for_qty`): float64's ~15–16 significant digits are far beyond the
precision that bps-level edge math needs.

**Consequences.** Checksums verify byte-for-byte
(`tests/test_book.py::test_checksum_preserves_trailing_zeros`,
`test_checksum_never_uses_scientific_notation`); recorded journals
round-trip literal strings (D-003). The float boundary is one-directional
and read-only — no float ever writes back into book state. Instrument
rounding runs in `Decimal` end-to-end (`round_qty_for_pair`,
`arb/instruments/registry.py`) and only converts to float at return.
Execution-phase accounting (Milestone 2+) must NOT inherit the float views;
it gets fixed-point/Decimal arithmetic per §6 — this decision covers signal
research math only.

---

## D-006 — Checked-in `instruments.yaml` is a conservative UNVERIFIED snapshot with a fetch-refresh path

- **Date:** 2026-07-11 · **Status:** accepted

**Context.** The detector needs per-venue tick/step/minimum metadata (§10,
§16) to round clip quantities to venue rules. This development sandbox
blocks egress to exchange endpoints, so live metadata could not be fetched
at build time — and hard-coding published limits as permanent constants is
forbidden (§8).

**Decision.** Ship `config/instruments.yaml` as a static snapshot explicitly
labelled UNVERIFIED (file provenance in `source`/`fetched_at` fields;
README and `config/config.yaml` comments repeat the warning), with values
chosen in the conservative direction — a too-coarse step or too-high minimum
under-sizes clips and can only *understate* viability, never overstate it.
Provide `python -m arb instruments fetch` (`arb/instruments/fetch.py`) to
regenerate the file from the venues' public REST endpoints; the four payload
parsers are pure functions pinned by fixture tests
(`tests/test_instruments.py`), and a partial fetch aborts whole — nothing is
written until every requested venue parsed.

**Consequences.** Research output is honest about its metadata provenance;
operators must run the refresh from a machine with exchange access before
trusting the report. Loading rejects metadata whose `exchange_symbol` does
not match the venue transform (a typo would price venue A's book against
venue B's rules). A missing entry degrades to unrounded pricing with a
boot-time WARNING; a suspended instrument is excluded from evaluation
entirely (`arb/signals/spread.py`).

---

## D-007 — Open-time economics plus a persistence floor; no capture claims

- **Date:** 2026-07-11 · **Status:** accepted

**Context.** §34 forbids mixing theoretical and realized categories; §17
forbids opaque scores; §48.10 wants a ranked report of what *appears* viable.
Any single number summarizing an unfilled opportunity involves a choice:
best price during its life (flattering), mean (unmeasurable execution
assumption), or price at open (what a taker acting on the trigger would
actually have crossed against).

**Decision.** `OpportunityRecord.est_profit_usdt_open` is computed at open
time (`clip × net_bps_open / 1e4`, `arb/signals/spread.py:_close`), with
`max_net_bps`/`mean_net_bps` recorded alongside as context, never as score
inputs. The report (`arb/report/viability.py`) sums `est_profit_usdt_open`
only over opportunities whose measured persistence ≥ `min_duration_ms` — a
parameter modelling round-trip reality (README: 150–300 ms+ non-colocated),
not an opinion. The formula is printed in the module docstring and the
output carries a baked-in note: **THEORETICAL edge; NOT realized profit —
this system places no orders.**

**Consequences.** No claim of capture, fill probability, queue position or
adverse selection is embedded — those belong to Milestone 2+ simulation
(§36) and would be fiction here. The score is explainable and backtestable
(§17), deterministic under replay (same journal ⇒ same records ⇒ same
report), and deliberately pessimistic-leaning: an opportunity that decayed
40 ms after opening contributes nothing at any realistic floor. Evidence:
`tests/test_report.py` (persistence gating, ranking, THEORETICAL labelling
in the JSON shape).
