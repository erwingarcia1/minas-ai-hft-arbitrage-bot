"""Viability report: aggregation, persistence floor, transparent ranking."""

from __future__ import annotations

import pytest

from arb.report.viability import build_report, render_table


def opp(
    *,
    buy: str = "okx",
    sell: str = "binance",
    clip: float = 1000.0,
    duration_ms: float = 500.0,
    net_bps: float = 5.0,
    profit: float = 0.5,
    opened: str = "2026-07-11T00:00:00+00:00",
    closed: str = "2026-07-11T00:00:01+00:00",
) -> dict:
    return {
        "type": "opportunity",
        "symbol": "BTC/USDT",
        "buy_venue": buy,
        "sell_venue": sell,
        "clip_usdt": clip,
        "duration_ms": duration_ms,
        "net_bps_open": net_bps,
        "max_net_bps": net_bps + 1.0,
        "est_profit_usdt_open": profit,
        "opened_at": opened,
        "closed_at": closed,
    }


def sample(*, net_bps: float, ts: str = "2026-07-11T00:00:30+00:00") -> dict:
    return {
        "type": "spread_sample",
        "symbol": "BTC/USDT",
        "buy_venue": "okx",
        "sell_venue": "binance",
        "clip_usdt": 1000.0,
        "net_bps": net_bps,
        "ts": ts,
    }


class TestBuildReport:
    def test_persistence_floor_gates_capturable_profit(self) -> None:
        records = [
            opp(duration_ms=40.0, profit=100.0),  # unexecutable fiction: excluded
            opp(duration_ms=400.0, profit=1.5),
            opp(duration_ms=2000.0, profit=2.0),
        ]
        report = build_report(records, min_duration_ms=250.0)
        (row,) = report.rows
        assert row.opp_count == 3
        assert row.persistent_count == 2
        assert row.capturable_profit_usdt == pytest.approx(3.5)

    def test_ranking_is_by_capturable_profit(self) -> None:
        records = [
            opp(buy="okx", sell="binance", profit=1.0),
            opp(buy="binance", sell="okx", profit=9.0),
            opp(buy="binance", sell="okx", profit=9.0),
        ]
        report = build_report(records, min_duration_ms=250.0)
        assert [(r.buy_venue, r.capturable_profit_usdt) for r in report.rows] == [
            ("binance", 18.0),
            ("okx", 1.0),
        ]

    def test_samples_aggregate(self) -> None:
        records = [sample(net_bps=-8.0), sample(net_bps=2.0), sample(net_bps=4.0)]
        report = build_report(records, min_duration_ms=250.0)
        (row,) = report.rows
        assert row.sample_count == 3
        assert row.sample_positive_share == pytest.approx(2 / 3)
        assert row.sample_net_bps_p50 == pytest.approx(2.0)

    def test_window_spans_all_records(self) -> None:
        records = [
            opp(opened="2026-07-10T00:00:00+00:00", closed="2026-07-10T00:00:01+00:00"),
            sample(net_bps=1.0, ts="2026-07-11T12:00:00+00:00"),
        ]
        report = build_report(records, min_duration_ms=250.0)
        assert report.window_hours == pytest.approx(36.0)

    def test_junk_records_are_counted_not_guessed(self) -> None:
        records = [opp(), {"type": "mystery"}, {"type": "opportunity"}]  # last: missing fields
        report = build_report(records, min_duration_ms=250.0)
        assert report.records_read == 3
        assert report.records_skipped == 2
        (row,) = report.rows
        assert row.opp_count == 1

    def test_empty_dataset_is_a_report_not_an_error(self) -> None:
        report = build_report([], min_duration_ms=250.0)
        assert report.rows == []
        assert "no records" in render_table(report)

    def test_json_shape_labels_results_theoretical(self) -> None:
        report = build_report([opp()], min_duration_ms=250.0)
        doc = report.to_dict()
        assert "NOT realized profit" in doc["note"]
        assert doc["rows"][0]["capturable_profit_usdt"] == pytest.approx(0.5)


class TestRenderTable:
    def test_renders_ranked_rows(self) -> None:
        records = [opp(profit=3.0), opp(buy="binance", sell="okx", profit=7.0)]
        text = render_table(build_report(records, min_duration_ms=250.0))
        lines = [l for l in text.splitlines() if l and l[0].isdigit()]
        assert lines[0].startswith("1\tBTC/USDT\tbinance")
        assert "THEORETICAL" in text
