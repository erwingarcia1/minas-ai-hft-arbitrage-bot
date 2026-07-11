"""Canonical instrument registry: per-venue precision and minimum metadata.

The detector prices one base quantity on two venues; that quantity must be a
multiple of BOTH venues' quantity steps and satisfy both venues' minimums or
the clip is not executable at all. ``round_qty_for_pair`` implements exactly
that: floor to the least common multiple of the two steps (never round up —
rounding up can exceed depth/inventory, CLAUDE.md §16), then check minimum
quantities. Minimum notionals are checked by the caller once VWAPs are known.

Asset identity (§10/§27): canonical asset codes are compared by exact string
equality and nothing else. There is no conversion table that could quietly
declare USDT == USDC; ``assets_equivalent`` exists to make that rule an
explicit, testable function instead of an implicit assumption.

Failure modes:
- A malformed instruments file raises ``InstrumentsError`` at boot (same
  philosophy as config loading: refuse to start, never default around).
- An exchange_symbol that does not match the venue's canonical->venue symbol
  transform is rejected at load: a typo here would silently price venue A's
  book against venue B's metadata.
- Missing metadata for a (venue, symbol) means "no rounding possible": the
  registry returns None and the detector prices unrounded, WARNING once at
  boot. Suspended instruments are excluded from evaluation entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from math import gcd
from pathlib import Path
from typing import Iterable, Literal

import yaml
from pydantic import Field, ValidationError, field_validator

from arb.models import StrictModel, VenueName

SCHEMA_VERSION = 1


class InstrumentsError(RuntimeError):
    """Raised for any instruments-file problem; fatal at boot by design."""


# ---------------------------------------------------------------------------
# Asset registry (§10): canonical identity, never equivalence-by-similarity.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssetInfo:
    code: str
    kind: Literal["crypto", "stablecoin"]
    issuer: str | None = None
    note: str = ""


ASSET_REGISTRY: dict[str, AssetInfo] = {
    "BTC": AssetInfo("BTC", "crypto"),
    "ETH": AssetInfo("ETH", "crypto"),
    "SOL": AssetInfo("SOL", "crypto"),
    "USDT": AssetInfo(
        "USDT", "stablecoin", issuer="Tether", note="not interchangeable with USDC (§27)"
    ),
    "USDC": AssetInfo(
        "USDC", "stablecoin", issuer="Circle", note="not interchangeable with USDT (§27)"
    ),
}


def assets_equivalent(a: str, b: str) -> bool:
    """Two assets are economically equivalent iff they are the same canonical
    code. Deliberately trivial and deliberately a function: the invariant
    "stablecoins are not automatically valued equally" (§40.23) hangs on
    nobody ever 'improving' this with a similarity table."""
    return a == b


# ---------------------------------------------------------------------------
# Instrument metadata
# ---------------------------------------------------------------------------

# Canonical "BASE/QUOTE" -> venue-native symbol, per venue. Must agree with the
# transforms the feeds use (tested in tests/test_instruments.py).
def venue_symbol_for(venue: VenueName, symbol: str) -> str:
    base, quote = symbol.split("/", 1)
    if venue is VenueName.OKX:
        return f"{base}-{quote}"
    return f"{base}{quote}"


class InstrumentMeta(StrictModel):
    venue: VenueName
    symbol: str  # canonical "BASE/QUOTE"
    exchange_symbol: str
    status: Literal["trading", "suspended", "unknown"] = "trading"
    price_tick: Decimal = Field(gt=0)
    qty_step: Decimal = Field(gt=0)
    min_qty: Decimal = Field(ge=0)
    min_notional: Decimal = Field(ge=0)  # in quote units
    max_qty: Decimal | None = Field(default=None, gt=0)
    max_notional: Decimal | None = Field(default=None, gt=0)
    source: str
    fetched_at: str | None = None

    @field_validator("symbol")
    @classmethod
    def _canonical(cls, v: str) -> str:
        if "/" not in v:
            raise ValueError(f"symbol {v!r} must be canonical 'BASE/QUOTE'")
        return v

    @property
    def canonical_base(self) -> str:
        return self.symbol.split("/", 1)[0]

    @property
    def canonical_quote(self) -> str:
        return self.symbol.split("/", 1)[1]


def _decimal_from_fraction(f: Fraction) -> Decimal:
    # Steps are decimal numbers, so denominators are 2^a * 5^b and this
    # division is exact.
    return Decimal(f.numerator) / Decimal(f.denominator)


def common_qty_step(a: Decimal, b: Decimal) -> Decimal:
    """Smallest quantity increment valid on BOTH venues: lcm of the steps
    (exact, via Fractions — correct even for non-nested steps like
    0.002 vs 0.005 -> 0.01)."""
    fa, fb = Fraction(a), Fraction(b)
    num = fa.numerator * fb.numerator // gcd(fa.numerator, fb.numerator)
    den = gcd(fa.denominator, fb.denominator)
    return _decimal_from_fraction(Fraction(num, den))


def round_qty_for_pair(
    qty: float, buy: InstrumentMeta | None, sell: InstrumentMeta | None
) -> float | None:
    """Floor ``qty`` (base units) to a quantity executable on both venues.

    Returns None when the rounded quantity would violate either venue's
    minimum quantity (the clip is unexecutable — callers must not shrink the
    check away, §16). With no metadata at all, returns qty unchanged: the
    caller decided at boot that unrounded pricing is acceptable and warned.
    """
    if qty <= 0:
        return None
    metas = [m for m in (buy, sell) if m is not None]
    if not metas:
        return qty
    steps = [m.qty_step for m in metas]
    step = steps[0] if len(steps) == 1 else common_qty_step(steps[0], steps[1])
    try:
        qty_d = Decimal(str(qty))
    except InvalidOperation:  # pragma: no cover - str(float) is always valid
        return None
    # Clamp to the tightest maximum FIRST, then floor, then check minimums:
    # a max-clamp that lands below the other venue's minimum must yield
    # "unexecutable", never a quantity that violates a checked constraint.
    caps = [m.max_qty for m in metas if m.max_qty is not None]
    if caps:
        qty_d = min(qty_d, min(caps))
    rounded = (qty_d // step) * step
    if rounded <= 0:
        return None
    for m in metas:
        if rounded < m.min_qty:
            return None
    return float(rounded)


class InstrumentRegistry:
    def __init__(self, instruments: Iterable[InstrumentMeta] = ()) -> None:
        self._by_key: dict[tuple[VenueName, str], InstrumentMeta] = {}
        for meta in instruments:
            key = (meta.venue, meta.symbol)
            if key in self._by_key:
                raise InstrumentsError(
                    f"duplicate instrument for {meta.venue.value} {meta.symbol}"
                )
            expected = venue_symbol_for(meta.venue, meta.symbol)
            if meta.exchange_symbol != expected:
                raise InstrumentsError(
                    f"{meta.venue.value} {meta.symbol}: exchange_symbol "
                    f"{meta.exchange_symbol!r} does not match the venue transform "
                    f"({expected!r}) — refusing metadata that maps to a different market"
                )
            self._by_key[key] = meta

    @classmethod
    def empty(cls) -> "InstrumentRegistry":
        return cls(())

    def get(self, venue: VenueName, symbol: str) -> InstrumentMeta | None:
        return self._by_key.get((venue, symbol))

    def __len__(self) -> int:
        return len(self._by_key)

    def all(self) -> list[InstrumentMeta]:
        return list(self._by_key.values())

    @classmethod
    def load(cls, path: Path) -> "InstrumentRegistry":
        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise InstrumentsError(
                f"cannot read instruments file {path}: {exc} "
                f"(set instruments_file: null to disable rounding explicitly)"
            ) from exc
        try:
            raw = yaml.safe_load(raw_text)
        except yaml.YAMLError as exc:
            raise InstrumentsError(f"invalid YAML in {path}: {exc}") from exc
        if not isinstance(raw, dict) or "instruments" not in raw:
            raise InstrumentsError(f"{path} must be a mapping with an 'instruments' list")
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise InstrumentsError(
                f"{path}: schema_version {raw.get('schema_version')!r} != {SCHEMA_VERSION}"
            )
        entries = raw["instruments"]
        if not isinstance(entries, list):
            raise InstrumentsError(f"{path}: 'instruments' must be a list")
        metas: list[InstrumentMeta] = []
        for i, entry in enumerate(entries):
            try:
                metas.append(InstrumentMeta.model_validate(entry))
            except ValidationError as exc:
                raise InstrumentsError(f"{path}: instruments[{i}] invalid:\n{exc}") from exc
        return cls(metas)
