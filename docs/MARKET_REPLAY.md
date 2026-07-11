# Market Replay

> **Status: IMPLEMENTED** (Phase 1 / Milestone 1, CLAUDE.md §35, §48.8) except
> where a subsection is explicitly marked **SPECIFIED — NOT IMPLEMENTED**.
> Recording lives in `arb/recording/recorder.py` on the tap seam of
> `arb/feeds/book.py`; replay lives in `arb/replay/engine.py` and
> `arb/replay/clock.py`. Evidence: `tests/test_replay.py` (determinism and
> live-vs-replay equivalence, byte-for-byte) and the PROGRESS.md Iteration-2
> record of a 23,972-event simulated session replayed twice via the CLI to
> identical SHA-256 outputs. Order/decision-event recording does not exist
> because no order code exists (`arb/execution/` is empty by design).

## 1. Purpose

CLAUDE.md §35 requires recording sufficient normalized data to reconstruct
historical state deterministically: *the same event stream and configuration
must generate the same result*. In this build the "result" is the detector's
research output — opportunity open/close records and periodic spread samples
(`arb/signals/spread.py`) — and the recorded stream is every local order-book
state change. Replay is Mode 2 of CLAUDE.md §5: it places no orders, reads no
credentials, and needs no network.

## 2. Recording

### 2.1 The tap seam — structural completeness

Every `OrderBook` mutation flows through exactly four paths:
`apply_snapshot()`, `apply_deltas()`, `clear()`, and the `synced` property
setter (`arb/feeds/book.py`). Each fires the optional `tap` callback
(`BookTap`) with the event kind, the **literal** price/size strings the
exchange sent, and the timestamps. `BookEventRecorder.attach()` sets itself
as the tap on every book in the `BookRegistry`
(`arb/recording/recorder.py`), so recording completeness is structural —
there is no per-feed recording code that a new adapter could forget.

Event kinds (`BookEventKind` in `arb/feeds/book.py`):

| Kind | Meaning | Levels carried |
|---|---|---|
| `snapshot` | levels replaced wholesale (book cleared first) | full snapshot |
| `delta` | incremental level changes applied (size `"0"` = removal) | changed levels |
| `clear` | levels dropped (disconnect/resync); an `unsync` follows if the book was synced | none |
| `sync` | `synced` False → True | none |
| `unsync` | `synced` True → False | none |
| `eval` | a live evaluation point (journaled by `BookEventRecorder.on_evaluation`, not by the tap) | none |

`eval` events exist because evaluation is **not inferable from mutations**:
feeds set state and then run integrity checks (checksum, crossed book), and
deliberately do not invoke the update callback for state that failed — so a
replay that evaluated after every mutation would price books live rejected
as corrupt. The production callback wiring (`on_book_update` in
`arb/__main__.py`) journals the evaluation point first, then evaluates.
Feed-teardown unsyncs at process shutdown are excluded from the journal
(`BookEventRecorder.detach()` runs before feed cancellation): they are
shutdown mechanics, not market data, and journaling them would make replay
close still-open opportunities as `book_unusable` instead of reproducing
the live `shutdown` flush.

A raising tap is contained and logged inside `OrderBook._emit()` — recording
failures degrade recording, never market-data processing
(`tests/test_recording.py::TestBookTap::test_raising_tap_does_not_break_mutation`).

### 2.2 Time-stamping rules — one time source per journal

Data-frame events (`snapshot`/`delta`) carry three stamps: exchange event
time (`ets`, ms, as sent — may be null), local wall clock (`wall_ns`), and
local monotonic clock (`mono_ns`), captured by the feed at receive time.

