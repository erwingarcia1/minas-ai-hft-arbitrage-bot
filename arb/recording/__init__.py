"""Deterministic market-data recording (CLAUDE.md §35).

Journals every applied order-book state change as normalized JSONL events so
sessions can be replayed exactly by arb/replay — no venue protocol logic is
needed at replay time.
"""

from arb.recording.recorder import BookEventRecorder

__all__ = ["BookEventRecorder"]
