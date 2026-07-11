"""Refresh instrument metadata from the venues' public REST endpoints.

``python -m arb instruments fetch`` regenerates ``config/instruments.yaml``
from live exchangeInfo-style endpoints (no credentials involved — these are
public). Parsing is split into pure per-venue functions so the payload
formats are pinned by unit tests against recorded fixtures; the network layer
is a thin aiohttp GET.

Failure modes:
- Any venue failing to fetch or parse aborts the whole refresh with the
  original error: a half-updated instruments file mixing fetch dates is worse
  than the old file. Nothing is written until every requested venue parsed.
- A requested symbol missing from a venue's response is a hard error (a
  delisted market must be noticed, not silently dropped from metadata while
  the feed still subscribes to it).
- This development sandbox blocks exchange egress; run the refresh from an
  operator machine. The checked-in file documents its own provenance.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import aiohttp
import yaml

from arb.instruments.registry import SCHEMA_VERSION, InstrumentMeta, venue_symbol_for
from arb.models import VenueName


class FetchError(RuntimeError):
    """Raised when a venue's instrument metadata cannot be retrieved/parsed."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _wanted(venue: VenueName, symbols: list[str]) -> dict[str, str]:
    """venue-native symbol -> canonical symbol."""
    return {venue_symbol_for(venue, s): s for s in symbols}


# ---------------------------------------------------------------------------
# Pure parsers, one per venue payload format
# ---------------------------------------------------------------------------


def parse_binance(payload: dict[str, Any], symbols: list[str]) -> list[InstrumentMeta]:
    """GET /api/v3/exchangeInfo — filters carry tick/step/minima."""
    wanted = _wanted(VenueName.BINANCE, symbols)
    found: dict[str, InstrumentMeta] = {}
    for sym in payload.get("symbols", []):
        canonical = wanted.get(sym.get("symbol", ""))
        if canonical is None:
            continue
        filters = {f["filterType"]: f for f in sym.get("filters", [])}
        try:
            price_filter = filters["PRICE_FILTER"]
            lot = filters["LOT_SIZE"]
            # NOTIONAL replaced MIN_NOTIONAL in 2023; accept either.
            notional = filters.get("NOTIONAL") or filters["MIN_NOTIONAL"]
            found[canonical] = InstrumentMeta(
                venue=VenueName.BINANCE,
                symbol=canonical,
                exchange_symbol=sym["symbol"],
                status="trading" if sym.get("status") == "TRADING" else "suspended",
                price_tick=Decimal(price_filter["tickSize"]),
                qty_step=Decimal(lot["stepSize"]),
                min_qty=Decimal(lot["minQty"]),
                min_notional=Decimal(notional["minNotional"]),
                source="rest:binance /api/v3/exchangeInfo",
                fetched_at=_now_iso(),
            )
        except (KeyError, ValueError) as exc:
            raise FetchError(f"binance exchangeInfo malformed for {canonical}: {exc!r}") from exc
    _require_all(VenueName.BINANCE, wanted, found)
    return [found[c] for c in sorted(found)]


def parse_bybit(payload: dict[str, Any], symbols: list[str]) -> list[InstrumentMeta]:
    """GET /v5/market/instruments-info?category=spot."""
    wanted = _wanted(VenueName.BYBIT, symbols)
    found: dict[str, InstrumentMeta] = {}
    for item in payload.get("result", {}).get("list", []):
        canonical = wanted.get(item.get("symbol", ""))
        if canonical is None:
            continue
        try:
            lot = item["lotSizeFilter"]
            found[canonical] = InstrumentMeta(
                venue=VenueName.BYBIT,
                symbol=canonical,
                exchange_symbol=item["symbol"],
                status="trading" if item.get("status") == "Trading" else "suspended",
                price_tick=Decimal(item["priceFilter"]["tickSize"]),
                qty_step=Decimal(lot["basePrecision"]),
                min_qty=Decimal(lot["minOrderQty"]),
                min_notional=Decimal(lot["minOrderAmt"]),
                source="rest:bybit /v5/market/instruments-info",
                fetched_at=_now_iso(),
            )
        except (KeyError, ValueError) as exc:
            raise FetchError(f"bybit instruments-info malformed for {canonical}: {exc!r}") from exc
    _require_all(VenueName.BYBIT, wanted, found)
    return [found[c] for c in sorted(found)]


