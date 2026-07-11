"""Replay: determinism and live-equivalence.

The two guarantees under test (CLAUDE.md §35, §48.8):

1. DETERMINISM — replaying the same journal with the same config twice
   yields byte-identical records.
2. COMPLETENESS/EQUIVALENCE — a "live" session driven exactly the way the
   feeds and periodic tasks drive the system (apply -> integrity gate ->
   synced -> journaled eval -> evaluate; StalenessMonitor ticks on the 100ms
   grid; sample sweeps on the interval grid), recorded through the tap, then
   replayed, yields IDENTICAL records. This is what proves the journal
   captures everything the detector's decisions depend on — including that
   replay does NOT evaluate state live rejected on integrity grounds.

Both sides run controlled clocks derived from the same event stamps; in
production the residual live-vs-replay skew is scheduling lateness of the
periodic tasks and the µs between a frame's receive stamp and wall-clock
reads (documented in docs/MARKET_REPLAY.md §5).

Synthetic stamps are placed FAR IN THE FUTURE of the test machine's real
clocks on BOTH domains so nothing real-stamped can drag the replay clock
ahead of the synthetic timeline.
"""

from __future__ import annotations

import time

import orjson
import pytest

from arb.feeds.book import BookRegistry
from arb.models import VenueName
from arb.recording.recorder import BookEventRecorder
from arb.replay.clock import ReplayClock
from arb.replay.engine import ReplayError, run_replay
from arb.risk.staleness import TICK_S, StalenessMonitor
from arb.signals.spread import SpreadDetector
from tests.conftest import ListSink, make_metrics, make_settings

BINANCE = VenueName.BINANCE
OKX = VenueName.OKX

MS = 10**6  # ns
TICK_NS = int(TICK_S * 1e9)


class TestReplayClock:
    def test_advance_is_monotonic(self) -> None:
        clock = ReplayClock()
        clock.advance(100, 200)
        clock.advance(50, 100)  # corrupt/backward stamps clamp, never rewind
        assert clock.mono_ns() == 100
        assert clock.wall_ns() == 200

    def test_reset_rebases_both_domains(self) -> None:
        clock = ReplayClock(1000, 2000)
        clock.reset(10, 20)
        assert clock.mono_ns() == 10
        assert clock.wall_ns() == 20

    def test_now_utc_derives_from_wall(self) -> None:
        clock = ReplayClock(0, 1_700_000_000_000_000_000)
        assert clock.now_utc().year == 2023


