"""Phase 4 step 4: per-stage cost model `cost_s = a + b * rows * ops`, a peak-memory model and the batch
budget guard (predict before running, shrink the sample or refuse)."""

from __future__ import annotations

import numpy as np
import pytest

from quant_research.search.cost_model import (
    CostSample,
    LinearCost,
    MemoryBudgetExceeded,
    MemorySample,
    fit_cost,
    fit_memory,
    guard_memory,
    plan_rows,
    spec_ops,
    within_band,
)
from quant_research.strategy.space import SPACES, Point, build_point


def _samples(rng: np.random.Generator, n: int, a: float, b: float, noise: float) -> list[CostSample]:
    out: list[CostSample] = []
    for _ in range(n):
        rows = int(rng.integers(1_000, 50_000))
        ops = int(rng.integers(5, 40))
        cost = (a + b * rows * ops) * float(np.exp(noise * rng.standard_normal()))
        out.append(CostSample("S1", rows, ops, cost))
    return out


def test_a_fit_on_past_batches_predicts_held_out_batches_within_the_band() -> None:
    rng = np.random.default_rng(0)
    train = _samples(rng, 60, a=0.05, b=2e-7, noise=0.2)
    held = _samples(rng, 200, a=0.05, b=2e-7, noise=0.2)
    model = fit_cost(train)["S1"]
    assert model.a == pytest.approx(0.05, rel=0.5)
    assert model.b == pytest.approx(2e-7, rel=0.2)
    share = within_band([model.predict(s.rows, s.ops) for s in held], [s.cost_s for s in held])
    assert share >= 0.9  # completion criterion: >= 90% of held-out batches with ratio in [0.5, 1.5]


def test_the_band_counts_ratio_prediction_over_actual() -> None:
    assert within_band([1.0, 1.0, 1.0, 1.0], [1.0, 0.7, 2.5, 0.6]) == pytest.approx(0.5)
    with pytest.raises(ValueError):
        within_band([1.0], [1.0, 2.0])
    with pytest.raises(ValueError):
        within_band([], [])


def test_stages_are_fitted_separately_and_need_two_distinct_sizes() -> None:
    samples = [
        CostSample("S2", 100, 10, 1.0),
        CostSample("S2", 200, 10, 2.0),
        CostSample("S3", 100, 10, 0.5),
    ]
    models = fit_cost(samples)
    assert set(models) == {"S2"}  # S3 has a single size: no fit, the caller keeps its prior
    assert models["S2"].predict(300, 10) == pytest.approx(3.0)


def test_a_fit_never_predicts_negative_time() -> None:
    model = LinearCost(a=-1.0, b=1e-6)
    assert model.predict(10, 1) >= 0.0


def test_the_memory_guard_refuses_before_running() -> None:
    model = fit_memory([MemorySample(10_000, 300.0), MemorySample(20_000, 500.0), MemorySample(40_000, 900.0)])
    assert model.predict(30_000) == pytest.approx(700.0)
    guard_memory(model, 30_000, limit_mb=800.0)
    with pytest.raises(MemoryBudgetExceeded, match="30000 rows"):
        guard_memory(model, 30_000, limit_mb=600.0)


def test_plan_rows_shrinks_the_sample_to_fit_the_time_budget() -> None:
    model = LinearCost(a=0.0, b=1e-6)
    # 100 candidates x 10 ops on 100k rows = 100 s; a 50 s budget halves the rows
    assert plan_rows(model, rows=100_000, ops=[10] * 100, budget_s=50.0) == 50_000
    assert plan_rows(model, rows=100_000, ops=[10] * 100, budget_s=500.0) == 100_000
    # the fixed part alone exceeds the budget: nothing can run
    assert plan_rows(LinearCost(a=1.0, b=1e-6), rows=1_000, ops=[10] * 100, budget_s=50.0) == 0


def test_spec_ops_counts_the_nodes_of_the_expanded_trees() -> None:
    struct = {"side": "long_short", "smooth": 0, "universe_n": 5, "sizing": "equal", "time_stop": 0}
    plain = build_point(SPACES["trend"], Point.of("trend", {"fast": 24, "slow": 96}, struct))
    smoothed = build_point(SPACES["trend"], Point.of("trend", {"fast": 24, "slow": 96}, {**struct, "smooth": 3}))
    # signal div(sub(ema(close), ema(close)), close) = 7 nodes; long/short gt(signal, 0) = 8 each
    assert spec_ops(plain) == 7 + 8 + 8
    assert spec_ops(smoothed) == spec_ops(plain) + 3