Control-flow events (`sync`/`unsync`/`clear`) happen in feed control flow —
not on a data frame — so they have no stamps of their own. The `synced`
setter and `clear()` journal them **at the book's last data-frame stamps**
(`last_local_mono_ns` / `last_local_wall_ns`). Consequence: every recorded
timestamp originates from feed data — a single time source per journal — and
the replay clock is never advanced by control-flow events. File order still
records the true application order (single asyncio event loop, single
writer), which is the replay order. `tests/test_replay.py` deliberately
places its synthetic timeline far ahead of the test machine's real clocks to
prove control-flow stamps cannot drag the replay clock off the recorded
timeline.

### 2.3 JSONL format

One file per observer session: `books-<UTC-stamp>.jsonl` under
`recording.dir` (default `data/recordings/`). `SCHEMA_VERSION = 1`. Three
record types, serialized with `orjson`:

**`recording_header`** (line 1; built by `header_record()`), a
detector-relevant config snapshot so replay can verify comparability:

| Field | Content |
|---|---|
| `schema_version` | `1` |
| `started_at`, `wall_ns`, `mono_ns` | session start stamps (both clock domains) |
| `symbols`, `venues` | active canonical symbols and venue names |
| `signals.min_net_edge_bps`, `signals.clip_sizes_usdt` | detector thresholds/clips |
| `staleness_ms` | book-usability threshold |
| `taker_bps` | per-venue taker fee (bps) in effect |
| `spread_sample_interval_s` | live sampler cadence |
| `instruments` | per-`venue:symbol` snapshot of the metadata the pricing consumes (`status`, `qty_step`, `min_qty`, `min_notional`, `max_qty` as Decimal-exact strings): a refreshed instruments file changes the money math and must be as visible on replay as a changed fee |

**`book_event`** (one per tap firing or evaluation point): `v` (venue), `s`
(symbol), `k` (kind), `ets` (exchange ms or null), `mono_ns`, `wall_ns`,
`b`/`a` (bid/ask levels as literal `[price, size]` string pairs —
Decimal-exact round trip).

**`recording_end`** (last line; written by `close()`): `mono_ns`/`wall_ns`
of shutdown. Replay uses it to close still-open opportunities at the clock
of the live session's shutdown flush (within scheduling µs — gap 4 in §5).
In `run_observer()` (`arb/__main__.py`) the recorder closes *after*
`detector.close_all("shutdown")` so the end stamp is ≥ the shutdown-flush
clock.

### 2.4 Writer and failure modes

Writes are buffered: flush every `_FLUSH_EVERY = 500` events or
`_FLUSH_INTERVAL_NS = 1s` of recorded monotonic time, whichever first. A
crash loses at most one buffer — research-grade loss tolerance, counted,
never silent. Each write failure is logged and counted in
`arb_recording_errors_total`; after `_MAX_CONSECUTIVE_ERRORS = 10`
consecutive failures the recorder disables itself loudly (log line:
"recording DISABLED …") instead of spamming a dead disk — market-data
collection continues either way
(`tests/test_recording.py::TestRecorder::test_disables_after_consecutive_write_failures`).
Journaled events are counted per venue in `arb_recording_events_total`.

### 2.5 Configuration

`RecordingSettings` (`arb/models.py`): `enabled` (model default `false`,
keeping unit tests hermetic) and `dir`. The shipped `config/config.yaml`
sets `enabled: true`. Disk cost is real — the journal is sized like the raw
feeds; budget for it or disable deliberately.

## 3. Replay

### 3.1 The clock abstraction

`Clock` is a protocol (`arb/replay/clock.py`) with `mono_ns()`, `wall_ns()`,
`now_utc()`. Live code uses `SystemClock` (the default everywhere); the
`SpreadDetector` takes an injectable clock, so every duration, book age,
staleness decision and record timestamp in replay derives from the recording
— never from the machine performing the replay. `ReplayClock.advance()`
refuses to run backwards within a session (out-of-order stamps indicate a
corrupt recording and clamp forward); `reset()` rebases both domains at a
session boundary, where a new process's monotonic domain legitimately
restarts.

### 3.2 Event application

