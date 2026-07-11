"""Instrument registry: mapping correctness, rounding, minimums, and the
stablecoin-identity invariant (CLAUDE.md §10, §16, §27, §40.23, §48.1/.6)."""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from arb.feeds.book import BookRegistry
from arb.instruments.fetch import (
    FetchError,
    parse_binance,
    parse_bitget,
    parse_bybit,
    parse_okx,
)
from arb.instruments.registry import (
    ASSET_REGISTRY,
    InstrumentMeta,
    InstrumentRegistry,
    InstrumentsError,
    assets_equivalent,
    common_qty_step,
    round_qty_for_pair,
)
from arb.models import VenueName
from arb.signals.spread import SpreadDetector
from tests.conftest import ListSink, fill_book, make_metrics, make_settings


def meta(
    venue: VenueName,
    *,
    step: str = "0.00001",
    min_qty: str = "0",
    min_notional: str = "0",
    max_qty: str | None = None,
    status: str = "trading",
    symbol: str = "BTC/USDT",
) -> InstrumentMeta:
    exchange_symbol = symbol.replace("/", "-" if venue is VenueName.OKX else "")
    return InstrumentMeta(
        venue=venue,
        symbol=symbol,
        exchange_symbol=exchange_symbol,
        status=status,
        price_tick=Decimal("0.01"),
        qty_step=Decimal(step),
        min_qty=Decimal(min_qty),
        min_notional=Decimal(min_notional),
        max_qty=Decimal(max_qty) if max_qty is not None else None,
        source="test",
    )


class TestAssetIdentity:
    def test_stablecoins_are_never_equivalent(self) -> None:
        assert not assets_equivalent("USDT", "USDC")
        assert ASSET_REGISTRY["USDT"].issuer != ASSET_REGISTRY["USDC"].issuer

    def test_identity_is_exact_match_only(self) -> None:
        assert assets_equivalent("BTC", "BTC")
        assert not assets_equivalent("BTC", "XBT")  # alias mapping is explicit, never implied


class TestCommonStep:
    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            ("0.00001", "0.000001", "0.00001"),  # nested: coarser wins
            ("0.002", "0.005", "0.01"),  # non-nested: true lcm
            ("0.1", "0.1", "0.1"),
            ("1", "0.25", "1"),
        ],
    )
    def test_lcm(self, a: str, b: str, expected: str) -> None:
        assert common_qty_step(Decimal(a), Decimal(b)) == Decimal(expected)


class TestRoundQtyForPair:
    def test_floors_to_common_step(self) -> None:
        buy = meta(VenueName.BINANCE, step="0.01")
        sell = meta(VenueName.OKX, step="0.001")
        assert round_qty_for_pair(0.0999, buy, sell) == pytest.approx(0.09)

    def test_never_rounds_up(self) -> None:
        buy = meta(VenueName.BINANCE, step="0.01")
        assert round_qty_for_pair(0.019999, buy, None) == pytest.approx(0.01)

    def test_min_qty_violation_is_unexecutable(self) -> None:
        buy = meta(VenueName.BINANCE, step="0.01", min_qty="0.5")
        assert round_qty_for_pair(0.4, buy, None) is None

    def test_zero_after_rounding_is_unexecutable(self) -> None:
        buy = meta(VenueName.BINANCE, step="1")
        assert round_qty_for_pair(0.9, buy, None) is None

    def test_max_qty_clamps_downward(self) -> None:
        buy = meta(VenueName.BINANCE, step="0.01", max_qty="0.5")
        assert round_qty_for_pair(3.0, buy, None) == pytest.approx(0.5)

    def test_max_clamp_below_other_venues_min_is_unexecutable(self) -> None:
        # Venue A demands >= 0.6 while venue B caps at 0.5: no quantity can
        # satisfy both — the clamp must not sneak past the already-checked min.
        buy = meta(VenueName.BINANCE, step="0.01", min_qty="0.6")
        sell = meta(VenueName.OKX, step="0.01", max_qty="0.5")
        assert round_qty_for_pair(1.0, buy, sell) is None

    def test_no_metadata_passes_through(self) -> None:
        assert round_qty_for_pair(0.123456, None, None) == 0.123456

    def test_non_positive_qty_is_none(self) -> None:
        assert round_qty_for_pair(0.0, None, None) is None


