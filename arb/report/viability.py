"""Ranked viability report: which (symbol, venue pair, clip) deserve paper
testing, from the JSONL research logs (live observation or replay output).

The ranking score is deliberately transparent (§17 forbids opaque scores):

    capturable_profit_usdt =
        sum of est_profit_usdt_open over opportunities whose measured
        persistence >= min_duration_ms

i.e. "theoretical edge that stayed open long enough to plausibly execute
against, at the open-time executable price, in observed USDT". The
persistence floor models round-trip reality: an opportunity that lived 40ms
is unexecutable fiction for a non-colocated system (README: expect
150-300ms RTTs; colocated 10-35ms) — the floor is a parameter, not an
opinion. This score is THEORETICAL edge (§34: never mixed with realized
figures — nothing in this build trades, so no realized figures exist).

Failure modes:
- Records with unknown/missing fields are counted as skipped, never guessed.
- An empty dataset produces an empty report with an explicit note, not an
  error — 'no data yet' is a normal state on day one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Iterable

import orjson

logger = logging.getLogger("arb.report.viability")


@dataclass
class PairClipStats:
    symbol: str
    buy_venue: str
    sell_venue: str
    clip_usdt: float

    opp_count: int = 0
    persistent_count: int = 0
    durations_ms: list[float] = field(default_factory=list)
    net_bps_opens: list[float] = field(default_factory=list)
    max_net_bps: float = float("-inf")
    capturable_profit_usdt: float = 0.0

    sample_count: int = 0
    sample_positive: int = 0
    sample_net_bps: list[float] = field(default_factory=list)

    @property
    def median_duration_ms(self) -> float | None:
        return median(self.durations_ms) if self.durations_ms else None

    @property
    def median_net_bps_open(self) -> float | None:
        return median(self.net_bps_opens) if self.net_bps_opens else None

    @property
    def sample_positive_share(self) -> float | None:
        return self.sample_positive / self.sample_count if self.sample_count else None

    @property
    def sample_net_bps_p50(self) -> float | None:
        return median(self.sample_net_bps) if self.sample_net_bps else None


@dataclass
class ViabilityReport:
    min_duration_ms: float
    rows: list[PairClipStats]
    window_start: datetime | None
    window_end: datetime | None
    records_read: int
    records_skipped: int

    @property
    def window_hours(self) -> float | None:
        if self.window_start is None or self.window_end is None:
            return None
        return max((self.window_end - self.window_start).total_seconds(), 0.0) / 3600.0

    def to_dict(self) -> dict:
        return {
            "type": "viability_report",
            "schema_version": 1,
            "note": (
                "THEORETICAL edge from observed books net of configured taker fees, "
                "depth-implied slippage and venue rounding; NOT realized profit — "
                "this system places no orders."
            ),
            "min_duration_ms": self.min_duration_ms,
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "window_hours": self.window_hours,
            "records_read": self.records_read,
            "records_skipped": self.records_skipped,
            "rows": [
                {
                    "symbol": r.symbol,
                    "buy_venue": r.buy_venue,
                    "sell_venue": r.sell_venue,
                    "clip_usdt": r.clip_usdt,
                    "opportunities": r.opp_count,
                    "persistent_opportunities": r.persistent_count,
                    "median_duration_ms": r.median_duration_ms,
                    "median_net_bps_open": r.median_net_bps_open,
                    "max_net_bps": None if r.max_net_bps == float("-inf") else r.max_net_bps,
                    "capturable_profit_usdt": r.capturable_profit_usdt,
                    "spread_samples": r.sample_count,
                    "sample_positive_share": r.sample_positive_share,
                    "sample_net_bps_p50": r.sample_net_bps_p50,
                }
                for r in self.rows
            ],
        }


def load_records(paths: Iterable[Path]) -> Iterable[dict]:
    for path in paths:
        with path.open("rb") as fh:
            for line in fh:
                if line.strip():
                    yield orjson.loads(line)


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def build_report(records: Iterable[dict], *, min_duration_ms: float) -> ViabilityReport:
    stats: dict[tuple[str, str, str, float], PairClipStats] = {}
    window_start: datetime | None = None
    window_end: datetime | None = None
    read = 0
    skipped = 0

    def bucket(rec: dict) -> PairClipStats:
        key = (rec["symbol"], rec["buy_venue"], rec["sell_venue"], float(rec["clip_usdt"]))
        if key not in stats:
            stats[key] = PairClipStats(*key)
        return stats[key]

    def widen(dt: datetime) -> None:
        nonlocal window_start, window_end
        if window_start is None or dt < window_start:
            window_start = dt
        if window_end is None or dt > window_end:
            window_end = dt

    for rec in records:
        read += 1
        try:
            rtype = rec.get("type")
            if rtype == "opportunity":
                # Parse EVERY field before touching shared state: a record
                # that fails mid-parse must be fully skipped, never
                # half-ingested into the money statistics (a "skipped"
                # counter that hides partially-counted profit would be the
                # worst kind of accounting bug).
                duration_ms = float(rec["duration_ms"])
                net_bps_open = float(rec["net_bps_open"])
                max_net_bps = float(rec["max_net_bps"])
                est_profit = float(rec["est_profit_usdt_open"])
                opened = _parse_ts(rec["opened_at"])
                closed = _parse_ts(rec["closed_at"])
                b = bucket(rec)
                b.opp_count += 1
                b.durations_ms.append(duration_ms)
                b.net_bps_opens.append(net_bps_open)
                b.max_net_bps = max(b.max_net_bps, max_net_bps)
                if duration_ms >= min_duration_ms:
                    b.persistent_count += 1
                    b.capturable_profit_usdt += est_profit
                widen(opened)
                widen(closed)
            elif rtype == "spread_sample":
                net_bps = float(rec["net_bps"])
                ts = _parse_ts(rec["ts"])
                b = bucket(rec)
                b.sample_count += 1
                b.sample_net_bps.append(net_bps)
                if net_bps > 0:
                    b.sample_positive += 1
                widen(ts)
            else:
                skipped += 1
        except (KeyError, ValueError, TypeError) as exc:
            skipped += 1
            logger.warning("skipping malformed record (%r): %s", exc, str(rec)[:200])

    rows = sorted(
        stats.values(),
        key=lambda r: (
            -r.capturable_profit_usdt,
            -r.persistent_count,
            -(r.sample_net_bps_p50 if r.sample_net_bps_p50 is not None else float("-inf")),
            r.symbol,
            r.buy_venue,
            r.sell_venue,
            r.clip_usdt,
        ),
    )
    return ViabilityReport(
        min_duration_ms=min_duration_ms,
        rows=rows,
        window_start=window_start,
        window_end=window_end,
        records_read=read,
        records_skipped=skipped,
    )


def render_table(report: ViabilityReport) -> str:
    """Plain-text table (rich is used by the dashboard; the report must also
    be diff-able and pipe-able, so it renders to a string)."""
    header = (
        f"Viability report — THEORETICAL edge only, nothing here is realized profit\n"
        f"persistence floor: {report.min_duration_ms:.0f}ms; window: "
        f"{report.window_start} .. {report.window_end} "
        f"({report.window_hours:.2f}h)\n"
        if report.window_hours is not None
        else "Viability report — no records found (run `python -m arb observe` first)\n"
    )
    if not report.rows:
        return header
    cols = [
        "rank", "symbol", "buy", "sell", "clip$", "opps", "persist",
        "med_ms", "med_bps", "max_bps", "capturable$", "samples", "pos%", "p50_bps",
    ]
    lines = [header, "\t".join(cols)]
    for i, r in enumerate(report.rows, start=1):
        lines.append(
            "\t".join(
                [
                    str(i),
                    r.symbol,
                    r.buy_venue,
                    r.sell_venue,
                    f"{r.clip_usdt:.0f}",
                    str(r.opp_count),
                    str(r.persistent_count),
                    f"{r.median_duration_ms:.0f}" if r.median_duration_ms is not None else "-",
                    f"{r.median_net_bps_open:.2f}" if r.median_net_bps_open is not None else "-",
                    f"{r.max_net_bps:.2f}" if r.max_net_bps != float("-inf") else "-",
                    f"{r.capturable_profit_usdt:.2f}",
                    str(r.sample_count),
                    f"{100 * r.sample_positive_share:.1f}" if r.sample_positive_share is not None else "-",
                    f"{r.sample_net_bps_p50:.2f}" if r.sample_net_bps_p50 is not None else "-",
                ]
            )
        )
    return "\n".join(lines) + "\n"
