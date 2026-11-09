"""Fixed-parameter templates of the five v1 families (design v2.1 §7: trend, mean reversion, momentum,
funding/basis, volatility). Phase 2 uses them to exercise the engines on real data; they are NOT a search
(parameter search is Phase 4) and their results are not evidence of an edge."""

from __future__ import annotations

from quant_research.strategy.spec import StrategySpec

Json = dict[str, object]


def _close() -> Json:
    return {"op": "close"}


def _common(family: str, features: Json, signal: Json | str, entry: Json) -> StrategySpec:
    return StrategySpec.model_validate(
        {
            "spec_version": 1,
            "family": family,
            "venue": "binance",
            "market": "usdm_futures",
            "universe": {"selector": "pit_top_liquidity", "n": 10, "rebalance": "1d"},
            "timeframe": "1h",
            "features": features,
            "signal": signal,
            "entry": entry,
            "sizing": {"method": "vol_target", "target_ann_vol": 0.15, "max_leverage": 1.0, "vol_window": 168},
            "execution": {"style": "taker", "max_participation": 0.01},
        }
    )


def templates() -> dict[str, StrategySpec]:
    ret1 = {"op": "ret", "args": [_close()], "window": 1}
    return {
        "trend": _common(
            "trend",
            {
                "fast": {"op": "ema", "args": [_close()], "window": 24},
                "slow": {"op": "ema", "args": [_close()], "window": 96},
            },
            {"op": "div", "args": [{"op": "sub", "args": ["fast", "slow"]}, _close()]},
            {"long": {"op": "gt", "args": ["signal", 0.0]}, "short": {"op": "lt", "args": ["signal", 0.0]}},
        ),
        "mean_reversion": _common(
            "mean_reversion",
            {"z": {"op": "zscore", "args": [_close()], "window": 48}},
            "z",
            {"long": {"op": "lt", "args": ["signal", -1.5]}, "short": {"op": "gt", "args": ["signal", 1.5]}},
        ),
        "momentum": _common(
            "momentum",
            {"r": {"op": "rank_xs", "args": [{"op": "ret", "args": [_close()], "window": 168}]}},
            "r",
            {"long": {"op": "gt", "args": ["signal", 0.8]}, "short": {"op": "lt", "args": ["signal", 0.2]}},
        ),
        "funding_basis": _common(
            "funding_basis",
            {"f": {"op": "rank_xs", "args": [{"op": "zscore", "args": [{"op": "funding_rate"}], "window": 90}]}},
            "f",
            {"long": {"op": "lt", "args": ["signal", 0.2]}, "short": {"op": "gt", "args": ["signal", 0.8]}},
        ),
        "volatility": _common(
            "volatility",
            {
                "ratio": {
                    "op": "div",
                    "args": [{"op": "std", "args": [ret1], "window": 24}, {"op": "std", "args": [ret1], "window": 168}],
                },
                "trend": {"op": "gt", "args": [_close(), {"op": "sma", "args": [_close()], "window": 48}]},
            },
            "ratio",
            {"long": {"op": "and", "args": [{"op": "lt", "args": ["signal", 0.7]}, "trend"]}},
        ),
    }
