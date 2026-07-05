"""Spread detection: fee math, depth-implied slippage, opportunity lifecycle.

Failure modes:
- No signal is ever computed from a book that is unsynced or older than the
  configured staleness limit; a venue with degraded data simply drops out of
  every pair evaluation.
- Insufficient depth for a clip yields "no executable price" (None), never a
  top-of-book approximation.
"""

from arb.signals.spread import SpreadDetector

__all__ = ["SpreadDetector"]