class TestVenueTransformAgreement:
    """The registry's venue_symbol_for is a parallel implementation of the
    four feed modules' venue_symbol transforms. If they ever drift, the
    registry validates metadata against a mapping the FEEDS do not use —
    instrument rules would silently attach to a different market."""

    @pytest.mark.parametrize("symbol", ["BTC/USDT", "ETH/USDT", "SOL/USDC"])
    @pytest.mark.parametrize("venue", list(VenueName))
    def test_registry_transform_matches_feed_transform(self, venue, symbol) -> None:
        from arb.feeds import binance, bitget, bybit, okx
        from arb.instruments.registry import venue_symbol_for

        feed_transform = {
            VenueName.BINANCE: binance.venue_symbol,
            VenueName.BYBIT: bybit.venue_symbol,
            VenueName.OKX: okx.venue_symbol,
            VenueName.BITGET: bitget.venue_symbol,
        }[venue]
        assert venue_symbol_for(venue, symbol) == feed_transform(symbol)


class TestRegistryLoad:
    def test_load_checked_in_file(self) -> None:
        from pathlib import Path

        reg = InstrumentRegistry.load(Path("config/instruments.yaml"))
        assert len(reg) == 8
        okx_btc = reg.get(VenueName.OKX, "BTC/USDT")
        assert okx_btc is not None and okx_btc.exchange_symbol == "BTC-USDT"

    def test_exchange_symbol_mismatch_rejected(self) -> None:
        bad = InstrumentMeta(
            venue=VenueName.OKX,
            symbol="BTC/USDT",
            exchange_symbol="BTCUSDT",  # wrong transform for OKX (should be BTC-USDT)
            price_tick=Decimal("0.1"),
            qty_step=Decimal("0.0001"),
            min_qty=Decimal("0"),
            min_notional=Decimal("0"),
            source="test",
        )
        with pytest.raises(InstrumentsError, match="different market"):
            InstrumentRegistry([bad])

    def test_duplicate_rejected(self) -> None:
        with pytest.raises(InstrumentsError, match="duplicate"):
            InstrumentRegistry([meta(VenueName.BINANCE), meta(VenueName.BINANCE)])

    def test_missing_file_is_fatal(self, tmp_path) -> None:
        with pytest.raises(InstrumentsError, match="cannot read"):
            InstrumentRegistry.load(tmp_path / "nope.yaml")


def _detector(settings, instruments):
    registry = BookRegistry(settings.enabled_venues, settings.symbols)
    sink = ListSink()
    det = SpreadDetector(
        registry, settings, sink, make_metrics(), instruments=instruments
    )
    return registry, det, sink


class TestDetectorRounding:
    """Books: buy okx at 10000, sell binance at 10030 (net ~+10bps at 10bps
    taker fees each side); clip 1000 USDT -> raw qty ~0.09985."""

    def _books(self, registry) -> None:
        fill_book(registry.get(VenueName.OKX, "BTC/USDT"), bid=9990, ask=10000)
        fill_book(registry.get(VenueName.BINANCE, "BTC/USDT"), bid=10030, ask=10040)

    def test_qty_rounded_down_to_common_step(self) -> None:
        settings = make_settings()
        instruments = InstrumentRegistry(
            [meta(VenueName.OKX, step="0.001"), meta(VenueName.BINANCE, step="0.01")]
        )
        registry, det, _ = _detector(settings, instruments)
        self._books(registry)
        det.evaluate("BTC/USDT")
        evals = {
            (k[1].value, k[2].value): v.qty_base for k, v in det.last_evals.items()
        }
        assert evals[("okx", "binance")] == pytest.approx(0.09)

    def test_unrounded_without_metadata(self) -> None:
        settings = make_settings()
        registry, det, _ = _detector(settings, InstrumentRegistry.empty())
        self._books(registry)
        det.evaluate("BTC/USDT")
        key = ("BTC/USDT", VenueName.OKX, VenueName.BINANCE, 1000.0)
        assert det.last_evals[key].qty_base == pytest.approx(1000.0 / 10015.0)

    def test_min_notional_violation_kills_the_clip(self) -> None:
        settings = make_settings()
        instruments = InstrumentRegistry(
            [
                meta(VenueName.OKX, step="0.001", min_notional="2000"),
                meta(VenueName.BINANCE, step="0.001"),
            ]
        )
        registry, det, _ = _detector(settings, instruments)
        self._books(registry)
        det.evaluate("BTC/USDT")
        assert det.last_evals == {}  # 1000 USDT clip < 2000 min notional: unexecutable

    def test_min_qty_violation_kills_the_clip(self) -> None:
        settings = make_settings()
        instruments = InstrumentRegistry(
            [meta(VenueName.OKX, min_qty="1"), meta(VenueName.BINANCE)]
        )
        registry, det, _ = _detector(settings, instruments)
        self._books(registry)
        det.evaluate("BTC/USDT")
        assert det.last_evals == {}

    def test_suspended_instrument_excluded_entirely(self) -> None:
        settings = make_settings()
        instruments = InstrumentRegistry(
            [meta(VenueName.OKX, status="suspended"), meta(VenueName.BINANCE)]
        )
        registry, det, _ = _detector(settings, instruments)
        self._books(registry)
        det.evaluate("BTC/USDT")
        assert det.last_evals == {}  # only pair partner is suspended

    def test_rounding_reduces_estimated_profit_not_edge(self) -> None:
        """Rounding shrinks the executable quantity; with flat one-level books
        the bps edge is unchanged but qty (and thus absolute profit at that
        qty) never grows. Rounding must never manufacture money (§39.3)."""
        settings = make_settings()
        registry_a, det_a, _ = _detector(settings, InstrumentRegistry.empty())
        self._books(registry_a)
        det_a.evaluate("BTC/USDT")
        instruments = InstrumentRegistry(
            [meta(VenueName.OKX, step="0.01"), meta(VenueName.BINANCE, step="0.01")]
        )
        registry_b, det_b, _ = _detector(settings, instruments)
        self._books(registry_b)
        det_b.evaluate("BTC/USDT")
        key = ("BTC/USDT", VenueName.OKX, VenueName.BINANCE, 1000.0)
        assert det_b.last_evals[key].qty_base <= det_a.last_evals[key].qty_base


