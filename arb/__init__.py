"""Cross-exchange crypto arbitrage system.

Phase 1: market data + spread observer. This package intentionally contains
NO order-placement code — ``arb.execution`` is an empty seam reserved for
Phase 2 (paper) and Phase 3 (live, flag-gated).

Architecture (single-process asyncio, v1):

    feeds/       native WebSocket L2 book maintenance per venue
    signals/     fee/slippage-aware executable spread detection
    risk/        staleness monitoring (kill-switch seam for Phase 2)
    accounting/  JSONL opportunity + spread-sample research log
    monitoring/  structured JSON logs, Prometheus metrics, terminal dashboard
    config/      YAML -> validated pydantic settings

REGION-SPLITTING SEAM (Phase 4): everything upstream of the signal callback
(feeds -> books) is venue-local and would move to per-region processes
(AWS Tokyo/Singapore/Hong Kong); the callback boundary in ``arb.__main__``
is where an inter-region bus would replace in-process function calls.
"""

__version__ = "0.1.0"