def parse_okx(payload: dict[str, Any], symbols: list[str]) -> list[InstrumentMeta]:
    """GET /api/v5/public/instruments?instType=SPOT. OKX enforces a minimum
    base size (minSz) but no spot minimum notional -> min_notional 0."""
    wanted = _wanted(VenueName.OKX, symbols)
    found: dict[str, InstrumentMeta] = {}
    for item in payload.get("data", []):
        canonical = wanted.get(item.get("instId", ""))
        if canonical is None:
            continue
        try:
            found[canonical] = InstrumentMeta(
                venue=VenueName.OKX,
                symbol=canonical,
                exchange_symbol=item["instId"],
                status="trading" if item.get("state") == "live" else "suspended",
                price_tick=Decimal(item["tickSz"]),
                qty_step=Decimal(item["lotSz"]),
                min_qty=Decimal(item["minSz"]),
                min_notional=Decimal(0),
                source="rest:okx /api/v5/public/instruments",
                fetched_at=_now_iso(),
            )
        except (KeyError, ValueError) as exc:
            raise FetchError(f"okx instruments malformed for {canonical}: {exc!r}") from exc
    _require_all(VenueName.OKX, wanted, found)
    return [found[c] for c in sorted(found)]


def parse_bitget(payload: dict[str, Any], symbols: list[str]) -> list[InstrumentMeta]:
    """GET /api/v2/spot/public/symbols. Bitget expresses precision as decimal
    places, not step strings; min quantity is minTradeAmount (base), minimum
    notional is minTradeUSDT (quote)."""
    wanted = _wanted(VenueName.BITGET, symbols)
    found: dict[str, InstrumentMeta] = {}
    for item in payload.get("data", []):
        canonical = wanted.get(item.get("symbol", ""))
        if canonical is None:
            continue
        try:
            price_places = int(item["pricePrecision"])
            qty_places = int(item["quantityPrecision"])
            found[canonical] = InstrumentMeta(
                venue=VenueName.BITGET,
                symbol=canonical,
                exchange_symbol=item["symbol"],
                status="trading" if item.get("status") == "online" else "suspended",
                price_tick=Decimal(1).scaleb(-price_places),
                qty_step=Decimal(1).scaleb(-qty_places),
                min_qty=Decimal(item.get("minTradeAmount", "0")),
                min_notional=Decimal(item.get("minTradeUSDT", "0")),
                source="rest:bitget /api/v2/spot/public/symbols",
                fetched_at=_now_iso(),
            )
        except (KeyError, ValueError) as exc:
            raise FetchError(f"bitget symbols malformed for {canonical}: {exc!r}") from exc
    _require_all(VenueName.BITGET, wanted, found)
    return [found[c] for c in sorted(found)]


def _require_all(
    venue: VenueName, wanted: dict[str, str], found: dict[str, InstrumentMeta]
) -> None:
    missing = sorted(set(wanted.values()) - set(found))
    if missing:
        raise FetchError(
            f"{venue.value}: no instrument metadata returned for {missing} — "
            f"delisted/renamed markets must be resolved, not silently dropped"
        )


_ENDPOINTS: dict[VenueName, tuple[str, Callable[[dict, list[str]], list[InstrumentMeta]]]] = {
    VenueName.BINANCE: ("https://api.binance.com/api/v3/exchangeInfo", parse_binance),
    VenueName.BYBIT: (
        "https://api.bybit.com/v5/market/instruments-info?category=spot",
        parse_bybit,
    ),
    VenueName.OKX: ("https://www.okx.com/api/v5/public/instruments?instType=SPOT", parse_okx),
    VenueName.BITGET: ("https://api.bitget.com/api/v2/spot/public/symbols", parse_bitget),
}


async def fetch_venue(venue: VenueName, symbols: list[str]) -> list[InstrumentMeta]:
    url, parser = _ENDPOINTS[venue]
    timeout = aiohttp.ClientTimeout(total=15)
    # trust_env: same deliberate proxy exception as the Binance snapshot client.
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
    return parser(payload, symbols)


async def fetch_all(venues: list[VenueName], symbols: list[str]) -> list[InstrumentMeta]:
    metas: list[InstrumentMeta] = []
    for venue in venues:
        try:
            metas.extend(await fetch_venue(venue, symbols))
        except FetchError:
            raise
        except Exception as exc:
            raise FetchError(f"{venue.value}: fetch failed: {exc!r}") from exc
    return metas


def write_instruments_yaml(path: Path, metas: list[InstrumentMeta]) -> None:
    doc = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "instruments": [
            {
                "venue": m.venue.value,
                "symbol": m.symbol,
                "exchange_symbol": m.exchange_symbol,
                "status": m.status,
                "price_tick": str(m.price_tick),
                "qty_step": str(m.qty_step),
                "min_qty": str(m.min_qty),
                "min_notional": str(m.min_notional),
                "source": m.source,
                "fetched_at": m.fetched_at,
            }
            for m in metas
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