BINANCE_FIXTURE = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
                {"filterType": "LOT_SIZE", "stepSize": "0.00001000", "minQty": "0.00001000"},
                {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
            ],
        }
    ]
}

BYBIT_FIXTURE = {
    "result": {
        "list": [
            {
                "symbol": "BTCUSDT",
                "status": "Trading",
                "priceFilter": {"tickSize": "0.01"},
                "lotSizeFilter": {
                    "basePrecision": "0.000001",
                    "minOrderQty": "0.000048",
                    "minOrderAmt": "1",
                },
            }
        ]
    }
}

OKX_FIXTURE = {
    "data": [
        {
            "instId": "BTC-USDT",
            "state": "live",
            "tickSz": "0.1",
            "lotSz": "0.00000001",
            "minSz": "0.00001",
        }
    ]
}

BITGET_FIXTURE = {
    "data": [
        {
            "symbol": "BTCUSDT",
            "status": "online",
            "pricePrecision": "2",
            "quantityPrecision": "6",
            "minTradeAmount": "0.00001",
            "minTradeUSDT": "5",
        }
    ]
}


class TestFetchParsers:
    def test_binance(self) -> None:
        (m,) = parse_binance(BINANCE_FIXTURE, ["BTC/USDT"])
        assert (m.qty_step, m.min_notional, m.status) == (
            Decimal("0.00001000"),
            Decimal("5.00000000"),
            "trading",
        )

    def test_binance_suspended_status(self) -> None:
        fixture = {"symbols": [dict(BINANCE_FIXTURE["symbols"][0], status="BREAK")]}
        (m,) = parse_binance(fixture, ["BTC/USDT"])
        assert m.status == "suspended"

    def test_bybit(self) -> None:
        (m,) = parse_bybit(BYBIT_FIXTURE, ["BTC/USDT"])
        assert (m.qty_step, m.min_qty, m.min_notional) == (
            Decimal("0.000001"),
            Decimal("0.000048"),
            Decimal("1"),
        )

    def test_okx(self) -> None:
        (m,) = parse_okx(OKX_FIXTURE, ["BTC/USDT"])
        assert (m.price_tick, m.qty_step, m.min_notional) == (
            Decimal("0.1"),
            Decimal("0.00000001"),
            Decimal("0"),
        )

    def test_bitget_precision_decimals_become_steps(self) -> None:
        (m,) = parse_bitget(BITGET_FIXTURE, ["BTC/USDT"])
        assert (m.price_tick, m.qty_step, m.min_notional) == (
            Decimal("0.01"),
            Decimal("0.000001"),
            Decimal("5"),
        )

    def test_missing_symbol_is_fatal(self) -> None:
        with pytest.raises(FetchError, match="ETH/USDT"):
            parse_binance(BINANCE_FIXTURE, ["BTC/USDT", "ETH/USDT"])

    def test_malformed_filters_are_fatal(self) -> None:
        broken = {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "filters": []}]}
        with pytest.raises(FetchError, match="malformed"):
            parse_binance(broken, ["BTC/USDT"])
