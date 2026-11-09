"""Templates of the five v1 families (design v2.1 §7: trend, mean reversion, momentum, funding/basis,
volatility). Phase 2 uses the defaults to exercise the engines on real data; Phase 3 lets the Director pick a
template and a few points of a FIXED parameter grid (§7: the LLM chooses structure, numbers come from an
optimiser; the optimiser is Phase 4). They are NOT a search and their results are not evidence of an edge."""

from __future__ import annotations

from collections.abc import Mapping

from quant_research.strategy.spec import StrategySpec

Json = dict[str, object]
Params = Mapping[str, float]
FAMILIES: tuple[str, ...] = ("trend", "mean_reversion", "momentum", "funding_basis", "volatility")

# Pre-registered grids (initial priors); index 0 is the Phase 2 default of each family.
GRID: dict[str, list[dict[str, float]]] = {
    "trend": [
        {"fast": 24, "slow": 96},
        {"fast": 12, "slow": 96},
        {"fast": 48, "slow": 168},
        {"fast": 24, "slow": 336},
    ],
    "mean_reversion": [
        {"window": 48, "threshold": 1.5},
        {"window": 24, "threshold": 2.0},
        {"window": 96, "threshold": 1.5},
        {"window": 48, "threshold": 2.5},
    ],
    "momentum": [
        {"lookback": 168, "quantile": 0.8},
        {"lookback": 72, "quantile": 0.8},
        {"lookback": 336, "quantile": 0.8},
        {"lookback": 168, "quantile": 0.9},
    ],
    "funding_basis": [
        {"window": 90, "quantile": 0.8},
        {"window": 30, "quantile": 0.8},
        {"window": 90, "quantile": 0.9},
    ],
    "volatility": [
        {"short": 24, "long": 168, "ratio": 0.7},
        {"short": 12, "long": 168, "ratio": 0.6},
        {"short": 24, "long": 336, "ratio": 0.8},
    ],
}
REQUIRED_DATA: dict[str, tuple[str, ...]] = {
    "trend": ("klines",),
    "mean_reversion": ("klines",),
    "momentum": ("klines",),
    "funding_basis": ("klines", "funding"),
    "volatility": ("klines",),
}


def _close() -> Json:
    return {"op": "close"}


def _common(family: str, features: Json, signal: Json | str, entry: Json, n: int) -> StrategySpec:
    return StrategySpec.model_validate(
        {
            "spec_version": 1,
            "family": family,
            "venue": "binance",
            "market": "usdm_futures",
            "universe": {"selector": "pit_top_liquidity", "n": n, "rebalance": "1d"},
            "timeframe": "1h",
            "features": features,
            "signal": signal,
            "entry": entry,
            "sizing": {"method": "vol_target", "target_ann_vol": 0.15, "max_leverage": 1.0, "vol_window": 168},
            "execution": {"style": "taker", "max_participation": 0.01},
        }
    )


def build(family: str, params: Params, *, n: int = 10) -> StrategySpec:
    """The family's spec with the given grid point."""
    ret1 = {"op": "ret", "args": [_close()], "window": 1}
    p = {k: float(v) for k, v in params.items()}
    match family:
        case "trend":
            return _common(
                "trend",
                {
                    "fast": {"op": "ema", "args": [_close()], "window": int(p["fast"])},
                    "slow": {"op": "ema", "args": [_close()], "window": int(p["slow"])},
                },
                {"op": "div", "args": [{"op": "sub", "args": ["fast", "slow"]}, _close()]},
                {"long": {"op": "gt", "args": ["signal", 0.0]}, "short": {"op": "lt", "args": ["signal", 0.0]}},
                n,
            )
        case "mean_reversion":
            t = p["threshold"]
            return _common(
                "mean_reversion",
                {"z": {"op": "zscore", "args": [_close()], "window": int(p["window"])}},
                "z",
                {"long": {"op": "lt", "args": ["signal", -t]}, "short": {"op": "gt", "args": ["signal", t]}},
                n,
            )
        case "momentum":
            q = p["quantile"]
            return _common(
                "momentum",
                {"r": {"op": "rank_xs", "args": [{"op": "ret", "args": [_close()], "window": int(p["lookback"])}]}},
                "r",
                {
                    "long": {"op": "gt", "args": ["signal", q]},
                    "short": {"op": "lt", "args": ["signal", round(1 - q, 10)]},
                },
                n,
            )
        case "funding_basis":
            q = p["quantile"]
            return _common(
                "funding_basis",
                {
                    "f": {
                        "op": "rank_xs",
                        "args": [{"op": "zscore", "args": [{"op": "funding_rate"}], "window": int(p["window"])}],
                    }
                },
                "f",
                {
                    "long": {"op": "lt", "args": ["signal", round(1 - q, 10)]},
                    "short": {"op": "gt", "args": ["signal", q]},
                },
                n,
            )
        case "volatility":
            return _common(
                "volatility",
                {
                    "ratio": {
                        "op": "div",
                        "args": [
                            {"op": "std", "args": [ret1], "window": int(p["short"])},
                            {"op": "std", "args": [ret1], "window": int(p["long"])},
                        ],
                    },
                    "trend": {"op": "gt", "args": [_close(), {"op": "sma", "args": [_close()], "window": 48}]},
                },
                "ratio",
                {"long": {"op": "and", "args": [{"op": "lt", "args": ["signal", p["ratio"]]}, "trend"]}},
                n,
            )
        case _:
            raise ValueError(f"unknown family {family!r} (v1 families: {', '.join(FAMILIES)})")


def templates() -> dict[str, StrategySpec]:
    """Phase 2 defaults: grid point 0 of every family."""
    return {family: build(family, GRID[family][0]) for family in FAMILIES}
