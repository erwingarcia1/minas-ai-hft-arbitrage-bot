"""Clock abstraction so the detector runs identically live and in replay.

Live code uses ``SystemClock`` (the default everywhere). Replay drives a
``ReplayClock`` forward from recorded event stamps, so every duration, book
age, staleness decision and record timestamp is derived from the recording —
never from the machine performing the replay. That is what makes replay
deterministic (CLAUDE.md §35: same events + same config => same result).

Failure modes: ``ReplayClock`` refuses to run backwards via ``advance`` —
out-of-order stamps within a session indicate a corrupt recording and clamp
forward (monotonic reads stay monotonic). Session boundaries, where a new
process's monotonic domain legitimately restarts, must use ``reset``.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def mono_ns(self) -> int: ...

    def wall_ns(self) -> int: ...

    def now_utc(self) -> datetime: ...


class SystemClock:
    """Real time. The live default."""

    def mono_ns(self) -> int:
        return time.monotonic_ns()

    def wall_ns(self) -> int:
        return time.time_ns()

    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)


class ReplayClock:
    """Time as recorded. ``advance`` never moves backwards within a session;
    ``reset`` rebases both domains at a recording/session boundary."""

    def __init__(self, mono_ns: int = 0, wall_ns: int = 0) -> None:
        self._mono_ns = mono_ns
        self._wall_ns = wall_ns

    def advance(self, mono_ns: int, wall_ns: int) -> None:
        if mono_ns > self._mono_ns:
            self._mono_ns = mono_ns
        if wall_ns > self._wall_ns:
            self._wall_ns = wall_ns

    def reset(self, mono_ns: int, wall_ns: int) -> None:
        self._mono_ns = mono_ns
        self._wall_ns = wall_ns

    def mono_ns(self) -> int:
        return self._mono_ns

    def wall_ns(self) -> int:
        return self._wall_ns

    def now_utc(self) -> datetime:
        return datetime.fromtimestamp(self._wall_ns / 1e9, tz=timezone.utc)
