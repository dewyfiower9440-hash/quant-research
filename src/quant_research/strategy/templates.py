"""Templates of the five v1 families (design v2.1 §7: trend, mean reversion, momentum, funding/basis,
volatility). Phase 2 uses the defaults to exercise the engines on real data; Phase 3 lets the Director pick a
template and a few points of a FIXED parameter grid (§7: the LLM chooses structure, numbers come from an
optimiser; the optimiser is Phase 4). They are NOT a search and their results are not evidence of an edge."""

from __future__ import annotations

from collections.abc import Mapping

from quant_research.strategy.spec import StrategySpec

Json = dict[str, object]
Params = Mapping[str, float]
FAMILIES: tuple[str, ...] = ("trend", "mean_reversion", "momentum", "funding_basis", "volatility")  # v1 (Director)
# v2.2 B6 (D6): documented crypto cross-sectional / time-series premia that the panel data already supports.
# v2.3 step 8: OI positioning, taker flow (metrics 5m ratios) and the spot basis, now that the panel carries those
# columns (step 7); they stay data_missing until the data tiers are in the lake. BTC lead-lag (a reference-asset
# operator) and the kimchi premium (Upbit T6) are still held back.
NEW_FAMILIES: tuple[str, ...] = (
    "xs_reversal",
    "funding_carry",
    "premium_basis",
    "low_vol",
    "liquidity",
    "breakout",
    "oi_positioning",
    "taker_flow",
    "spot_basis",
)
ALL_FAMILIES: tuple[str, ...] = FAMILIES + NEW_FAMILIES