class _Session:
    """Drives books exactly the way the live system does — feed-style
    mutations with integrity-gated sync/eval, the REAL StalenessMonitor
    ticked on its 100ms grid, sample sweeps on the interval grid — with the
    recorder attached and a controlled clock, collecting live detector
    output for comparison against replay."""

    def __init__(self, tmp_path, settings) -> None:
        self.settings = settings
        self.metrics = make_metrics()
        self.registry = BookRegistry(settings.enabled_venues, settings.symbols)
        # Synthetic timeline strictly ahead of the machine's real clocks.
        self.mono0 = time.monotonic_ns() + 10**12
        self.wall0 = time.time_ns() + 10**12
        self.clock = ReplayClock(self.mono0, self.wall0)
        self.recorder = BookEventRecorder(
            tmp_path, settings, self.metrics, wall_ns=self.wall0, mono_ns=self.mono0
        )
        self.recorder.attach(self.registry)
        self.sink = ListSink()
        self.detector = SpreadDetector(
            self.registry, settings, self.sink, self.metrics, clock=self.clock
        )
        self.monitor = StalenessMonitor(
            self.registry,
            settings,
            self.metrics,
            self.detector.on_venue_unusable,
            on_symbol_degraded=self.detector.evaluate,
        )
        self._interval_ns = int(settings.monitoring.spread_sample_interval_s * 1e9)
        self._next_sample = self.mono0 + self._interval_ns
        self._next_tick = self.mono0 + TICK_NS

    # -- periodic tasks on their grids (the replay engine's convention:
    # chronological, sample first on an exact tie) -------------------------

    def _run_periodic(self, upto: int) -> None:
        while True:
            candidates = []
            if self._next_tick <= upto:
                candidates.append((self._next_tick, "tick"))
            if self._next_sample <= upto:
                candidates.append((self._next_sample, "sample"))
            if not candidates:
                return
            due, kind = min(candidates)
            delta = due - self.clock.mono_ns()
            if delta > 0:
                self.clock.advance(due, self.clock.wall_ns() + delta)
            if kind == "tick":
                self.monitor.tick(due)
                self._next_tick = due + TICK_NS
            else:
                for symbol in self.settings.symbols:
                    self.detector.evaluate(symbol, emit_sample=True)
                self._next_sample = due + self._interval_ns

    def at(self, offset_ms: float) -> tuple[int, int]:
        mono = self.mono0 + int(offset_ms * MS)
        wall = self.wall0 + int(offset_ms * MS)
        self._run_periodic(mono)
        self.clock.advance(mono, wall)
        return mono, wall

    # -- feed-style drivers ----------------------------------------------------

    def _notify(self, venue, symbol="BTC/USDT") -> None:
        """The production on_book_update composite: journal the evaluation
        point, then evaluate (arb/__main__.py wiring)."""
        self.recorder.on_evaluation(venue, symbol)
        self.detector.evaluate(symbol)

    def snapshot(self, venue, offset_ms, bids, asks, symbol="BTC/USDT") -> None:
        mono, wall = self.at(offset_ms)
        book = self.registry.get(venue, symbol)
        book.apply_snapshot(bids, asks, exchange_ts_ms=None, mono_ns=mono, wall_ns=wall)
        book.synced = True  # integrity checks passed (feed order)
        self._notify(venue, symbol)

    def delta(self, venue, offset_ms, bids, asks, symbol="BTC/USDT") -> None:
        mono, wall = self.at(offset_ms)
        book = self.registry.get(venue, symbol)
        book.apply_deltas(bids, asks, exchange_ts_ms=None, mono_ns=mono, wall_ns=wall)
        self._notify(venue, symbol)

    def corrupt_delta(self, venue, offset_ms, bids, asks, symbol="BTC/USDT") -> None:
        """The checksum-failure path (okx/bitget): the delta was APPLIED,
        then validation failed — the feed unsyncs and resubscribes WITHOUT
        invoking the update callback. Live never evaluates this state."""
        mono, wall = self.at(offset_ms)
        book = self.registry.get(venue, symbol)
        book.apply_deltas(bids, asks, exchange_ts_ms=None, mono_ns=mono, wall_ns=wall)
        book.synced = False
        # deliberately NO _notify: that is the point.

    def unsync(self, venue, symbol="BTC/USDT") -> None:
        """Feed marks a book untrusted in control flow (no data frame, no
        evaluation): closes happen at the next monitor tick, as live."""
        self.registry.get(venue, symbol).synced = False

    def finish(self, offset_ms) -> None:
        """Production shutdown ordering (arb/__main__.py finally): detach the
        recorder, feed teardown unsyncs books (unjournaled, no evaluation),
        flush open opportunities, stamp recording_end."""
        mono, wall = self.at(offset_ms)
        self.recorder.detach(self.registry)
        for book in self.registry.all_books():
            book.synced = False
        self.detector.close_all("shutdown")
        self.recorder.close(wall_ns=wall, mono_ns=mono)


