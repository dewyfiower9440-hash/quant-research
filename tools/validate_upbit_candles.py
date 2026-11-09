"""Step 9 validation: compare backfilled Upbit 1m candles with 1m OHLCV aggregated from the T0 trade
capture (`upbit.trades`) over the minutes both cover. Read-only.

    uv run python tools/validate_upbit_candles.py --market KRW-BTC [--hours 12]
"""

from __future__ import annotations

import argparse
import json

import polars as pl

from quant_research.backfill.upbit_candles import DATASET
from quant_research.config import load_settings
from quant_research.data.lake import load_dataset

MIN_NS = 60 * 10**9


def trades_to_minutes(trades: pl.DataFrame, market: str) -> pl.DataFrame:
    return (
        trades.filter(pl.col("market") == market)
        .with_columns((pl.col("event_ns") // MIN_NS * MIN_NS).alias("minute"))
        .sort(["minute", "event_ns", "sequential_id"])
        .group_by("minute", maintain_order=True)
        .agg(
            pl.col("trade_price").first().alias("t_open"),
            pl.col("trade_price").max().alias("t_high"),
            pl.col("trade_price").min().alias("t_low"),
            pl.col("trade_price").last().alias("t_close"),
            pl.col("trade_volume").sum().alias("t_volume"),
            pl.len().alias("t_trades"),
        )
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="KRW-BTC")
    ap.add_argument("--hours", type=float, default=12.0)
    a = ap.parse_args()
    root = load_settings().data_root
    candles = load_dataset(root, DATASET).filter(pl.col("market") == a.market)
    trades = load_dataset(root, "upbit.trades")
    mins = trades_to_minutes(trades, a.market)
    c_ns: list[int] = candles["event_ns"].to_list()
    m_ns: list[int] = mins["minute"].to_list()
    hi = min(max(c_ns), max(m_ns))
    lo = max(min(c_ns), min(m_ns), hi - int(a.hours * 60) * MIN_NS)
    c = candles.filter(pl.col("event_ns").is_between(lo, hi))
    m = mins.filter(pl.col("minute").is_between(lo, hi))
    joined = c.join(m, left_on="event_ns", right_on="minute", how="full", coalesce=True)
    both = joined.filter(pl.col("close").is_not_null() & pl.col("t_close").is_not_null())
    tol = 1e-9
    report = {
        "market": a.market,
        "window_utc": [
            str(pl.from_epoch(pl.Series([lo]), time_unit="ns")[0]),
            str(pl.from_epoch(pl.Series([hi]), time_unit="ns")[0]),
        ],
        "candle_minutes": c.height,
        "trade_minutes": m.height,
        "only_in_candles": joined.filter(pl.col("t_close").is_null()).height,
        "only_in_trades": joined.filter(pl.col("close").is_null()).height,
        "compared": both.height,
        "open_eq": both.filter((pl.col("open") - pl.col("t_open")).abs() <= tol).height,
        "high_eq": both.filter((pl.col("high") - pl.col("t_high")).abs() <= tol).height,
        "low_eq": both.filter((pl.col("low") - pl.col("t_low")).abs() <= tol).height,
        "close_eq": both.filter((pl.col("close") - pl.col("t_close")).abs() <= tol).height,
        "volume_rel_err_max": float(
            both.select(((pl.col("volume") - pl.col("t_volume")).abs() / pl.col("volume")).max()).item() or 0.0
        ),
    }
    print(json.dumps(report, indent=1))  # CLI output


if __name__ == "__main__":
    main()
