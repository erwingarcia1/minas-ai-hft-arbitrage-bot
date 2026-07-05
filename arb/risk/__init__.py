"""Risk controls.

Phase 1 scope: the staleness monitor only — there are no positions and no
orders yet. The monitor is deliberately built as the seam where the Phase 2
kill switch attaches: it already detects the conditions (stale data, dead
feeds) that must halt trading, and exposes a subscription hook for it.

Failure modes: see ``arb.risk.staleness``.
"""

from arb.risk.staleness import StalenessMonitor

__all__ = ["StalenessMonitor"]