`run_replay()` (`arb/replay/engine.py`) streams each input file line by line
(journals grow to GBs; never loaded whole). Per `book_event`: run every due
periodic task (see §3.3), advance the clock to the event's stamps, then
apply the event to a fresh `BookRegistry`
(`apply_snapshot`/`apply_deltas`/`clear`/`synced`). The detector is
evaluated ONLY where live evaluated it — at `eval` events, monitor grid
ticks, and sample-sweep boundaries — never inferred from mutations, so book
state the live feeds rejected on integrity grounds is never priced
(`tests/test_replay.py::TestLiveReplayEquivalence::test_integrity_rejected_state_is_never_priced`).
Events for venues/symbols outside the active config are skipped and counted
(`events_skipped`); replaying a wide recording with a narrow config is
legitimate. A malformed line raises `ReplayError` with the input file and
line number; a failing output sink raises `ReplayError` naming the sink —
an intact journal is never blamed for an output failure
(`test_sink_failure_is_not_blamed_on_the_journal`).

### 3.3 Periodic reconstruction: monitor grid + sample sweeps

Live runs two periodic tasks; replay reconstructs both on deterministic
grids seeded from the recording header's `mono_ns`
(`_Replayer._run_periodic`):

- **Staleness monitor.** The REAL `StalenessMonitor.tick()` (extracted for
  exactly this purpose — `arb/risk/staleness.py`) runs on the same 100 ms
  cadence (`TICK_S`) as the live task. Venue-level `venue_unusable` closes
  and per-book stale `book_unusable` closes therefore carry live's reasons
  and semantics
  (`tests/test_replay.py::TestLiveReplayEquivalence::test_monitor_grid_closes_venue_wide`).
- **Sample sweeps.** Live, `_sampler()` (`arb/__main__.py`) fires on an
  ABSOLUTE deadline grid anchored at the same session anchor the header
  stamps (drift-free by construction); replay fires each boundary at
  exactly its grid clock
  (`tests/test_replay.py::TestReplayMechanics::test_sample_sweep_at_recorded_cadence`).

Due periodics run in chronological order; on an exact tie the sample sweep
fires first — an arbitrary but FIXED convention (live ordering at
coincident deadlines is scheduler-dependent; determinism needs one rule).

### 3.4 Session boundaries and truncated recordings

A second `recording_header` (multiple input files, or a restarted session)
is treated as the live equivalent of a process restart: open opportunities
are flushed with reason `shutdown`, all books are cleared, and the clock is
`reset()` to the new header's stamps
(`test_two_sessions_replay_as_restart`). A recording that ends without a
`recording_end` stamp (crash) still flushes open opportunities at the last
event's clock via `_Replayer.finish()`
(`test_truncated_recording_flushes_open_opportunities`).

### 3.5 Config divergence — sensitivity analysis

`_compare_header()` compares the header against the active config
(symbols, venues, threshold, clips, staleness, per-venue taker fees,
sampler interval, AND the instrument-metadata snapshot) and appends a
warning to `ReplayStats.warnings` on any divergence — e.g. "sensitivity
run, not a reproduction". Divergence is a *supported* use (§35 lists
threshold/fee sensitivity as a replay purpose) but must be visible so
nobody mistakes a what-if run for a reproduction
(`test_config_divergence_warns` shows a 50 bps threshold replay producing
zero opportunities, with the warning; `test_instrument_divergence_warns`
covers a changed instruments file).

### 3.6 CLI

```bash
python -m arb replay --input data/recordings/books-*.jsonl \
    [--output PATH | --output -]        # default: <data_dir>/replays/replay-<utc>.jsonl
```

Prints a `ReplayStats` summary (files, sessions, events applied/skipped,
sample sweeps, opportunities, records) plus all warnings. Replay metrics go
to a throwaway private `CollectorRegistry` — replay reports through its
returned stats and record sink, not Prometheus.

## 4. Determinism contract and evidence

Contract (§35): **same input files + same configuration ⇒ byte-identical
records.** Evidence:

