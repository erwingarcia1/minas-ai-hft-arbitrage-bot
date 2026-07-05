"""Fee-aware edge math. Pure functions; unit-tested exactly.

Conventions:
- Fees are expressed in basis points of notional and modelled in quote terms.
  (Some venues charge spot buy-side fees in the base asset; expressing both
  legs in quote terms is equivalent for edge purposes at these sizes and keeps
  the math venue-uniform. Documented so nobody mistakes it for an omission.)
- All edges are TAKER edges: this strategy crosses the spread on both legs.

Failure modes: none at runtime — inputs are validated upstream; a
non-positive VWAP is a programming error and raises immediately.
"""

from __future__ import annotations

BPS = 1e4


def gross_edge_bps(buy_vwap: float, sell_vwap: float) -> float:
    """Edge before fees, in bps of the buy price: (sell/buy - 1) * 1e4."""
    if buy_vwap <= 0 or sell_vwap <= 0:
        raise ValueError(f"VWAPs must be positive (buy={buy_vwap}, sell={sell_vwap})")
    return (sell_vwap / buy_vwap - 1.0) * BPS


def net_edge_bps(
    buy_vwap: float,
    sell_vwap: float,
    buy_taker_bps: float,
    sell_taker_bps: float,
) -> float:
    """Edge net of both taker fees, in bps.

    Buying q base costs  q * buy_vwap  * (1 + f_buy);
    selling q base yields q * sell_vwap * (1 - f_sell).
    Net edge = proceeds / cost - 1, expressed in bps. Exact, not additive
    approximation — at 10bps fees the difference is small but this is the
    number capital decisions rest on.
    """
    if buy_vwap <= 0 or sell_vwap <= 0:
        raise ValueError(f"VWAPs must be positive (buy={buy_vwap}, sell={sell_vwap})")
    f_buy = buy_taker_bps / BPS
    f_sell = sell_taker_bps / BPS
    cost = buy_vwap * (1.0 + f_buy)
    proceeds = sell_vwap * (1.0 - f_sell)
    return (proceeds / cost - 1.0) * BPS
