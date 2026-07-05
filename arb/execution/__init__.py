"""Execution — INTENTIONALLY EMPTY in Phase 1.

This build contains no order-placement code anywhere: nothing constructs,
signs, or sends an order request, on purpose. Phase 2 adds a paper-fill
order manager here (IOC/FOK limit orders on both legs, partial-fill and
one-leg-filled hedge-or-unwind handling, idempotent client order IDs), and
Phase 3 gates live flow behind ``--live`` AND ``ARB_CONFIRM_LIVE_TRADING``.
"""
