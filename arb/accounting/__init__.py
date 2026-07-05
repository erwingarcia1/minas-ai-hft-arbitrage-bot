"""Accounting.

Phase 1 scope: the research log only — opportunity records and periodic
spread samples as JSONL. Fills, PnL, and balance reconciliation arrive in
Phase 2 when (paper) fills exist.

Failure modes: see ``arb.accounting.opportunities``.
"""

from arb.accounting.opportunities import OpportunityLog

__all__ = ["OpportunityLog"]
