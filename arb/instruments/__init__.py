"""Canonical instrument and asset registry (CLAUDE.md §10).

Maps canonical symbols to per-venue instruments with precision/minimum
metadata, and defines asset identity rules. The central rule: two assets are
economically equivalent only when their canonical codes are identical —
USDT and USDC are distinct assets, always (§27).
"""

from arb.instruments.registry import (
    ASSET_REGISTRY,
    AssetInfo,
    InstrumentMeta,
    InstrumentRegistry,
    assets_equivalent,
    common_qty_step,
    round_qty_for_pair,
)

__all__ = [
    "ASSET_REGISTRY",
    "AssetInfo",
    "InstrumentMeta",
    "InstrumentRegistry",
    "assets_equivalent",
    "common_qty_step",
    "round_qty_for_pair",
]
