"""Bulk kline CSV parsing and 1-minute grid QA (plan step 8). Pure functions.

Files from data.binance.vision: 12 columns, a header row only in newer files, timestamps in ms
(futures) or us (spot since 2025), detected per file from the digit count.
"""

from __future__ import annotations

import io

import polars as pl

from quant_research.domain.probe import detect_epoch_unit

COLUMNS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
]
_TO_NS = {"s": 1_000_000_000, "ms": 1_000_000, "us": 1_000, "ns": 1}
MINUTE_NS = 60 * 1_000_000_000


def parse_kline_csv(text: str) -> pl.DataFrame:
    body = text.split("\n", 1)[1] if text[:1].isalpha() else text
    raw = pl.read_csv(
        io.StringIO(body),
        has_header=False,
        new_columns=COLUMNS,
        schema_overrides={"open_time": pl.Int64, "close_time": pl.Int64},
    )
    if raw.height == 0:
        return raw
    scale = _TO_NS[detect_epoch_unit(int(raw["open_time"][0]))]
    return raw.select(
        (pl.col("open_time") * scale).alias("event_ns"),
        (pl.col("close_time") * scale).alias("close_ns"),
        pl.col("open").cast(pl.Float64),
        pl.col("high").cast(pl.Float64),
        pl.col("low").cast(pl.Float64),
        pl.col("close").cast(pl.Float64),
        pl.col("volume").cast(pl.Float64),
        pl.col("quote_volume").cast(pl.Float64),
        pl.col("count").cast(pl.Int64).alias("trades"),
        pl.col("taker_buy_volume").cast(pl.Float64),
        pl.col("taker_buy_quote_volume").cast(pl.Float64),
    )


def missing_minutes(opens_ns: list[int], *, month_start_ns: int, month_end_ns: int) -> list[tuple[int, int]]:
    """[start, end) spans of the 1-minute grid in [month_start, month_end) without a bar."""
    present = sorted(set(opens_ns))
    gaps: list[tuple[int, int]] = []
    expected = month_start_ns
    for t in present:
        if t < month_start_ns or t >= month_end_ns:
            continue
        if t > expected:
            gaps.append((expected, t))
        expected = t + MINUTE_NS
    if expected < month_end_ns:
        gaps.append((expected, month_end_ns))
    return gaps
