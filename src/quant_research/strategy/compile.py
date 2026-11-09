"""Evaluate a StrategySpec on a point-in-time panel (Phase 2 step 4; design v2.1 §7).

Rows are (symbol, decision_ns) sorted by symbol then time. Time-series operators look only at the same
symbol's earlier rows (rolling windows include the current row, which is itself point-in-time); cross-
sectional operators use the universe members at that decision only (others get null); the regime
operator ranks a value within its own past (expanding window). Missing inputs give null; null
conditions are False (no entry during warm-up or missing data).
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

import polars as pl

from quant_research.strategy.canonical import Arg, expanded_trees
from quant_research.strategy.ops import OPS
from quant_research.strategy.spec import Node, StrategySpec

EXPANDING_MIN_HISTORY = 30


class CompileError(ValueError):
    """An expression that cannot be evaluated (unresolved reference or unknown operator: S0 should catch)."""


@dataclass(frozen=True, slots=True)
class Compiled:
    frame: pl.DataFrame  # symbol, decision_ns + signal (Float64), long, short (Boolean, nulls -> False)


def order(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.sort(["symbol", "decision_ns"])


def _num(s: pl.Series) -> pl.Series:
    return s.cast(pl.Float64)


def _expanding_quantile(values: list[float | None]) -> list[float | None]:
    past: list[float] = []
    out: list[float | None] = []
    for v in values:
        if v is None or math.isnan(v):
            out.append(None)
            continue
        out.append(bisect.bisect_right(past, v) / len(past) if len(past) >= EXPANDING_MIN_HISTORY else None)
        bisect.insort(past, v)
    return out


def evaluate(frame: pl.DataFrame, arg: Arg) -> pl.Series:
    """`frame` must be in `order()`; returns a Series aligned with it."""
    if isinstance(arg, str):
        raise CompileError(f"unresolved reference {arg!r} (expand the spec first)")
    if not isinstance(arg, Node):
        return pl.Series("v", [float(arg)] * frame.height, dtype=pl.Float64)
    spec = OPS.get(arg.op)
    if spec is None:
        raise CompileError(f"unknown operator {arg.op!r}")
    if spec.kind == "data":
        if arg.op == "atr":
            return _atr(frame, int(arg.window or 1))
        if spec.column is None:
            raise CompileError(f"data operator {arg.op!r} declares no panel column")
        return _num(frame[spec.column])
    kids = [evaluate(frame, a) for a in arg.args]
    work = frame.select("symbol", "decision_ns", "member").with_columns(*(k.alias(f"a{i}") for i, k in enumerate(kids)))
    a = pl.col("a0")
    b = pl.col("a1")
    w = int(arg.window or 0)
    over = "symbol"
    match arg.op:
        case "add":
            expr = a + b
        case "sub":
            expr = a - b
        case "mul":
            expr = a * b
        case "div":
            expr = pl.when(b != 0).then(a / b)
        case "neg":
            expr = -a
        case "abs":
            expr = a.abs()
        case "sign":
            expr = a.sign().cast(pl.Float64)
        case "log":
            expr = pl.when(a > 0).then(a.log())
        case "lt":
            expr = a < b
        case "gt":
            expr = a > b
        case "and":
            expr = a & b
        case "or":
            expr = a | b
        case "not":
            expr = ~a
        case "sma":
            expr = a.rolling_mean(w, min_samples=w).over(over)
        case "ema":
            expr = a.ewm_mean(span=w, adjust=False, min_samples=w).over(over)
        case "std":
            expr = a.rolling_std(w, min_samples=w).over(over)
        case "zscore":
            sd = a.rolling_std(w, min_samples=w).over(over)
            expr = pl.when(sd > 0).then((a - a.rolling_mean(w, min_samples=w).over(over)) / sd)
        case "ret":
            prev = a.shift(w).over(over)
            expr = pl.when(prev != 0).then(a / prev - 1)
        case "delta":
            expr = a - a.shift(w).over(over)
        case "lag":
            expr = a.shift(w).over(over)
        case "rolling_max":
            expr = a.rolling_max(w, min_samples=w).over(over)
        case "rolling_min":
            expr = a.rolling_min(w, min_samples=w).over(over)
        case "rank_xs" | "demean_xs" | "zscore_xs":
            return _cross_section(work, arg.op)
        case "expanding_quantile":
            out: list[float | None] = []
            for _sym, group in work.group_by("symbol", maintain_order=True):
                out += _expanding_quantile(group["a0"].to_list())
            return pl.Series("v", out, dtype=pl.Float64)
        case _:
            raise CompileError(f"operator {arg.op!r} has no evaluator")
    return work.select(expr.alias("v"))["v"]


def _cross_section(work: pl.DataFrame, op: str) -> pl.Series:
    x = pl.when(pl.col("member") & pl.col("a0").is_not_null() & pl.col("a0").is_not_nan()).then(pl.col("a0"))
    by = "decision_ns"
    match op:
        case "rank_xs":
            n = x.count().over(by)
            rank = x.rank("average").over(by)
            expr = pl.when(x.is_null()).then(None).when(n > 1).then((rank - 1) / (n - 1)).otherwise(0.5)
        case "demean_xs":
            expr = x - x.mean().over(by)
        case _:
            sd = x.std().over(by)
            expr = pl.when(sd > 0).then((x - x.mean().over(by)) / sd)
    return work.select(expr.cast(pl.Float64).alias("v"))["v"]


def _atr(frame: pl.DataFrame, window: int) -> pl.Series:
    prev = pl.col("close").shift(1).over("symbol")
    tr = pl.max_horizontal(pl.col("high") - pl.col("low"), (pl.col("high") - prev).abs(), (pl.col("low") - prev).abs())
    return frame.select(tr.rolling_mean(window, min_samples=window).over("symbol").alias("v"))["v"]


def compile_spec(spec: StrategySpec, panel: pl.DataFrame) -> Compiled:
    frame = order(panel)
    trees = expanded_trees(spec)
    signal_tree = trees["signal"]
    if signal_tree is None:
        raise CompileError("spec has no signal")
    out = frame.select("symbol", "decision_ns").with_columns(evaluate(frame, signal_tree).alias("signal"))
    for side in ("long", "short"):
        tree = trees[side]
        cond = (
            pl.Series(side, [False] * frame.height, dtype=pl.Boolean)
            if tree is None
            else evaluate(frame, tree).cast(pl.Boolean).fill_null(False)
        )
        out = out.with_columns(cond.alias(side))
    return Compiled(out)
