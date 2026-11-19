"""Paper market feed from the collected 1 s top-of-book samples (Phase 6 step 16).

The T0 collector stores `binance.um.book_ticker_1s` (ADR-003); the paper engine builds 1-minute mid-price
bars from it (open/high/low/close of the mid, volume = sample count, quote_volume = displayed notional) and
assembles the same point-in-time panel the backtest uses, with `available_ns` = the sample's `received_ns`
(no modelled lag: these rows were observed live). Fills are simulated against the raw quotes that arrived
after the order (`quotes_after`). Only compacted lake files are read (the raw segment still being written
is not), so the feed lags real time by up to one compaction interval (10 min) — documented, not hidden."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import polars as pl

from quant_research.backtest.panel import MINUTE_NS, Panel, PanelConfig, assemble, decision_grid, settlements
from quant_research.data.lake import read_lake_file
from quant_research.paper.engine import Quote

BOOK_TICKER = "binance.um.book_ticker_1s"


@dataclass(frozen=True, slots=True)
class FeedWindow:
    first_ns: int
    last_ns: int


def lake_files(data_root: Path, dataset_id: str, window: FeedWindow) -> list[Path]:
    """Lake files whose day folder may overlap the window (the folder name is the UTC day)."""
    from datetime import UTC, datetime

    lo = datetime.fromtimestamp(window.first_ns / 1e9, UTC).strftime("%Y-%m-%d")
    hi = datetime.fromtimestamp(window.last_ns / 1e9, UTC).strftime("%Y-%m-%d")
    out: list[Path] = []
    for day_dir in sorted((data_root / "lake" / dataset_id).glob("*")):
        if lo <= day_dir.name <= hi:
            out.extend(sorted(day_dir.glob("*.parquet")))
    return out


def load_quotes(data_root: Path, symbols: list[str], window: FeedWindow) -> pl.DataFrame:
    files = lake_files(data_root, BOOK_TICKER, window)
    if not files:
        return pl.DataFrame(
            schema={
                "symbol": pl.String,
                "event_ns": pl.Int64,
                "received_ns": pl.Int64,
                "bid_price": pl.Float64,
                "bid_qty": pl.Float64,
                "ask_price": pl.Float64,
                "ask_qty": pl.Float64,
            }
        )
    frame = pl.concat([read_lake_file(f) for f in files], how="diagonal_relaxed")  # lake module owns Parquet reads
    return (
        frame.filter(
            pl.col("symbol").is_in(symbols) & pl.col("received_ns").is_between(window.first_ns, window.last_ns)
        )
        .select("symbol", "event_ns", "received_ns", "bid_price", "bid_qty", "ask_price", "ask_qty")
        .sort(["symbol", "received_ns"])
    )


def mid_minute_bars(quotes: pl.DataFrame) -> pl.DataFrame:
    """1m bars of the mid price per symbol, in the shape `assemble` expects (available_ns = last receipt)."""
    if quotes.height == 0:
        return pl.DataFrame(
            schema={
                "symbol": pl.String,
                "event_ns": pl.Int64,
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Float64,
                "quote_volume": pl.Float64,
                "available_ns": pl.Int64,
            }
        )
    q = quotes.with_columns(
        ((pl.col("bid_price") + pl.col("ask_price")) / 2).alias("mid"),
        (pl.col("received_ns") // MINUTE_NS * MINUTE_NS).alias("event_ns"),
        ((pl.col("bid_price") * pl.col("bid_qty") + pl.col("ask_price") * pl.col("ask_qty")) / 2).alias("notional"),
    ).sort(["symbol", "received_ns"])
    return (
        q.group_by("symbol", "event_ns", maintain_order=True)
        .agg(
            pl.col("mid").first().alias("open"),
            pl.col("mid").max().alias("high"),
            pl.col("mid").min().alias("low"),
            pl.col("mid").last().alias("close"),
            pl.len().cast(pl.Float64).alias("volume"),
            pl.col("notional").sum().alias("quote_volume"),
            pl.col("received_ns").max().alias("available_ns"),
        )
        .sort(["symbol", "event_ns"])
    )


def panel_from_quotes(
    quotes: pl.DataFrame, symbols: list[str], config: PanelConfig, *, first_decision_ns: int, last_decision_ns: int
) -> Panel:
    minutes = mid_minute_bars(quotes)
    decisions = decision_grid(first_decision_ns, last_decision_ns, config)
    frame = assemble(decisions, symbols, minutes, config)
    return Panel(frame, settlements(None, None), [("paper-feed", quotes.height)], config)


def quotes_after(quotes: pl.DataFrame, symbol: str, since_ns: int, *, until_ns: int | None = None) -> list[Quote]:
    f = quotes.filter((pl.col("symbol") == symbol) & (pl.col("received_ns") >= since_ns))
    if until_ns is not None:
        f = f.filter(pl.col("received_ns") <= until_ns)
    return [
        Quote(symbol, int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]))
        for r in f.select("received_ns", "bid_price", "bid_qty", "ask_price", "ask_qty").sort("received_ns").iter_rows()
    ]


def latest_mid(quotes: pl.DataFrame, symbol: str, at_ns: int) -> float | None:
    f = quotes.filter((pl.col("symbol") == symbol) & (pl.col("received_ns") <= at_ns)).sort("received_ns")
    if f.height == 0:
        return None
    row = f.tail(1).row(0, named=True)
    return (float(row["bid_price"]) + float(row["ask_price"])) / 2
