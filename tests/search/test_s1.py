"""Phase 4 step 3: S1 IC screening (pure part). Labels start at the first fill after the decision
(exec_open at d + latency), cross-sectional Spearman IC, Newey-West HAC t, event-conditional return."""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np
import polars as pl
import pytest

from quant_research.backtest.panel import Panel, PanelConfig, assemble, decision_grid, settlements
from quant_research.search.s1 import (
    IcFeatureEvaluator,
    S1Config,
    evaluate_s1,
    forward_labels,
    hac_t,
    ic_by_decision,
)
from quant_research.strategy.space import SPACES, Point, build_point
from quant_research.strategy.spec import Node

M = 60 * 10**9
H = 60 * M
T0 = 1_767_225_600 * 10**9


def _mean(s: pl.Series) -> float:
    values = [float(v) for v in s.to_list()]
    return sum(values) / len(values)


def _reference_hac_t(x: list[float], lags: int) -> float:
    """Textbook Newey-West (Bartlett kernel) t of the mean, written as plain loops."""
    t = len(x)
    m = sum(x) / t
    d = [v - m for v in x]
    s = sum(v * v for v in d) / t
    for lag in range(1, lags + 1):
        g = sum(d[i] * d[i - lag] for i in range(lag, t)) / t
        s += 2 * (1 - lag / (lags + 1)) * g
    return m / math.sqrt(s / t)


@pytest.mark.parametrize("lags", [0, 1, 3])
def test_hac_t_matches_the_textbook_formula_on_a_fixed_case(lags: int) -> None:
    x = [0.3, -0.1, 0.4, 0.2, -0.05, 0.25, 0.1, -0.2, 0.35, 0.05]
    got = hac_t(np.asarray(x), lags)
    assert got is not None
    assert got == pytest.approx(_reference_hac_t(x, lags), rel=1e-12)


def test_hac_t_is_undefined_for_too_few_or_constant_samples() -> None:
    assert hac_t(np.asarray([0.1]), 0) is None
    assert hac_t(np.asarray([0.2, 0.2, 0.2]), 1) is None
    with pytest.raises(ValueError, match="lags"):
        hac_t(np.asarray([0.1, 0.2]), -1)


def test_ic_reproduces_a_known_rank_correlation_on_synthetic_data() -> None:
    rng = np.random.default_rng(42)
    t, n, rho = 600, 60, 0.3
    s = rng.standard_normal((t, n))
    y = rho * s + math.sqrt(1 - rho**2) * rng.standard_normal((t, n))
    frame = pl.DataFrame(
        {
            "decision_ns": np.repeat(np.arange(t), n),
            "signal": s.ravel(),
            "label": y.ravel(),
        }
    )
    ic = ic_by_decision(frame, min_cross_section=3)
    expected = 6 / math.pi * math.asin(rho / 2)  # Spearman of a bivariate normal with Pearson rho
    assert ic.height == t
    assert _mean(ic["ic"]) == pytest.approx(expected, abs=0.02)


def test_ic_skips_decisions_with_too_small_a_cross_section_or_a_constant_signal() -> None:
    frame = pl.DataFrame(
        {
            "decision_ns": [1, 1, 2, 2, 2, 3, 3, 3],
            "signal": [1.0, 2.0, 1.0, 2.0, 3.0, 5.0, 5.0, 5.0],
            "label": [0.1, 0.2, 0.3, 0.1, 0.2, 0.1, 0.2, 0.3],
        }
    )
    ic = ic_by_decision(frame, min_cross_section=3)
    assert ic["decision_ns"].to_list() == [2]
    assert ic["ic"].to_list() == pytest.approx([-0.5])


def test_the_label_is_the_return_from_the_first_fill_after_the_decision() -> None:
    frame = pl.DataFrame(
        {
            "symbol": ["A"] * 4 + ["B"] * 4,
            "decision_ns": [1, 2, 3, 4] * 2,
            "exec_open": [100.0, 110.0, 121.0, 133.1, 50.0, 40.0, 30.0, 20.0],
        }
    )
    lab = forward_labels(frame, 2).sort(["symbol", "decision_ns"])
    got = lab["label"].to_list()
    assert got[:2] == pytest.approx([0.21, 0.21])
    assert got[4:6] == pytest.approx([-0.4, -0.5])
    assert got[2] is None and got[3] is None and got[6] is None  # no label past the panel end