# Pre-registered grids (initial priors; widened 2026-11-10, ADR-024); index 0 is the Phase 2 default.
GRID: dict[str, list[dict[str, float]]] = {
    "trend": [
        {"fast": 24, "slow": 96},
        {"fast": 12, "slow": 96},
        {"fast": 48, "slow": 168},
        {"fast": 24, "slow": 336},
        {"fast": 12, "slow": 48},
        {"fast": 48, "slow": 336},
    ],
    "mean_reversion": [
        {"window": 48, "threshold": 1.5},
        {"window": 24, "threshold": 2.0},
        {"window": 96, "threshold": 1.5},
        {"window": 48, "threshold": 2.5},
        {"window": 24, "threshold": 1.5},
        {"window": 96, "threshold": 2.0},
    ],
    "momentum": [
        {"lookback": 168, "quantile": 0.8},
        {"lookback": 72, "quantile": 0.8},
        {"lookback": 336, "quantile": 0.8},
        {"lookback": 168, "quantile": 0.9},
        {"lookback": 72, "quantile": 0.9},
        {"lookback": 336, "quantile": 0.9},
    ],
    "funding_basis": [
        {"window": 90, "quantile": 0.8},
        {"window": 30, "quantile": 0.8},
        {"window": 90, "quantile": 0.9},
        {"window": 30, "quantile": 0.9},
    ],
    "volatility": [
        {"short": 24, "long": 168, "ratio": 0.7},
        {"short": 12, "long": 168, "ratio": 0.6},
        {"short": 24, "long": 336, "ratio": 0.8},
        {"short": 12, "long": 336, "ratio": 0.7},
        {"short": 48, "long": 336, "ratio": 0.6},
    ],
    "xs_reversal": [
        {"lookback": 1, "quantile": 0.8},
        {"lookback": 4, "quantile": 0.8},
        {"lookback": 24, "quantile": 0.9},
    ],
    "funding_carry": [
        {"window": 24, "quantile": 0.8},
        {"window": 72, "quantile": 0.8},
        {"window": 168, "quantile": 0.9},
    ],
    "premium_basis": [{"window": 8, "quantile": 0.8}, {"window": 24, "quantile": 0.8}, {"window": 72, "quantile": 0.9}],
    "low_vol": [{"window": 168, "quantile": 0.8}, {"window": 336, "quantile": 0.8}, {"window": 720, "quantile": 0.9}],
    "liquidity": [{"window": 168, "quantile": 0.8}, {"window": 336, "quantile": 0.8}, {"window": 720, "quantile": 0.9}],
    "breakout": [{"window": 48, "buffer": 0.0}, {"window": 168, "buffer": 0.0}, {"window": 336, "buffer": 0.05}],
    "oi_positioning": [
        {"window": 24, "quantile": 0.8},
        {"window": 72, "quantile": 0.8},
        {"window": 168, "quantile": 0.9},
    ],
    "taker_flow": [{"window": 4, "quantile": 0.8}, {"window": 24, "quantile": 0.8}, {"window": 72, "quantile": 0.9}],
    "spot_basis": [{"window": 8, "quantile": 0.8}, {"window": 24, "quantile": 0.8}, {"window": 72, "quantile": 0.9}],
}
REQUIRED_DATA: dict[str, tuple[str, ...]] = {
    "trend": ("klines",),
    "mean_reversion": ("klines",),
    "momentum": ("klines",),
    "funding_basis": ("klines", "funding"),
    "volatility": ("klines",),
    "xs_reversal": ("klines",),
    "funding_carry": ("klines", "funding"),
    "premium_basis": ("klines", "premium"),
    "low_vol": ("klines",),
    "liquidity": ("klines",),
    "breakout": ("klines",),
    "oi_positioning": ("klines", "open_interest"),
    "taker_flow": ("klines", "taker_ratio"),
    "spot_basis": ("klines", "spot"),
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
        case "xs_reversal":  # short-term cross-sectional reversal: buy the recent losers, sell the winners
            return _ranked("xs_reversal", {"op": "ret", "args": [_close()], "window": int(p["lookback"])}, p, n)
        case "funding_carry":  # long low (negative) funding, short high funding: the carry the shorts receive
            return _ranked(
                "funding_carry", {"op": "sma", "args": [{"op": "funding_rate"}], "window": int(p["window"])}, p, n
            )
        case "premium_basis":  # perp premium over the index: short the richest, long the cheapest
            return _ranked(
                "premium_basis", {"op": "sma", "args": [{"op": "premium_close"}], "window": int(p["window"])}, p, n
            )
        case "low_vol":  # low-volatility anomaly: long the calmest names, short the most volatile
            return _ranked("low_vol", {"op": "std", "args": [ret1], "window": int(p["window"])}, p, n)
        case "liquidity":  # size / liquidity premium: long the least traded members, short the most traded
            return _ranked(
                "liquidity", {"op": "sma", "args": [{"op": "quote_volume"}], "window": int(p["window"])}, p, n
            )
        case "breakout":  # channel breakout: position of the close in the previous `window` bars' high-low range
            w, b = int(p["window"]), p["buffer"]
            hi = {"op": "lag", "args": [{"op": "rolling_max", "args": [{"op": "high"}], "window": w}], "window": 1}
            lo = {"op": "lag", "args": [{"op": "rolling_min", "args": [{"op": "low"}], "window": w}], "window": 1}
            return _common(
                "breakout",
                {
                    "pos": {
                        "op": "div",
                        "args": [{"op": "sub", "args": [_close(), lo]}, {"op": "sub", "args": [hi, lo]}],
                    }
                },
                "pos",
                {
                    "long": {"op": "gt", "args": ["signal", round(1.0 + b, 10)]},
                    "short": {"op": "lt", "args": ["signal", round(-b, 10)]},
                },
                n,
            )
        case "oi_positioning":  # crowding: short the names whose open interest grew most, long those deleveraging
            # contracts = OI value / price: the quote-currency value moves with the price, and its change rate would
            # largely repeat the short-term reversal of xs_reversal (review, step 8)
            contracts = {"op": "div", "args": [{"op": "oi_value"}, _close()]}
            return _ranked("oi_positioning", {"op": "ret", "args": [contracts], "window": int(p["window"])}, p, n)
        case "taker_flow":  # order flow: long persistent taker buying, short taker selling (rank of minus the ratio)
            flow = {"op": "sma", "args": [{"op": "taker_ls"}], "window": int(p["window"])}
            return _ranked("taker_flow", {"op": "neg", "args": [flow]}, p, n)
        case "spot_basis":  # contract over spot: short the richest futures, long the cheapest (cash-and-carry)
            basis = {"op": "sub", "args": [{"op": "div", "args": [_close(), {"op": "spot_close"}]}, 1.0]}
            return _ranked("spot_basis", {"op": "sma", "args": [basis], "window": int(p["window"])}, p, n)
        case _:
            raise ValueError(f"unknown family {family!r} (families: {', '.join(ALL_FAMILIES)})")


def _ranked(family: str, value: Json, p: dict[str, float], n: int) -> StrategySpec:
    """Cross-sectional contrarian rank: long the members ranked below 1 - q, short those above q."""
    q = p["quantile"]
    return _common(
        family,
        {"r": {"op": "rank_xs", "args": [value]}},
        "r",
        {"long": {"op": "lt", "args": ["signal", round(1 - q, 10)]}, "short": {"op": "gt", "args": ["signal", q]}},
        n,
    )


def templates() -> dict[str, StrategySpec]:
    """Phase 2 defaults: grid point 0 of every v1 family (the smoke tool and the Director use these)."""
    return {family: build(family, GRID[family][0]) for family in FAMILIES}


def all_templates() -> dict[str, StrategySpec]:
    """Grid point 0 of every family, v2.2 B6 included."""
    return {family: build(family, GRID[family][0]) for family in ALL_FAMILIES}
