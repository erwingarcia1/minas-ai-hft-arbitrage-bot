"""Monitoring: structured JSON logging, Prometheus metrics, terminal dashboard.

Failure modes:
- Metrics server failing to bind is fatal at boot (config error).
- The dashboard is cosmetic: any exception in it is logged and the dashboard
  stops, but feeds/signals/logging continue unaffected.
"""

from arb.monitoring.metrics import Metrics

__all__ = ["Metrics"]
