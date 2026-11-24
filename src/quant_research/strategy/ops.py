"""DSL operator registry (design v2.1 §7). Every operator declares its kind, arity, whether it takes a
window, its lookback and the datasets (hence lag policies) it reads. Pure data, no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Kind = Literal["data", "arith", "compare", "logic", "ts", "xs", "regime"]

MAX_WINDOW = 2_000  # bars
MAX_DEPTH = 8
MAX_NODES = 64
MAX_ABS_CONST = 1e6

KLINES = "binance.um.bulk.klines_1m"
MARK = "binance.um.bulk.mark_klines_1m"
PREMIUM = "binance.um.bulk.premium_klines_1m"
FUNDING = "binance.um.bulk.funding"
METRICS = "binance.um.bulk.metrics_5m"  # v2.3 step 7: open interest, top-trader and taker long/short ratios
SPOT = "binance.spot.bulk.klines_1m"  # v2.3 step 7: the spot pair's 1m close (basis = contract / spot - 1)


@dataclass(frozen=True, slots=True)
class OpSpec:
    kind: Kind
    min_args: int
    max_args: int
    window: bool  # True: requires `window` (bars); False: must not have one
    datasets: tuple[str, ...] = ()
    commutative: bool = False
    column: str | None = None  # data ops: panel column read


def _data(column: str, dataset: str) -> OpSpec:
    return OpSpec("data", 0, 0, False, (dataset,), column=column)


OPS: dict[str, OpSpec] = {
    # data (panel columns; lag = the dataset's lag policy, applied by the point-in-time panel)
    "open": _data("open", KLINES),
    "high": _data("high", KLINES),
    "low": _data("low", KLINES),
    "close": _data("close", KLINES),
    "volume": _data("volume", KLINES),
    "quote_volume": _data("quote_volume", KLINES),
    "mark_close": _data("mark_close", MARK),
    "premium_close": _data("premium_close", PREMIUM),
    "funding_rate": _data("funding_rate", FUNDING),
    "oi_value": _data("oi_value", METRICS),  # open interest in quote currency (5 m snapshot)
    "toptrader_ls": _data("toptrader_ls", METRICS),  # top traders' long / short position ratio
    "taker_ls": _data("taker_ls", METRICS),  # taker buy / sell volume ratio
    "spot_close": _data("spot_close", SPOT),  # spot close at the contract's price level (multiplier applied)
    "atr": OpSpec("data", 0, 0, True, (KLINES,), column="atr"),  # mean true range over `window` bars
    # arithmetic
    "add": OpSpec("arith", 2, 2, False, commutative=True),
    "mul": OpSpec("arith", 2, 2, False, commutative=True),
    "sub": OpSpec("arith", 2, 2, False),
    "div": OpSpec("arith", 2, 2, False),
    "neg": OpSpec("arith", 1, 1, False),
    "abs": OpSpec("arith", 1, 1, False),
    "sign": OpSpec("arith", 1, 1, False),
    "log": OpSpec("arith", 1, 1, False),
    # comparison and logic (entry conditions)
    "lt": OpSpec("compare", 2, 2, False),
    "gt": OpSpec("compare", 2, 2, False),
    "and": OpSpec("logic", 2, 2, False, commutative=True),
    "or": OpSpec("logic", 2, 2, False, commutative=True),
    "not": OpSpec("logic", 1, 1, False),
    # time series (per instrument, past `window` bars including the current one)
    "sma": OpSpec("ts", 1, 1, True),
    "ema": OpSpec("ts", 1, 1, True),
    "std": OpSpec("ts", 1, 1, True),
    "zscore": OpSpec("ts", 1, 1, True),
    "ret": OpSpec("ts", 1, 1, True),  # x / x[window bars ago] - 1
    "delta": OpSpec("ts", 1, 1, True),  # x - x[window bars ago]
    "lag": OpSpec("ts", 1, 1, True),  # x[window bars ago]
    "rolling_max": OpSpec("ts", 1, 1, True),
    "rolling_min": OpSpec("ts", 1, 1, True),
    # cross-section (members of the point-in-time universe at that decision only)
    "rank_xs": OpSpec("xs", 1, 1, False),  # percentile in [0, 1]
    "demean_xs": OpSpec("xs", 1, 1, False),
    "zscore_xs": OpSpec("xs", 1, 1, False),
    # regime: quantile of the value within its own past (expanding window, §7)
    "expanding_quantile": OpSpec("regime", 1, 1, False),
    # ADR-055: the same quantile of the REFERENCE asset's value (BTCUSDT), on every symbol's rows (a market regime)
    "ref_quantile": OpSpec("regime", 1, 1, False),
}


EXPANDING_MIN_HISTORY = 30  # values a quantile against the own past needs before its first output


def lookback(op: str, window: int | None, child_lookbacks: list[int]) -> int:
    """Bars of history an operator needs before its first valid output."""
    inner = max(child_lookbacks, default=0)
    spec = OPS[op]
    if spec.kind == "ts" or op == "atr":
        return inner + int(window or 0)
    if spec.kind == "regime":  # review (v2.4 step 9): undefined until the past holds EXPANDING_MIN_HISTORY values
        return inner + EXPANDING_MIN_HISTORY
    return inner
