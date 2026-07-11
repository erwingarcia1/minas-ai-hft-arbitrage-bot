"""Deterministic historical replay (CLAUDE.md §35, operating Mode 2).

Re-applies recorded book events to fresh books and re-runs the detector under
a replayed clock: the same event stream and configuration produce the same
records, byte for byte. Places no orders (nothing in this repository can).

Import note: only the clock is re-exported here. The engine
(``arb.replay.engine``) imports the detector, which imports this package for
the clock — re-exporting the engine here would create an import cycle.
"""

from arb.replay.clock import Clock, ReplayClock, SystemClock

__all__ = ["Clock", "ReplayClock", "SystemClock"]