def drive_scripted_session(tmp_path, settings) -> _Session:
    """Open (okx cheap ask, binance rich bid), decay below threshold, lose a
    feed (closes at the next monitor tick), recover, and leave one
    opportunity open until shutdown."""
    s = _Session(tmp_path, settings)
    # Fees are 10bps each leg; gross ~30bps -> net ~+10bps >= 3 => OPEN.
    s.snapshot(OKX, 0, bids=[("9990", "5")], asks=[("10000", "5")])
    s.snapshot(BINANCE, 1, bids=[("10030", "5")], asks=[("10040", "5")])
    # Edge decays: binance bid falls to parity -> net negative => CLOSE.
    s.delta(BINANCE, 120, bids=[("10030", "0"), ("10005", "5")], asks=[])
    # Reopens...
    s.delta(BINANCE, 240, bids=[("10035", "5")], asks=[])
    # ...the sell-side feed goes untrusted in control flow: the monitor's
    # 300ms tick closes it (book_unusable via the per-book backstop).
    s.unsync(OKX)
    # Recovers and reopens; left open at shutdown => CLOSE (shutdown).
    s.snapshot(OKX, 380, bids=[("9990", "5")], asks=[("10000", "5")])
    s.finish(450)
    return s


class TestLiveReplayEquivalence:
    def test_replay_reproduces_live_records_exactly(self, tmp_path) -> None:
        settings = make_settings()
        session = drive_scripted_session(tmp_path, settings)
        live_records = session.sink.records
        # Sanity on the script: open->close cycles with the expected reasons.
        reasons = [r["close_reason"] for r in live_records if r["type"] == "opportunity"]
        assert reasons == ["edge_below_threshold", "book_unusable", "shutdown"]

        replay_sink = ListSink()
        stats = run_replay(settings, [session.recorder.path], replay_sink)
        assert stats.headers == 1
        assert stats.evals > 0
        assert replay_sink.records == live_records
        assert [orjson.dumps(r) for r in replay_sink.records] == [
            orjson.dumps(r) for r in live_records
        ]

    def test_integrity_rejected_state_is_never_priced(self, tmp_path) -> None:
        """A delta that fails its post-apply checksum is journaled (the
        mutation happened) but live never evaluates it. Replay must not
        either: a phantom evaluation would print an arbitrarily large fake
        edge into the research log."""
        settings = make_settings()
        s = _Session(tmp_path, settings)
        s.snapshot(OKX, 0, bids=[("9990", "5")], asks=[("10000", "5")])
        s.snapshot(BINANCE, 1, bids=[("10030", "5")], asks=[("10040", "5")])
        # (Monitor tick at +100ms observes both books fresh.)
        # Corrupt frame on binance at +150ms: a bid at 11000 WOULD price a
        # ~10% fake edge if anything evaluated it. The feed rejects and
        # unsyncs without invoking the update callback.
        s.corrupt_delta(BINANCE, 150, bids=[("11000", "5")], asks=[])
        s.finish(450)

        live_records = s.sink.records
        replay_sink = ListSink()
        run_replay(settings, [s.recorder.path], replay_sink)
        assert replay_sink.records == live_records
        # The open (offset 1) closes at the +200ms monitor tick, and no
        # record anywhere reflects the corrupt 11000 bid.
        opps = [r for r in replay_sink.records if r["type"] == "opportunity"]
        assert [r["close_reason"] for r in opps] == ["book_unusable"]
        assert all(r["max_net_bps"] < 50 for r in opps)
        assert opps[0]["duration_ms"] == pytest.approx(199.0)

    def test_replay_is_deterministic_across_runs(self, tmp_path) -> None:
        settings = make_settings()
        session = drive_scripted_session(tmp_path, settings)
        sink_a, sink_b = ListSink(), ListSink()
        run_replay(settings, [session.recorder.path], sink_a)
        run_replay(settings, [session.recorder.path], sink_b)
        assert sink_a.records == sink_b.records
        assert len(sink_a.records) > 0

    def test_durations_come_from_recorded_clock(self, tmp_path) -> None:
        from datetime import datetime, timezone

        settings = make_settings()
        session = drive_scripted_session(tmp_path, settings)
        replay_sink = ListSink()
        run_replay(settings, [session.recorder.path], replay_sink)
        first = replay_sink.of_type("opportunity")[0]
        # Opened at offset 1ms (binance snapshot), closed at 120ms — stamps
        # derived from the RECORDED timeline, not the replaying machine.
        assert first["duration_ms"] == pytest.approx(119.0)
        expected_open = datetime.fromtimestamp(
            (session.wall0 + 1 * MS) / 1e9, tz=timezone.utc
        ).isoformat(timespec="milliseconds")
        assert first["opened_at"] == expected_open

    def test_monitor_grid_closes_venue_wide(self, tmp_path) -> None:
        """The REAL StalenessMonitor runs inside replay: one venue book going
        untrusted closes that symbol's opportunities via the per-book
        backstop (book_unusable) AND — because venue usability requires ALL
        of the venue's books — closes the venue's other symbols'
        opportunities with live's venue_unusable reason, even though their
        books are still fresh."""
        settings = make_settings(symbols=["BTC/USDT", "ETH/USDT"])
        s = _Session(tmp_path, settings)
        # BTC: ~30bps gross -> opportunity opens (buy okx, sell binance).
        s.snapshot(OKX, 0, bids=[("9990", "5")], asks=[("10000", "5")])
        s.snapshot(BINANCE, 1, bids=[("10030", "5")], asks=[("10040", "5")])
        # ETH: ~52bps gross -> opportunity opens too.
        s.snapshot(OKX, 2, bids=[("3599", "50")], asks=[("3601", "50")], symbol="ETH/USDT")
        s.snapshot(BINANCE, 3, bids=[("3620", "50")], asks=[("3622", "50")], symbol="ETH/USDT")
        # ETH keeps ticking (fresh books, interleaving frames advance the
        # replay grids exactly as live feeds do)...
        s.delta(OKX, 160, bids=[], asks=[("3601", "49")], symbol="ETH/USDT")
        s.delta(BINANCE, 161, bids=[("3620", "49")], asks=[], symbol="ETH/USDT")
        # ...then okx's BTC book goes untrusted in control flow. No BTC frame
        # follows: only the monitor can close.
        s.unsync(OKX, "BTC/USDT")
        s.finish(400)

        replay_sink = ListSink()
        stats = run_replay(settings, [s.recorder.path], replay_sink)
        assert stats.monitor_ticks >= 3
        opps = replay_sink.of_type("opportunity")
        by_symbol = {r["symbol"]: r for r in opps}
        # +200ms tick: per-book backstop closes BTC (its okx book untrusted)...
        assert by_symbol["BTC/USDT"]["close_reason"] == "book_unusable"
        assert by_symbol["BTC/USDT"]["duration_ms"] == pytest.approx(199.0)
        # ...and the venue transition closes ETH with live's venue-level
        # reason although ETH's okx book is synced and fresh.
        assert by_symbol["ETH/USDT"]["close_reason"] == "venue_unusable"
        assert by_symbol["ETH/USDT"]["duration_ms"] == pytest.approx(197.0)
        assert replay_sink.records == s.sink.records


