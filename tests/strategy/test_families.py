"""v2.2 B6 (D6): reference cases for the new families. Each family's template must recover its intended effect on
a synthetic panel where that effect (and only that) drives the returns, and find nothing on a pure random walk.
The effects are planted at the hourly bar level; minutes interpolate each hour geometrically."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import numpy.typing as npt
import polars as pl
import pytest

from quant_research.backtest.panel import Panel, PanelConfig, assemble, decision_grid, settlements
from quant_research.search.s1 import S1Config, evaluate_s1
from quant_research.strategy.s0 import check
from quant_research.strategy.templates import GRID, NEW_FAMILIES, build

M = 60 * 10**9
H = 60 * M
T0 = 1_767_225_600 * 10**9  # 2026-01-01
HOURS = 24 * 40
SYMBOLS = [f"S{k:02d}USDT" for k in range(12)]
F64 = npt.NDArray[np.float64]


def _minutes(hourly: F64, volume: F64) -> pl.DataFrame:
    """(HOURS, N) hourly log returns -> 1m bars; within an hour the price moves geometrically."""
    frames: list[pl.DataFrame] = []
    for k, name in enumerate(SYMBOLS):
        steps = np.repeat(hourly[:, k] / 60.0, 60)
        price = 100.0 * np.exp(np.cumsum(steps))
        n = price.size
        frames.append(
            pl.DataFrame(
                {
                    "symbol": [name] * n,
                    "event_ns": T0 + np.arange(n, dtype=np.int64) * M,
                    "open": np.concatenate([[100.0], price[:-1]]),
                    "high": price * 1.0002,
                    "low": price * 0.9998,
                    "close": price,
                    "volume": np.full(n, 10.0),
                    "quote_volume": np.full(n, float(volume[k])),
                    "available_ns": T0 + np.arange(n, dtype=np.int64) * M + 2 * M,
                }
            )
        )
    return pl.concat(frames)


def _panel(hourly: F64, *, volume: F64 | None = None, funding: F64 | None = None, premium: F64 | None = None) -> Panel:
    n = len(SYMBOLS)
    minutes = _minutes(hourly, np.full(n, 1_000.0) if volume is None else volume)
    cfg = PanelConfig(H)
    decisions = decision_grid(T0, T0 + (HOURS - 2) * H, cfg)
    fund = None
    if funding is not None:
        stamps = [T0 + j * 8 * H for j in range(HOURS // 8)]
        fund = pl.DataFrame(
            {
                "symbol": [s for s in SYMBOLS for _ in stamps],
                "event_ns": [t for _ in SYMBOLS for t in stamps],
                "last_funding_rate": [float(funding[k]) for k in range(n) for _ in stamps],
            }
        ).with_columns((pl.col("event_ns") + M).alias("available_ns"))
    prem = None
    if premium is not None:
        stamps = [T0 + h * H for h in range(HOURS)]
        prem = pl.DataFrame(
            {
                "symbol": [s for s in SYMBOLS for _ in stamps],
                "event_ns": [t for _ in SYMBOLS for t in stamps],
                "open": [float(premium[k]) for k in range(n) for _ in stamps],
                "high": [float(premium[k]) for k in range(n) for _ in stamps],
                "low": [float(premium[k]) for k in range(n) for _ in stamps],
                "close": [float(premium[k]) for k in range(n) for _ in stamps],
            }
        ).with_columns((pl.col("event_ns") + H + M).alias("available_ns"))
    members = pl.DataFrame(
        {"symbol": SYMBOLS, "from_ns": [T0] * n, "to_ns": [None] * n}, schema_overrides={"to_ns": pl.Int64}
    )
    frame = assemble(decisions, SYMBOLS, minutes, cfg, funding=fund, premium=prem, members=members)
    return Panel(frame, settlements(fund, None), [("lake/synthetic.parquet", 1)], cfg)


def _noise(seed: int, scale: float = 0.004) -> F64:
    return scale * np.random.default_rng(seed).standard_normal((HOURS, len(SYMBOLS)))


def _ranked_drift(trait: F64, edge: float) -> F64:
    """A drift proportional to minus the cross-sectional rank of a trait (low trait -> higher return)."""
    rank = np.argsort(np.argsort(trait)) / (len(trait) - 1)
    return edge * (0.5 - rank)


def _xs_reversal() -> Panel:
    r = _noise(1)
    for t in range(1, HOURS):  # last hour's cross-sectional winners give back 40 % of their excess
        prev = r[t - 1] - r[t - 1].mean()
        r[t] -= 0.4 * prev
    return _panel(r)


def _low_vol() -> Panel:
    vol = np.linspace(0.002, 0.02, len(SYMBOLS))
    r = np.random.default_rng(2).standard_normal((HOURS, len(SYMBOLS))) * vol + _ranked_drift(vol, 0.002)
    return _panel(r)


def _liquidity() -> Panel:
    qv = np.geomspace(1e3, 1e6, len(SYMBOLS))
    return _panel(_noise(3) + _ranked_drift(qv, 0.002), volume=qv)


def _funding_carry() -> Panel:
    rate = np.linspace(-0.0005, 0.0005, len(SYMBOLS))
    return _panel(_noise(4) + _ranked_drift(rate, 0.002), funding=rate)


def _premium_basis() -> Panel:
    prem = np.linspace(-0.002, 0.002, len(SYMBOLS))
    return _panel(_noise(5) + _ranked_drift(prem, 0.002), premium=prem)


def _breakout() -> Panel:
    rng = np.random.default_rng(6)
    drift = np.zeros((HOURS, len(SYMBOLS)))
    for k in range(len(SYMBOLS)):  # trending regimes of 120 h with a random sign each
        signs = rng.choice([-1.0, 1.0], size=HOURS // 120 + 1)
        drift[:, k] = np.repeat(signs, 120)[:HOURS] * 0.003
    return _panel(_noise(7) + drift)


PLANTED: dict[str, tuple[Callable[[], Panel], dict[str, float]]] = {
    "xs_reversal": (_xs_reversal, {"lookback": 1, "quantile": 0.8}),
    "low_vol": (_low_vol, {"window": 48, "quantile": 0.8}),
    "liquidity": (_liquidity, {"window": 24, "quantile": 0.8}),
    "funding_carry": (_funding_carry, {"window": 24, "quantile": 0.8}),
    "premium_basis": (_premium_basis, {"window": 8, "quantile": 0.8}),
    "breakout": (_breakout, {"window": 48, "buffer": 0.0}),
}


def test_every_new_family_has_a_reference_case_and_its_grid_passes_s0() -> None:
    assert set(PLANTED) == set(NEW_FAMILIES)
    for family in NEW_FAMILIES:
        for point in GRID[family]:
            assert check(build(family, point)) == [], (family, point)


@pytest.mark.parametrize("family", NEW_FAMILIES)
def test_each_new_family_recovers_its_planted_effect(family: str) -> None:
    make, params = PLANTED[family]
    spec = build(family, params, n=len(SYMBOLS))
    res = evaluate_s1(spec, make(), S1Config(horizon_bars=4, round_trip_cost=0.0))
    assert res.event_samples >= 100 and res.event_mean is not None and res.event_hac_t is not None
    assert res.event_mean > 0 and res.event_hac_t > 3.0, (family, res)


@pytest.mark.parametrize("family", NEW_FAMILIES)
def test_each_new_family_finds_no_edge_on_a_random_walk(family: str) -> None:
    _make, params = PLANTED[family]
    n = len(SYMBOLS)
    rng = np.random.default_rng(12)  # traits in a random order (not tied to the symbol index)
    flat = _panel(
        _noise(11),
        volume=rng.permutation(np.geomspace(1e3, 1e6, n)),
        funding=rng.permutation(np.linspace(-0.0005, 0.0005, n)),
        premium=rng.permutation(np.linspace(-0.002, 0.002, n)),
    )
    res = evaluate_s1(build(family, params, n=n), flat, S1Config(horizon_bars=4, round_trip_cost=0.0))
    assert res.event_samples >= 100 and res.event_hac_t is not None  # not a pass by having no events
    assert abs(res.event_hac_t) < 3.0, (family, res)
