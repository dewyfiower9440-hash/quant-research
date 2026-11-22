"""Point-in-time decision panel (Phase 2 step 3; design v2.1 §5.4, §9).

Decisions happen on a regular grid: d_k = bar_start_k + timeframe + decision_delay. For every decision
and symbol the panel holds
- feature inputs: the newest timeframe bar of each dataset whose `available_ns` (from the versioned lag
  policy, via `PointInTime.rows_visible`) is <= d_k (backward as-of; a late bar simply is not seen yet);
- the execution interval [d_k + latency, d_{k+1} + latency): first 1m open (L1 fill price), high / low /
  close, mark high / low (liquidation check, §9) and the number of 1m bars in it (missing data);
- universe membership at d_k (point-in-time rule) and, optionally, the daily top-n liquidity selection.
Funding settlements are returned separately: they are cash flows, not information.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import polars as pl

from quant_research.catalog.universe_load import split_multiplier
from quant_research.data.access import PointInTime
from quant_research.data.lake import load_bulk
from quant_research.strategy.ops import FUNDING, KLINES, MARK, METRICS, PREMIUM, SPOT

MINUTE_NS = 60 * 10**9
DAY_NS = 86_400 * 10**9


@dataclass(frozen=True, slots=True)
class PanelConfig:
    timeframe_ns: int
    decision_delay_ns: int = MINUTE_NS  # a 1m bar becomes available open + 1m + 1m lag: last minute of the bar
    latency_ns: int = 0  # decision -> order; L1 fills at the 1m open at d + latency

    def __post_init__(self) -> None:
        if self.timeframe_ns % MINUTE_NS or self.timeframe_ns <= 0:
            raise ValueError(f"timeframe must be a positive whole number of minutes, got {self.timeframe_ns} ns")
        if self.latency_ns % MINUTE_NS or self.latency_ns < 0 or self.decision_delay_ns < 0:
            raise ValueError("latency must be whole minutes >= 0 and the decision delay >= 0")


@dataclass(frozen=True, slots=True)
class Panel:
    frame: pl.DataFrame  # one row per (decision_ns, symbol), sorted
    funding: pl.DataFrame  # symbol, event_ns, rate, mark (settlement price)
    files: list[tuple[str, int]]  # lake files read (path, size): the data version
    config: PanelConfig
    evaluate_from_ns: int | None = None  # rows before it are warm-up only (features, no trading, no metrics)


def bars(minutes: pl.DataFrame, timeframe_ns: int, *, offset_ns: int = 0, prefix: str = "") -> pl.DataFrame:
    """1m rows (symbol, event_ns, open, high, low, close[, volume, quote_volume][, available_ns]) -> bars of
    `timeframe_ns` starting at offset_ns (mod timeframe). `available_ns` of a bar = its last minute's."""
    start = ((pl.col("event_ns") - offset_ns) // timeframe_ns) * timeframe_ns + offset_ns
    aggs: list[pl.Expr] = [
        pl.col("open").first().alias(f"{prefix}open"),
        pl.col("high").max().alias(f"{prefix}high"),
        pl.col("low").min().alias(f"{prefix}low"),
        pl.col("close").last().alias(f"{prefix}close"),
        pl.len().cast(pl.Int64).alias(f"{prefix}minutes"),
    ]
    for extra in ("volume", "quote_volume"):
        if extra in minutes.columns:
            aggs.append(pl.col(extra).sum().alias(f"{prefix}{extra}"))
    if "available_ns" in minutes.columns:
        aggs.append(pl.col("available_ns").max().alias(f"{prefix}available_ns"))
    return (
        minutes.sort(["symbol", "event_ns"])
        .group_by("symbol", start.alias("start_ns"), maintain_order=True)
        .agg(aggs)
        .sort(["symbol", "start_ns"])
    )


def decision_grid(first_bar_ns: int, last_bar_ns: int, config: PanelConfig) -> pl.Series:
    """Decision times for the bars starting in [first_bar_ns, last_bar_ns] (both aligned to the timeframe)."""
    tf = config.timeframe_ns
    first = first_bar_ns // tf * tf
    count = (last_bar_ns // tf * tf - first) // tf + 1
    return pl.Series("decision_ns", [first + k * tf + tf + config.decision_delay_ns for k in range(max(0, count))])


def _asof(left: pl.DataFrame, right: pl.DataFrame, available: str) -> pl.DataFrame:
    right = right.sort(["symbol", available])
    return left.sort(["symbol", "decision_ns"]).join_asof(
        right,
        left_on="decision_ns",
        right_on=available,
        by="symbol",
        strategy="backward",
        check_sortedness=False,  # sorted by (symbol, key) just above; Polars cannot verify inside `by` groups
    )


def assemble(
    decisions: pl.Series,
    symbols: Sequence[str],
    klines: pl.DataFrame,
    config: PanelConfig,
    *,
    mark: pl.DataFrame | None = None,
    premium: pl.DataFrame | None = None,
    funding: pl.DataFrame | None = None,
    members: pl.DataFrame | None = None,
    metrics: pl.DataFrame | None = None,
    spot: pl.DataFrame | None = None,
    spot_multiplier: float = 1.0,
) -> pl.DataFrame:
    """Pure assembly from 1m frames that already carry `available_ns` (PointInTime.rows_visible). `metrics` /
    `spot` (v2.3 step 7) add their columns only when given (an empty frame: asked for but no rows -> null columns),
    so the panels of families that do not read them stay as they were."""
    tf = config.timeframe_ns
    grid = pl.DataFrame({"symbol": list(symbols)}).join(decisions.to_frame(), how="cross")
    feature = bars(klines, tf).rename({"start_ns": "bar_start_ns"})
    out = _asof(grid, feature, "available_ns").drop("available_ns")
    for frame, prefix in ((mark, "mark_"), (premium, "premium_")):
        if frame is not None and frame.height:
            extra = bars(
                frame.select("symbol", "event_ns", "open", "high", "low", "close", "available_ns"), tf, prefix=prefix
            )
            extra = extra.select("symbol", f"{prefix}close", f"{prefix}available_ns")
            out = _asof(out, extra, f"{prefix}available_ns").drop(f"{prefix}available_ns")
        else:
            out = out.with_columns(pl.lit(None, dtype=pl.Float64).alias(f"{prefix}close"))
    if funding is not None and funding.height:
        rate = pl.col("last_funding_rate")
        if "funding_interval_hours" in funding.columns:  # 8h-equivalent: 4 h and 8 h payers rank alike (B6 review)
            rate = rate * 8.0 / pl.col("funding_interval_hours").cast(pl.Float64)
        rates = funding.select("symbol", rate.alias("funding_rate"), "available_ns")
        out = _asof(out, rates, "available_ns").drop("available_ns")
    else:
        out = out.with_columns(pl.lit(None, dtype=pl.Float64).alias("funding_rate"))
    out = _join_metrics(out, metrics)
    out = _join_spot(out, spot, tf, spot_multiplier)
    # execution interval [d + latency, next d + latency) as 1m aggregates
    offset = (config.decision_delay_ns + config.latency_ns) % tf
    ex = bars(klines.drop("available_ns", strict=False), tf, offset_ns=offset, prefix="exec_")
    ex = ex.select(
        "symbol",
        (pl.col("start_ns") - config.latency_ns).alias("decision_ns"),
        "exec_open",
        "exec_high",
        "exec_low",
        "exec_close",
        "exec_minutes",
    )
    out = out.join(ex, on=["symbol", "decision_ns"], how="left")
    if mark is not None and mark.height:
        mx = bars(mark.drop("available_ns", strict=False), tf, offset_ns=offset, prefix="mark_exec_")
        mx = mx.select(
            "symbol", (pl.col("start_ns") - config.latency_ns).alias("decision_ns"), "mark_exec_high", "mark_exec_low"
        )
        out = out.join(mx, on=["symbol", "decision_ns"], how="left")
    else:
        out = out.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("mark_exec_high"),
            pl.lit(None, dtype=pl.Float64).alias("mark_exec_low"),
        )
    out = out.with_columns(
        (pl.col("exec_minutes").fill_null(0) == tf // MINUTE_NS).alias("complete"),
        pl.col("exec_minutes").fill_null(0),
    )
    if members is None:
        out = out.with_columns(pl.lit(True).alias("member"))
    else:
        spans = members.select("symbol", "from_ns", "to_ns")
        hit = (
            out.select("symbol", "decision_ns")
            .join(spans, on="symbol", how="inner")
            .filter(
                (pl.col("decision_ns") >= pl.col("from_ns"))
                & (pl.col("to_ns").is_null() | (pl.col("decision_ns") < pl.col("to_ns")))
            )
            .select("symbol", "decision_ns")
            .unique()
            .with_columns(pl.lit(True).alias("member"))
        )
        out = out.join(hit, on=["symbol", "decision_ns"], how="left").with_columns(pl.col("member").fill_null(False))
    return out.sort(["decision_ns", "symbol"])


METRIC_COLUMNS = {  # bulk metrics_5m column -> panel column
    "sum_open_interest_value": "oi_value",
    "sum_toptrader_long_short_ratio": "toptrader_ls",
    "sum_taker_long_short_vol_ratio": "taker_ls",
}


def _join_metrics(out: pl.DataFrame, metrics: pl.DataFrame | None) -> pl.DataFrame:
    """The newest metrics snapshot available at each decision (backward as-of on `available_ns`)."""
    if metrics is None:
        return out
    if not metrics.height:
        return out.with_columns(pl.lit(None, dtype=pl.Float64).alias(c) for c in METRIC_COLUMNS.values())
    snap = metrics.select("symbol", *(pl.col(k).alias(v) for k, v in METRIC_COLUMNS.items()), "available_ns")
    return _asof(out, snap, "available_ns").drop("available_ns")


def _join_spot(out: pl.DataFrame, spot: pl.DataFrame | None, tf: int, multiplier: float) -> pl.DataFrame:
    """The spot pair's bar close at the contract's price level (1000SHIB: SHIB x 1000), as of the decision."""
    if spot is None:
        return out
    if not spot.height:
        return out.with_columns(pl.lit(None, dtype=pl.Float64).alias("spot_close"))
    b = bars(spot.select("symbol", "event_ns", "open", "high", "low", "close", "available_ns"), tf, prefix="spot_")
    b = b.select("symbol", (pl.col("spot_close") * multiplier).alias("spot_close"), "spot_available_ns")
    return _asof(out, b, "spot_available_ns").drop("spot_available_ns")


def select_top_liquidity(frame: pl.DataFrame, n: int, *, window_days: int = 30) -> pl.DataFrame:
    """Daily point-in-time selection: at the first decision of each UTC day, the n members with the largest
    quote volume over the previous `window_days` days of AVAILABLE bars; held for that day."""
    with_day = frame.sort(["symbol", "decision_ns"]).with_columns(
        (pl.col("decision_ns") // DAY_NS).alias("_day"),
        # a NaN / inf volume would poison every later cumulative sum of the symbol (review): it counts as 0
        pl.when(pl.col("quote_volume").is_finite()).then(pl.col("quote_volume")).otherwise(0.0).alias("_qv"),
    )
    # quote volume of the as-of bars, counted once per bar (consecutive decisions may see the same bar)
    per_bar = with_day.unique(subset=["symbol", "bar_start_ns"], keep="first", maintain_order=True)
    # trailing sum over bar days in [day - window, day - 1] as a difference of cumulative sums (as-of joins): the
    # earlier (symbol, day) x (symbol, bar day) join was quadratic in days (4.4 GB on a 36-month 1d panel)
    daily = (
        per_bar.drop_nulls("bar_start_ns")
        .group_by("symbol", (pl.col("bar_start_ns") // DAY_NS).alias("_bar_day"))
        .agg(pl.col("_qv").sum())
        .sort(["symbol", "_bar_day"])
        .with_columns(
            pl.col("_qv").cum_sum().over("symbol").alias("_cum"),
            pl.int_range(1, pl.len() + 1).over("symbol").alias("_cnt"),
        )
        .select("symbol", "_bar_day", "_cum", "_cnt")
    )
    days = (
        with_day.select("symbol", "_day")
        .unique()
        .with_columns((pl.col("_day") - 1).alias("_hi"), (pl.col("_day") - window_days - 1).alias("_lo"))
    )

    def upto(key: str, suffix: str) -> pl.DataFrame:
        return (
            days.sort(["symbol", key])
            .join_asof(
                daily, left_on=key, right_on="_bar_day", by="symbol", strategy="backward", check_sortedness=False
            )
            .select("symbol", "_day", pl.col("_cum").alias(f"_cum{suffix}"), pl.col("_cnt").alias(f"_cnt{suffix}"))
        )

    trailing = (
        upto("_hi", "_hi")
        .join(upto("_lo", "_lo"), on=["symbol", "_day"], how="left")
        .filter((pl.col("_cnt_hi").fill_null(0) - pl.col("_cnt_lo").fill_null(0)) > 0)  # at least one bar day
        .select("symbol", "_day", (pl.col("_cum_hi") - pl.col("_cum_lo").fill_null(0.0)).alias("_trailing"))
    )
    first = with_day.filter(pl.col("decision_ns") == pl.col("decision_ns").min().over("_day"))
    ranked = (
        first.join(trailing, on=["symbol", "_day"], how="left")
        .filter(pl.col("member") & pl.col("_trailing").is_not_null())
        .with_columns(pl.col("_trailing").rank("ordinal", descending=True).over("_day").alias("_rank"))
        .filter(pl.col("_rank") <= n)
        .select("symbol", "_day", pl.lit(True).alias("selected"))
    )
    return (
        with_day.join(ranked, on=["symbol", "_day"], how="left")
        .with_columns((pl.col("selected").fill_null(False) & pl.col("member")).alias("selected"))
        .drop("_day", "_qv")
        .sort(["decision_ns", "symbol"])
    )


def bulk_source(symbol: str, dataset_id: str) -> tuple[str, float]:
    """(lake symbol, price multiplier) a contract's rows of `dataset_id` are stored under: the spot pair has its own
    name (1000SHIBUSDT -> SHIBUSDT, x 1000), every futures dataset the contract's. One mapping for the loader and
    the shard key (review v2.3 step 7). Known limit: a spot pair that keeps the prefix (1000SATS) is not found."""
    if dataset_id != SPOT:
        return symbol, 1.0
    base, multiplier = split_multiplier(symbol.removesuffix("USDT"))
    return f"{base}USDT", multiplier


def months_between(start_ns: int, end_ns: int) -> list[str]:
    out: list[str] = []
    day = datetime.fromtimestamp(start_ns / 1e9, UTC).replace(day=1)
    end = datetime.fromtimestamp(end_ns / 1e9, UTC)
    while day <= end:
        out.append(day.strftime("%Y-%m"))
        day = day.replace(year=day.year + (day.month == 12), month=day.month % 12 + 1)
    return out


def load_panel(
    pit: PointInTime,
    data_root: Path,
    symbols: Sequence[str],
    *,
    first_bar_ns: int,
    last_bar_ns: int,
    config: PanelConfig,
    rule: str | None = "binance.um.pit_top100.v1",
    datasets: Sequence[str] = (KLINES, MARK, PREMIUM, FUNDING),
) -> Panel:
    """Read only the needed lake files, apply the point-in-time rules, assemble. Research role: every
    decision must be before the seal boundary of every dataset read. Symbols are processed one at a time
    (1m frames of one symbol in memory, then its panel rows), which bounds the peak memory."""
    if KLINES not in datasets:
        raise ValueError(f"{KLINES} is required (prices)")
    decisions = decision_grid(first_bar_ns, last_bar_ns, config)
    months = months_between(first_bar_ns, last_bar_ns + config.timeframe_ns + DAY_NS)
    if decisions.is_empty():
        raise ValueError(f"no decisions between {first_bar_ns} and {last_bar_ns}")
    latest = int(cast(int, decisions.max()))
    members = membership_spans(pit, rule)
    files: list[tuple[str, int]] = []
    parts: list[pl.DataFrame] = []
    funding_parts: list[pl.DataFrame] = []
    for symbol in symbols:
        frames: dict[str, pl.DataFrame] = {}
        multiplier = bulk_source(symbol, SPOT)[1]
        for dataset_id in datasets:
            source = bulk_source(symbol, dataset_id)[0]  # the spot pair is stored under its own name
            read = load_bulk(data_root, dataset_id, [source], months)
            files += read.files
            if read.frame.height:
                pit.check_span(dataset_id, first_bar_ns, latest)  # warm-up included (ADR-040)
                visible = pit.rows_visible(read.frame, dataset_id)
                frames[dataset_id] = (
                    visible.with_columns(pl.lit(symbol).alias("symbol")) if source != symbol else visible
                )
        if KLINES not in frames:
            raise FileNotFoundError(f"no {KLINES} lake files for {symbol} in {months[0]}..{months[-1]}")
        parts.append(
            assemble(
                decisions,
                [symbol],
                frames[KLINES],
                config,
                mark=frames.get(MARK),
                premium=frames.get(PREMIUM),
                funding=frames.get(FUNDING),
                members=members,
                metrics=frames.get(METRICS, pl.DataFrame()) if METRICS in datasets else None,
                spot=frames.get(SPOT, pl.DataFrame()) if SPOT in datasets else None,
                spot_multiplier=multiplier,
            )
        )
        funding_parts.append(settlements(frames.get(FUNDING), frames.get(MARK)))
        del frames
    frame = pl.concat(parts, how="diagonal_relaxed").sort(["decision_ns", "symbol"])
    return Panel(frame, pl.concat(funding_parts, how="diagonal_relaxed"), files, config)


def membership_spans(pit: PointInTime, rule: str | None) -> pl.DataFrame | None:
    members = None
    if rule is not None:
        rows = pit.catalog.conn.execute(
            "select i.venue_symbol, m.from_ns, m.to_ns from universe_membership m join instrument i "
            "using (instrument_id) where m.rule_version = ? and i.venue = 'binance'",
            (rule,),
        ).fetchall()
        members = pl.DataFrame(
            {"symbol": [r[0] for r in rows], "from_ns": [r[1] for r in rows], "to_ns": [r[2] for r in rows]},
            schema={"symbol": pl.String, "from_ns": pl.Int64, "to_ns": pl.Int64},
        )
    return members


def settlements(funding: pl.DataFrame | None, mark: pl.DataFrame | None) -> pl.DataFrame:
    """Funding events with the mark price at settlement (the 1m mark open at that minute; §9)."""
    schema = {"symbol": pl.String, "event_ns": pl.Int64, "rate": pl.Float64, "mark": pl.Float64}
    if funding is None or not funding.height:
        return pl.DataFrame(schema=schema)
    events = funding.select("symbol", pl.col("event_ns").cast(pl.Int64), pl.col("last_funding_rate").alias("rate"))
    if mark is None or not mark.height:
        return events.with_columns(pl.lit(None, dtype=pl.Float64).alias("mark"))
    prices = mark.select("symbol", pl.col("event_ns").cast(pl.Int64), pl.col("open").alias("mark"))
    return events.join(prices, on=["symbol", "event_ns"], how="left").sort(["symbol", "event_ns"])


def slice_panel(panel: Panel, first_decision_ns: int, last_decision_ns: int, *, warmup_ns: int = 0) -> Panel:
    """The decisions in [first, last] (research splits: selection / confirmation, design §8), preceded by
    `warmup_ns` of earlier decisions so features have their lookback. Warm-up rows are marked not complete
    (no orders) and are excluded from the metrics (`evaluate_from_ns`). Funding settlements are kept up to
    one bar after the last decision (the last interval's cash flows)."""
    lo = first_decision_ns - warmup_ns
    frame = panel.frame.filter(pl.col("decision_ns").is_between(lo, last_decision_ns)).with_columns(
        (pl.col("complete") & (pl.col("decision_ns") >= first_decision_ns)).alias("complete")
    )
    funding = panel.funding.filter(
        pl.col("event_ns").is_between(first_decision_ns, last_decision_ns + panel.config.timeframe_ns)
    )
    return Panel(frame, funding, panel.files, panel.config, evaluate_from_ns=first_decision_ns if warmup_ns else None)