class TestReplayMechanics:
    def test_sample_sweep_at_recorded_cadence(self, tmp_path) -> None:
        settings = make_settings(spread_sample_interval_s=1.0)
        s = _Session(tmp_path, settings)
        s.snapshot(OKX, 0, bids=[("9990", "5")], asks=[("10000", "5")])
        s.snapshot(BINANCE, 1, bids=[("10030", "5")], asks=[("10040", "5")])
        # BOTH books tick every 400ms so both stay fresh (<500ms staleness)
        # across the sample boundaries.
        for i, off in enumerate((400, 800, 1200, 1600, 2000, 2400)):
            s.delta(BINANCE, off, bids=[(f"1003{i % 2}", "5")], asks=[])
            s.delta(OKX, off + 1, bids=[], asks=[(f"1000{i % 2}", "5")])
        s.finish(2500)

        sink = ListSink()
        stats = run_replay(settings, [s.recorder.path], sink)
        assert stats.samples_swept == 2  # boundaries at +1.0s and +2.0s
        samples = sink.of_type("spread_sample")
        # Two directions per boundary (buy-on-okx/sell-on-binance and reverse).
        assert len(samples) == 4
        assert sink.records == s.sink.records

    def test_events_outside_config_are_skipped(self, tmp_path) -> None:
        settings = make_settings()
        session = drive_scripted_session(tmp_path, settings)
        with session.recorder.path.open("ab") as fh:
            fh.write(
                orjson.dumps(
                    {
                        "type": "book_event", "v": "bybit", "s": "BTC/USDT", "k": "sync",
                        "ets": None, "mono_ns": session.mono0, "wall_ns": session.wall0,
                        "b": [], "a": [],
                    }
                )
                + b"\n"
            )
        sink = ListSink()
        stats = run_replay(settings, [session.recorder.path], sink)
        assert stats.events_skipped == 1

    def test_config_divergence_warns(self, tmp_path) -> None:
        settings = make_settings()
        session = drive_scripted_session(tmp_path, settings)
        hotter = make_settings(min_net_edge_bps=50.0)
        sink = ListSink()
        stats = run_replay(hotter, [session.recorder.path], sink)
        assert any("sensitivity" in w for w in stats.warnings)
        # At a 50bps threshold nothing opens: sensitivity analysis works.
        assert sink.of_type("opportunity") == []

    def test_instrument_divergence_warns(self, tmp_path) -> None:
        from pathlib import Path

        from arb.instruments.registry import InstrumentRegistry

        settings = make_settings()
        session = drive_scripted_session(tmp_path, settings)  # recorded WITHOUT instruments
        sink = ListSink()
        stats = run_replay(
            settings,
            [session.recorder.path],
            sink,
            instruments=InstrumentRegistry.load(Path("config/instruments.yaml")),
        )
        assert any("instrument metadata" in w for w in stats.warnings)

    def test_truncated_recording_flushes_open_opportunities(self, tmp_path) -> None:
        settings = make_settings()
        session = drive_scripted_session(tmp_path, settings)
        lines = session.recorder.path.read_bytes().splitlines()
        assert orjson.loads(lines[-1])["type"] == "recording_end"
        truncated = tmp_path / "truncated.jsonl"
        truncated.write_bytes(b"\n".join(lines[:-1]) + b"\n")  # crash: no end stamp
        sink = ListSink()
        run_replay(settings, [truncated], sink)
        reasons = [r["close_reason"] for r in sink.of_type("opportunity")]
        assert reasons[-1] == "shutdown"  # flushed at last event's clock

    def test_malformed_line_is_a_loud_error(self, tmp_path) -> None:
        bad = tmp_path / "bad.jsonl"
        bad.write_bytes(b'{"type": "book_event", "v": "binance"\n')
        with pytest.raises(ReplayError, match="bad.jsonl:1"):
            run_replay(make_settings(), [bad], ListSink())

    def test_sink_failure_is_not_blamed_on_the_journal(self, tmp_path) -> None:
        settings = make_settings()
        session = drive_scripted_session(tmp_path, settings)

        class ExplodingSink:
            def write(self, record: dict) -> None:
                raise OSError("output disk full")

        with pytest.raises(ReplayError, match="output sink failed"):
            run_replay(settings, [session.recorder.path], ExplodingSink())

    def test_two_sessions_replay_as_restart(self, tmp_path) -> None:
        settings = make_settings()
        s1 = drive_scripted_session(tmp_path / "a", settings)
        s2 = drive_scripted_session(tmp_path / "b", settings)
        sink = ListSink()
        stats = run_replay(settings, [s1.recorder.path, s2.recorder.path], sink)
        assert stats.headers == 2
        reasons = [r["close_reason"] for r in sink.of_type("opportunity")]
        assert reasons == ["edge_below_threshold", "book_unusable", "shutdown"] * 2