- `tests/test_replay.py::TestLiveReplayEquivalence::test_replay_is_deterministic_across_runs`
  — two replays of the same journal produce equal record lists.
- `test_replay_reproduces_live_records_exactly` — a session driven exactly
  the way feeds drive books (apply → synced → evaluate), recorded through
  the production tap, replays to records identical to the live detector
  output, compared via `orjson.dumps` byte equality. This is the §48.8
  acceptance evidence: the journal captures everything the detector's
  decisions depend on.
- `test_durations_come_from_recorded_clock` — opportunity durations and
  wall-clock stamps derive from the recorded timeline, not the replaying
  machine.
- PROGRESS.md (Iteration 2): a seeded synthetic 4-venue × 2-symbol session
  (5 min, 200 ms cadence, scripted dislocations plus one feed outage; 23,972
  applied events including journaled evaluation points) recorded through the production journal and replayed twice
  via the CLI produced identical SHA-256 output hashes (64 opportunities,
  717 samples, 10 sample sweeps). The driver scripts live outside the repo
  by design — they are test drivers, not product.

## 5. Live-vs-replay fidelity gaps (documented, deliberate)

Replay-to-replay output is exact. Live-to-replay matches to within four
known scheduling skews — none of which changes *what* was priced, and all
of which are bounded per occurrence (nothing accumulates):

1. **Periodic-task lateness.** Live monitor ticks and sample sweeps wake
   LATE by asyncio scheduling delay and stamp records with the
   really-observed clock; replay fires them at their exact grid stamps.
   Both schedules are anchored absolute grids (the live sampler
   deliberately so — `_sampler()` in `arb/__main__.py`), so the skew is
   per-occurrence wakeup lateness, not drift.
2. **Control-flow transitions in frame gaps.** Sync transitions carry
   last-data-frame stamps (§2.2), so the replay clock — and therefore the
   monitor grid — advances past them only when a later frame or the end
   stamp arrives. A freshness transition live observed inside a gap with
   NO interleaving frames can shift to the next frame's tick, bounded by
   the inter-frame gap. Real feeds tick constantly; this corner matters
   mainly for synthetic streams.
3. **Stall behavior.** A live process stalled across sweep boundaries
   skips the missed boundaries (`_sampler()` advances past them); replay
   fires every boundary events span.
4. **Shutdown µs.** Live flushes open opportunities microseconds before
   the `recording_end` stamp replay flushes them at; `closed_at` strings
   can differ at millisecond rounding within that window.

## 6. §35 coverage — what is and is not recorded

| §35 item | Status |
|---|---|
| Book snapshots, book deltas | **Recorded** (`snapshot`/`delta` events, literal levels) |
| Network receive timestamps | **Recorded** (`wall_ns`/`mono_ns` on every data frame) |
| Exchange status transitions (as observed locally) | **Partially recorded**: sync/unsync/clear capture local book usability; venue status-feed messages are not subscribed to |
| Best-bid-offer updates | **NOT RECORDED** — no BBO channels are subscribed (feeds are full-depth L2 only); SPECIFIED — Phase 1 extension if BBO channels are ever used |
| Public trades | **NOT RECORDED** — no trade channels are subscribed; SPECIFIED — needed before fill-probability work (Phase 2) |
| Opportunity decisions | Recorded as detector **output** in the research log (`data/opportunities-*.jsonl`) and reproduced by replay from book events; not a replay input |
| Order submissions, acknowledgements, fills, errors | **SPECIFIED — NOT IMPLEMENTED.** No order code exists (`arb/execution/` empty); journaling these is a Phase 2 (paper) / Phase 3+ (shadow/live) requirement |

Replay currently answers §35's opportunity-lifetime, depth-assumption,
fee-sensitivity, stablecoin-basis (via instrument registry asset identity)
and strategy-threshold questions. Capture-rate, order-latency, fill-mismatch
and hedge-loss replay require execution data that does not yet exist.