def _random_walk_panel(symbols: int, days: int, seed: int) -> Panel:
    rng = np.random.default_rng(seed)
    n = days * 1_440
    frames: list[pl.DataFrame] = []
    names = [f"S{k:02d}USDT" for k in range(symbols)]
    for name in names:
        price = 100.0 * np.exp(np.cumsum(0.001 * rng.standard_normal(n)))
        frames.append(
            pl.DataFrame(
                {
                    "symbol": [name] * n,
                    "event_ns": T0 + np.arange(n) * M,
                    "open": price,
                    "high": price * 1.0005,
                    "low": price * 0.9995,
                    "close": price,
                    "volume": np.full(n, 10.0),
                    "quote_volume": np.full(n, 1_000.0),
                    "available_ns": T0 + np.arange(n) * M + 2 * M,
                }
            )
        )
    minutes = pl.concat(frames)
    cfg = PanelConfig(H)
    decisions = decision_grid(T0, T0 + (days * 24 - 2) * H, cfg)
    frame = assemble(decisions, names, minutes, cfg)
    return Panel(frame, settlements(None, None), [("lake/rw.parquet", 1)], cfg)


@pytest.fixture(scope="module")
def rw_panel() -> Panel:
    return _random_walk_panel(symbols=16, days=20, seed=9)


def test_a_past_only_feature_has_no_ic_on_a_random_walk_while_a_leaky_label_would(rw_panel: Panel) -> None:
    feature = Node(op="ret", args=(Node(op="close"),), window=1)
    rec = IcFeatureEvaluator(S1Config(horizon_bars=1)).evaluate(feature, 1, rw_panel)
    assert rec.ic is not None and abs(rec.ic) < 0.03
    assert rec.samples > 400
    # the detector works: a label that starts at the feature bar's own open overlaps the feature
    frame = rw_panel.frame.sort(["symbol", "decision_ns"])
    leaky = frame.with_columns(
        (pl.col("close") / pl.col("open") - 1).alias("signal"),
        (pl.col("close") / pl.col("open") - 1).alias("label"),
    )
    leaky_ic = ic_by_decision(leaky.select("decision_ns", "signal", "label"), min_cross_section=3)
    assert _mean(leaky_ic["ic"]) > 0.9


def test_a_feature_equal_to_the_future_return_is_detected_as_perfect_ic(rw_panel: Panel) -> None:
    labels = forward_labels(rw_panel.frame, 3).rename({"label": "signal"})
    both = labels.join(forward_labels(rw_panel.frame, 3), on=["symbol", "decision_ns"])
    ic = ic_by_decision(both.select("decision_ns", "signal", "label"), min_cross_section=3)
    assert _mean(ic["ic"]) == pytest.approx(1.0)


def test_evaluate_s1_reports_ic_event_return_net_of_cost_and_flags(rw_panel: Panel) -> None:
    point = Point.of(
        "momentum",
        {"lookback": 24, "quantile": 0.8},
        {"side": "long_short", "smooth": 0, "universe_n": 8, "sizing": "equal", "time_stop": 0},
    )
    spec = build_point(SPACES["momentum"], point)
    res = evaluate_s1(spec, rw_panel, S1Config(horizon_bars=6, round_trip_cost=0.0016))
    assert res.horizon_bars == 6
    assert res.ic_samples > 300 and res.event_samples > 300
    assert res.ic_mean is not None and abs(res.ic_mean) < 0.05  # random walk: no edge
    assert res.event_mean is not None and res.event_mean_net is not None
    assert res.event_mean_net == pytest.approx(res.event_mean - 0.0016)
    assert "funding_not_modelled" in res.flags and "cost_uncalibrated" in res.flags
    m = res.metrics()
    assert m["horizon_bars"] == 6 and m["score"] == res.score()


def test_s1_ignores_warm_up_rows(rw_panel: Panel) -> None:
    decisions = sorted(set(rw_panel.frame["decision_ns"].to_list()))
    late = Panel(rw_panel.frame, rw_panel.funding, rw_panel.files, rw_panel.config, decisions[len(decisions) // 2])
    feature = Node(op="ret", args=(Node(op="close"),), window=1)
    full = IcFeatureEvaluator(S1Config(horizon_bars=1)).evaluate(feature, 1, rw_panel)
    half = IcFeatureEvaluator(S1Config(horizon_bars=1)).evaluate(feature, 1, late)
    assert half.samples < full.samples * 0.6


@pytest.mark.parametrize(
    "make",
    [
        lambda: S1Config(horizon_bars=0),
        lambda: S1Config(round_trip_cost=-0.1),
        lambda: S1Config(min_cross_section=1),
    ],
)
def test_malformed_s1_settings_are_rejected(make: Callable[[], object]) -> None:
    with pytest.raises(ValueError):
        make()
