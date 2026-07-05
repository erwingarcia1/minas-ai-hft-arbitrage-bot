"""Fee math: the numbers capital decisions rest on, tested exactly."""

from __future__ import annotations

import pytest

from arb.signals.fees import gross_edge_bps, net_edge_bps


def test_gross_edge_simple() -> None:
    assert gross_edge_bps(100.0, 101.0) == pytest.approx(100.0)
    assert gross_edge_bps(100.0, 100.0) == pytest.approx(0.0)
    assert gross_edge_bps(100.0, 99.0) == pytest.approx(-100.0)


def test_net_edge_exact() -> None:
    # buy 100 @ 10bps fee, sell 101 @ 10bps fee:
    # proceeds/cost = (101 * 0.999) / (100 * 1.001) = 100.899 / 100.1
    expected = (100.899 / 100.1 - 1.0) * 1e4
    assert net_edge_bps(100.0, 101.0, 10.0, 10.0) == pytest.approx(expected)
    assert expected == pytest.approx(79.82, abs=0.01)


def test_net_edge_negative_when_fees_exceed_gross() -> None:
    # 5bps gross cannot survive 10+10bps of taker fees.
    net = net_edge_bps(100.0, 100.05, 10.0, 10.0)
    assert net < 0
    assert net == pytest.approx((100.05 * 0.999) / (100.0 * 1.001) * 1e4 - 1e4)


def test_net_edge_zero_fees_equals_gross() -> None:
    assert net_edge_bps(100.0, 101.0, 0.0, 0.0) == pytest.approx(
        gross_edge_bps(100.0, 101.0)
    )


def test_net_is_not_naive_subtraction() -> None:
    # The exact form differs slightly from gross - fees; make sure we ship
    # the exact one (conservative: exact < naive for positive edges).
    naive = gross_edge_bps(100.0, 101.0) - 10.0 - 10.0
    exact = net_edge_bps(100.0, 101.0, 10.0, 10.0)
    assert exact < naive


def test_rejects_nonpositive_prices() -> None:
    with pytest.raises(ValueError):
        gross_edge_bps(0.0, 100.0)
    with pytest.raises(ValueError):
        net_edge_bps(100.0, -1.0, 10.0, 10.0)
