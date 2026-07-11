"""Property-based tests (hypothesis) for the money math (CLAUDE.md §39).

Properties proven here (numbering from §39):
1. Increasing fees cannot increase expected profit.
2. Increasing slippage (a worse VWAP on either leg) cannot increase profit.
3. Rounding cannot create money: rounded quantity never exceeds the raw
   quantity and is always a whole multiple of both venues' steps.
10. An invalid (unsynced/stale) book cannot produce an evaluation.
"""

from __future__ import annotations

import time
from decimal import Decimal
from fractions import Fraction

from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from arb.feeds.book import BookRegistry, OrderBook
from arb.instruments.registry import common_qty_step, round_qty_for_pair
from arb.models import VenueName
from arb.signals.fees import net_edge_bps
from arb.signals.spread import SpreadDetector
from tests.conftest import ListSink, fill_book, make_metrics, make_settings

prices = st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False)
fees = st.floats(min_value=0.0, max_value=100.0)
fee_bumps = st.floats(min_value=0.0, max_value=50.0)
quantities = st.floats(min_value=1e-9, max_value=1e6, allow_nan=False, allow_infinity=False)
steps = st.sampled_from(
    [Decimal(s) for s in ("0.00000001", "0.000001", "0.0001", "0.001", "0.01", "0.1", "1",
                          "0.002", "0.005", "0.25")]
)


class TestFeeMonotonicity:
    @given(buy=prices, sell=prices, f_buy=fees, f_sell=fees, bump=fee_bumps)
    def test_increasing_either_fee_cannot_increase_net_edge(
        self, buy: float, sell: float, f_buy: float, f_sell: float, bump: float
    ) -> None:
        base = net_edge_bps(buy, sell, f_buy, f_sell)
        assert net_edge_bps(buy, sell, f_buy + bump, f_sell) <= base + 1e-9
        assert net_edge_bps(buy, sell, f_buy, f_sell + bump) <= base + 1e-9

    @given(buy=prices, sell=prices, f_buy=fees, f_sell=fees)
    def test_net_never_exceeds_gross(self, buy, sell, f_buy, f_sell) -> None:
        from arb.signals.fees import gross_edge_bps

        assert net_edge_bps(buy, sell, f_buy, f_sell) <= gross_edge_bps(buy, sell) + 1e-9


class TestSlippageMonotonicity:
    @given(
        buy=prices,
        sell=prices,
        f=fees,
        slip=st.floats(min_value=0.0, max_value=0.1),
    )
    def test_worse_execution_on_either_leg_cannot_increase_net_edge(
        self, buy: float, sell: float, f: float, slip: float
    ) -> None:
        base = net_edge_bps(buy, sell, f, f)
        worse_buy = buy * (1.0 + slip)  # paying more
        worse_sell = sell * (1.0 - slip)  # receiving less
        assert net_edge_bps(worse_buy, sell, f, f) <= base + 1e-9
        assert net_edge_bps(buy, worse_sell, f, f) <= base + 1e-9


class TestRoundingCannotCreateMoney:
    @given(qty=quantities, step_a=steps, step_b=steps)
    def test_rounded_qty_never_exceeds_raw_and_fits_both_steps(
        self, qty: float, step_a: Decimal, step_b: Decimal
    ) -> None:
        from tests.test_instruments import meta

        buy = meta(VenueName.BINANCE, step=str(step_a))
        sell = meta(VenueName.OKX, step=str(step_b))
        rounded = round_qty_for_pair(qty, buy, sell)
        if rounded is None:
            return  # unexecutable is always a safe answer
        assert rounded <= qty * (1 + 1e-12)
        # Exact multiple of both steps (checked in exact rational arithmetic).
        as_fraction = Fraction(Decimal(str(rounded)))
        assert as_fraction % Fraction(step_a) == 0
        assert as_fraction % Fraction(step_b) == 0

    @given(step_a=steps, step_b=steps)
    def test_common_step_is_a_multiple_of_both(self, step_a: Decimal, step_b: Decimal) -> None:
        common = common_qty_step(step_a, step_b)
        assert Fraction(common) % Fraction(step_a) == 0
        assert Fraction(common) % Fraction(step_b) == 0


class TestInvalidBookCannotEvaluate:
    @hyp_settings(max_examples=25)
    @given(unsync_buy=st.booleans(), stale_buy=st.booleans())
    def test_unsynced_or_stale_book_yields_no_evaluation(
        self, unsync_buy: bool, stale_buy: bool
    ) -> None:
        if not (unsync_buy or stale_buy):
            return  # both books healthy: not this property's subject
        settings = make_settings()
        registry = BookRegistry(settings.enabled_venues, settings.symbols)
        detector = SpreadDetector(registry, settings, ListSink(), make_metrics())
        okx = registry.get(VenueName.OKX, "BTC/USDT")
        binance = registry.get(VenueName.BINANCE, "BTC/USDT")
        fill_book(okx, bid=9990, ask=10000)
        fill_book(binance, bid=10030, ask=10040)
        if unsync_buy:
            okx.synced = False
        if stale_buy:
            okx.last_local_mono_ns = time.monotonic_ns() - int(10e9)  # 10s old
        detector.evaluate("BTC/USDT")
        assert all(VenueName.OKX not in (k[1], k[2]) for k in detector.last_evals)
        assert detector.open_count == 0
