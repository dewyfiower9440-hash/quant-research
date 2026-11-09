"""Engine inputs shared by both engines (Phase 2 steps 6-7): price / range / funding matrices and the
point-in-time target weights. The two engines differ in ACCOUNTING only (event: quantities and cash;
vector: weights), which is what the cross-check compares.

Fidelity (§9-5):
- L0: fills at the as-of bar close, no costs, no funding (research upper bound, never for adoption);
- L1: fills at the execution interval's first 1m open, fee + half spread + impact, funding at settlement,
  liquidation on the interval's mark high / low (event engine);
- L2: needs aggTrades (not collected) -> refused.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt
import polars as pl

from quant_research.backtest.costs import CostModel
from quant_research.errors import AppError
from quant_research.strategy.compile import Compiled
from quant_research.strategy.spec import TIMEFRAME_NS, StrategySpec

Fidelity = Literal["L0", "L1", "L2"]
YEAR_NS = 365 * 86_400 * 10**9
DAY_NS = 86_400 * 10**9
ATR_WINDOW = 14
ADV_DAYS = 30
F64 = npt.NDArray[np.float64]


class FidelityUnavailable(AppError):
    """The requested fidelity needs data that is not collected (L2: aggTrades)."""


@dataclass(frozen=True, slots=True)
class Market:
    decisions: npt.NDArray[np.int64]  # (T,)
    symbols: list[str]  # (N,)
    price: F64  # (T, N) fill price at decision t (carried forward over missing data)
    tradable: npt.NDArray[np.bool_]  # (T, N) fill price observed and interval complete
    high: F64  # (T, N) interval high / low (NaN if missing)
    low: F64
    mark_high: F64
    mark_low: F64
    atr: F64  # (T, N) point-in-time ATR(ATR_WINDOW) of the as-of bars
    funding: F64  # (T, N) sum over settlements in interval t of rate * mark / price[t]
    funding_events: dict[tuple[int, int], list[tuple[float, float]]]  # (t, i) -> [(rate, mark)]
    sigma_daily: F64  # (T, N) for impact
    adv: F64  # (T, N) average daily quote volume (impact)
    funding_known: bool  # False: no funding data loaded (perps: funding not modelled)


@dataclass(frozen=True, slots=True)
class Targets:
    weight: F64  # (T, N) signed target weight (fraction of equity), 0 = flat
    desire: npt.NDArray[np.int8]  # (T, N) +1 / -1 / 0 from the entry conditions (eligible only)


def _matrix(frame: pl.DataFrame, column: str, decisions: list[int], symbols: list[str]) -> F64:
    wide = (
        frame.select("decision_ns", "symbol", pl.col(column).cast(pl.Float64))
        .pivot(on="symbol", index="decision_ns", values=column)
        .sort("decision_ns")
    )
    for s in symbols:
        if s not in wide.columns:
            wide = wide.with_columns(pl.lit(None, dtype=pl.Float64).alias(s))
    out = wide.select(symbols).to_numpy().astype(np.float64)
    if out.shape[0] != len(decisions):
        raise ValueError("panel is not a full decision x symbol grid")
    return out


def prepare(
    spec: StrategySpec,
    compiled: Compiled,
    panel: pl.DataFrame,
    funding_events: pl.DataFrame,
    *,
    fidelity: Fidelity,
    config_latency_ns: int = 0,
) -> tuple[Market, Targets]:
    if fidelity == "L2":
        raise FidelityUnavailable("L2 fidelity needs aggTrades replay; aggTrades are not collected (T2)")
    frame = panel.join(compiled.frame, on=["symbol", "decision_ns"], how="left").sort(["decision_ns", "symbol"])
    symbols = sorted(set(frame["symbol"].to_list()))
    decisions = sorted(set(frame["decision_ns"].to_list()))
    tf = TIMEFRAME_NS[spec.timeframe]
    bars_per_year = YEAR_NS / tf
    bars_per_day = DAY_NS / tf
    # point-in-time inputs computed per symbol on the as-of bars (no future rows involved)
    frame = frame.sort(["symbol", "decision_ns"]).with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol")).log().alias("_lr"),
    )
    frame = frame.with_columns(
        pl.col("_lr")
        .rolling_std(spec.sizing.vol_window, min_samples=spec.sizing.vol_window)
        .over("symbol")
        .alias("_sd"),
        pl.max_horizontal(
            pl.col("high") - pl.col("low"),
            (pl.col("high") - pl.col("close").shift(1).over("symbol")).abs(),
            (pl.col("low") - pl.col("close").shift(1).over("symbol")).abs(),
        )
        .rolling_mean(ATR_WINDOW, min_samples=ATR_WINDOW)
        .over("symbol")
        .alias("_atr"),
        (
            pl.col("quote_volume").rolling_mean(int(ADV_DAYS * bars_per_day), min_samples=1).over("symbol")
            * bars_per_day
        ).alias("_adv"),
    ).sort(["decision_ns", "symbol"])
    price_col = "close" if fidelity == "L0" else "exec_open"
    raw_price = _matrix(frame, price_col, decisions, symbols)
    observed = ~np.isnan(raw_price)
    complete = _matrix(frame.with_columns(pl.col("complete").cast(pl.Float64)), "complete", decisions, symbols) == 1.0
    tradable = observed & (complete if fidelity == "L1" else True)
    price = raw_price.copy()
    for i in range(price.shape[1]):  # carry the last observed price over missing data (no trading there)
        last = math.nan
        for t in range(price.shape[0]):
            if np.isnan(price[t, i]):
                price[t, i] = last
            else:
                last = price[t, i]
    sd = _matrix(frame, "_sd", decisions, symbols)
    sigma_daily = sd * math.sqrt(bars_per_day)
    sigma_ann = sd * math.sqrt(bars_per_year)
    # desire: long wins only when short is not also true; eligibility from the universe
    long_ = _matrix(frame.with_columns(pl.col("long").cast(pl.Float64)), "long", decisions, symbols) == 1.0
    short_ = _matrix(frame.with_columns(pl.col("short").cast(pl.Float64)), "short", decisions, symbols) == 1.0
    eligible_col = (
        "selected" if spec.universe.selector == "pit_top_liquidity" and "selected" in frame.columns else "member"
    )
    eligible = (
        _matrix(frame.with_columns(pl.col(eligible_col).cast(pl.Float64)), eligible_col, decisions, symbols) == 1.0
    )
    if spec.universe.selector == "fixed":
        eligible &= np.array([s in spec.universe.symbols for s in symbols])[None, :]
    desire = np.where(long_ & ~short_, 1, np.where(short_ & ~long_, -1, 0)).astype(np.int8)
    desire = np.where(eligible, desire, 0).astype(np.int8)
    lev, n = spec.sizing.max_leverage, spec.universe.n
    if spec.sizing.method == "equal":
        size = np.full(price.shape, lev / n)
    else:
        with np.errstate(divide="ignore", invalid="ignore"):
            size = np.minimum(spec.sizing.target_ann_vol / sigma_ann, lev) / n
    size = np.where(np.isfinite(size) & (size > 0), size, 0.0)
    desire = np.where(size > 0, desire, 0).astype(np.int8)  # no volatility estimate yet: no position
    weight = (desire * size).astype(np.float64)
    # funding settlements inside [d_t + latency, d_{t+1} + latency)
    fund = np.zeros(price.shape)
    events: dict[tuple[int, int], list[tuple[float, float]]] = {}
    funding_known = funding_events.height > 0
    if fidelity == "L1" and funding_known:
        starts = np.array(decisions, dtype=np.int64) + config_latency_ns
        col = {s: i for i, s in enumerate(symbols)}
        for sym, event_ns, rate, mark in funding_events.select("symbol", "event_ns", "rate", "mark").iter_rows():
            if sym not in col or rate is None or mark is None:
                continue
            t = int(np.searchsorted(starts, int(event_ns), side="right")) - 1
            if t < 0 or t >= len(decisions) - 1:
                continue  # before the first or after the last interval
            i = col[sym]
            events.setdefault((t, i), []).append((float(rate), float(mark)))
            if not np.isnan(price[t, i]):
                fund[t, i] += float(rate) * float(mark) / price[t, i]
    market = Market(
        decisions=np.array(decisions, dtype=np.int64),
        symbols=symbols,
        price=price,
        tradable=tradable,
        high=_matrix(frame, "exec_high", decisions, symbols),
        low=_matrix(frame, "exec_low", decisions, symbols),
        mark_high=_matrix(frame, "mark_exec_high", decisions, symbols),
        mark_low=_matrix(frame, "mark_exec_low", decisions, symbols),
        atr=_matrix(frame, "_atr", decisions, symbols),
        funding=fund,
        funding_events=events,
        sigma_daily=sigma_daily,
        adv=_matrix(frame, "_adv", decisions, symbols),
        funding_known=funding_known,
    )
    return market, Targets(weight=weight, desire=desire)


def trade_rate(cost: CostModel, notional: float, sigma_daily: float, adv: float, fidelity: Fidelity) -> float:
    if fidelity == "L0":
        return 0.0
    return cost.rate(notional, None if math.isnan(sigma_daily) else sigma_daily, None if math.isnan(adv) else adv)
