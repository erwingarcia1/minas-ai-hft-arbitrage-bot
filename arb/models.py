"""Domain and configuration models (pydantic v2).

Failure modes:
- A malformed YAML config raises ``pydantic.ValidationError`` at boot; the
  process refuses to start rather than run with silently-defaulted values.
- Unknown keys anywhere in the config are rejected (``extra="forbid"``) so a
  typo like ``min_net_edge_bp`` cannot silently disable a safety threshold.
- Fewer than two enabled venues is a boot error — there is nothing to arb.
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SYMBOL_RE = re.compile(r"^[A-Z0-9]+/[A-Z0-9]+$")


class VenueName(str, Enum):
    """The four supported venues. Values match config keys and log fields."""

    BINANCE = "binance"
    BYBIT = "bybit"
    OKX = "okx"
    BITGET = "bitget"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class FeeTier(StrictModel):
    """Per-venue fee schedule in basis points (maker may be negative: rebate)."""

    maker_bps: float = Field(ge=-10.0, le=100.0)
    taker_bps: float = Field(ge=0.0, le=100.0)


class VenueSettings(StrictModel):
    enabled: bool = True
    fees: FeeTier


class SignalSettings(StrictModel):
    min_net_edge_bps: float = Field(gt=0.0, le=1000.0)
    clip_sizes_usdt: list[float] = Field(min_length=1)

    @field_validator("clip_sizes_usdt")
    @classmethod
    def _clips_positive_sorted(cls, v: list[float]) -> list[float]:
        if any(c <= 0 for c in v):
            raise ValueError("clip sizes must be positive")
        return sorted(set(v))


class RiskSettings(StrictModel):
    """Risk limits. In Phase 1 only ``staleness_ms`` is active (there are no
    positions); position/loss limits are validated now so the config is
    complete before Phase 2 turns them into kill-switch triggers."""

    staleness_ms: int = Field(default=500, gt=0, le=10_000)
    max_position_usdt: float = Field(gt=0.0)
    max_daily_loss_usdt: float = Field(gt=0.0)


class MonitoringSettings(StrictModel):
    metrics_port: int = Field(default=9109, ge=1024, le=65535)
    dashboard: bool = True
    # Upper-bounded: this interval is also the slowest backstop that closes
    # opportunity records when every faster signal path is quiet.
    spread_sample_interval_s: float = Field(default=30.0, ge=1.0, le=300.0)
    log_dir: Path = Path("logs")


class Settings(StrictModel):
    """Root configuration. Loaded from a single YAML file — see config/loader."""

    symbols: list[str] = Field(min_length=1)
    venues: dict[VenueName, VenueSettings]
    signals: SignalSettings
    risk: RiskSettings
    monitoring: MonitoringSettings
    data_dir: Path = Path("data")

    @field_validator("symbols")
    @classmethod
    def _symbols_canonical(cls, v: list[str]) -> list[str]:
        for s in v:
            if not _SYMBOL_RE.match(s):
                raise ValueError(f"symbol {s!r} must look like 'BTC/USDT'")
        if len(set(v)) != len(v):
            raise ValueError("duplicate symbols in config")
        return v

    @model_validator(mode="after")
    def _need_two_venues(self) -> "Settings":
        if len(self.enabled_venues) < 2:
            raise ValueError("at least two venues must be enabled to arbitrage")
        return self

    @property
    def enabled_venues(self) -> list[VenueName]:
        return [v for v, cfg in self.venues.items() if cfg.enabled]

    def taker_bps(self, venue: VenueName) -> float:
        return self.venues[venue].fees.taker_bps


# ---------------------------------------------------------------------------
# Research records (written to JSONL by accounting.opportunities)
# ---------------------------------------------------------------------------


class OpportunityRecord(StrictModel):
    """One theoretical opportunity: opened when net edge crossed the threshold,
    closed when it fell back below (or a book became unusable)."""

    type: Literal["opportunity"] = "opportunity"
    schema_version: int = 1
    symbol: str
    buy_venue: VenueName
    sell_venue: VenueName
    clip_usdt: float
    qty_base: float
    opened_at: str  # ISO-8601 UTC
    closed_at: str
    duration_ms: float
    gross_bps_open: float
    net_bps_open: float
    max_net_bps: float
    mean_net_bps: float
    samples: int
    buy_vwap_open: float
    sell_vwap_open: float
    est_profit_usdt_open: float
    threshold_bps: float
    close_reason: str


class SpreadSample(StrictModel):
    """Periodic unconditional sample of the net spread for one pair/direction/
    clip — including negative spreads. This is the research dataset for
    'does positive edge exist at my fee tier at all'."""

    type: Literal["spread_sample"] = "spread_sample"
    schema_version: int = 1
    ts: str  # ISO-8601 UTC
    symbol: str
    buy_venue: VenueName
    sell_venue: VenueName
    clip_usdt: float
    qty_base: float
    buy_vwap: float
    sell_vwap: float
    gross_bps: float
    net_bps: float
    buy_book_age_ms: float
    sell_book_age_ms: float
