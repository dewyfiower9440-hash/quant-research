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
    RowCost,
    fit_cost,
    fit_cost_v2,
    fit_memory,
    guard_memory,
    holdout_band,
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


# --- v2.3 step 6 (ADR-049 decision 4): rows-only cost per panel bucket, recency-weighted, held-out by batch -----


def _fixed_cost_batches(rng: np.random.Generator, panels: dict[int, float], batches: int) -> list[CostSample]:
    """Trials whose cost is a per-panel fixed cost (A7: the slope on rows x ops was ~0) with 15 % noise."""
    out: list[CostSample] = []
    for b in range(batches):
        rows = list(panels)[b % len(panels)]
        for _ in range(20):
            ops = int(rng.integers(5, 60))  # ops vary but do not drive the cost
            out.append(CostSample("S2", rows, ops, panels[rows] * float(rng.uniform(0.85, 1.15)), group=f"b{b}"))
    return out


def test_the_v2_model_fits_fixed_cost_trials_per_panel_size_within_the_band() -> None:
    """A7: the v1 model (one line on rows x ops) predicted 2-3x; the v2 model keeps panels apart and ignores ops."""
    samples = _fixed_cost_batches(np.random.default_rng(3), {20_000: 0.4, 2_500_000: 6.0}, batches=12)
    v1 = holdout_band(samples, fit_cost)
    v2 = holdout_band(samples, fit_cost_v2)
    assert v2 >= 0.9 > v1


def test_a_bucket_with_few_samples_falls_back_to_the_stage_fit_and_recent_trials_weigh_more() -> None:
    rng = np.random.default_rng(5)
    old = [CostSample("S1", 50_000, 10, 2.0, group="old") for _ in range(400)]
    new = [CostSample("S1", 50_000, 10, 1.0, group="new") for _ in range(200)]
    model = fit_cost_v2([*old, *new])["S1"]
    assert 1.0 < model.predict(50_000, 10) < 1.5  # the newest 200 trials outweigh the 400 older ones
    rare = [CostSample("S1", 8_000_000, 10, 30.0) for _ in range(3)]  # under the bucket minimum
    fitted = fit_cost_v2([*new, *rare, *_fixed_cost_batches(rng, {300_000: 3.0}, 2)])["S1"]
    assert fitted.predict(8_000_000, 10) > 0  # stage-wide fit, not a missing bucket


def test_the_s1_time_budget_shrinks_the_sample_under_either_model() -> None:
    from quant_research.search.pipeline import s1_budget

    v2 = RowCost({}, (2.0, 0.0))  # 2 s per trial on any panel
    assert s1_budget(v2, rows=1_000, count=60, budget_s=200.0) == 60
    assert s1_budget(v2, rows=1_000, count=60, budget_s=50.0) == 25
    assert s1_budget(RowCost({}, (0.0, 0.0)), rows=1_000, count=60, budget_s=1.0) == 60  # no estimate: no cut
    v1 = LinearCost(0.0, 1e-4)  # 1e-4 s per row x op: 60 proposals x 20 ops x 1000 rows = 120 s
    assert s1_budget(v1, rows=1_000, count=60, budget_s=1_000.0) == 60
    assert s1_budget(v1, rows=1_000, count=60, budget_s=60.0) < 60


def test_one_panel_size_gives_a_fixed_cost_without_a_rounding_slope() -> None:
    """Review (step 6) 1: with every x equal, the weighted mean differed from x by rounding, sxx was ~1e-20 and a
    meaningless slope moved predictions for other sizes in the same bucket by tens of percent."""
    rng = np.random.default_rng(9)
    samples = [CostSample("S2", 50_000, 10, float(rng.uniform(0.5, 1.5))) for _ in range(600)]
    model = fit_cost_v2(samples)["S2"]
    assert model.predict(50_000, 10) == pytest.approx(model.predict(60_000, 10))  # b == 0, not a noise slope
    assert all(b == 0.0 for _a, b in model.buckets.values()) and model.fallback[1] == 0.0


def test_the_fallback_is_the_stage_wide_fit() -> None:
    """Review 5: the fallback test only checked > 0."""
    big = [CostSample("S1", 8_000_000, 10, 30.0) for _ in range(3)]  # under the bucket minimum
    small = [CostSample("S1", 50_000, 10, 1.0) for _ in range(40)]
    model = fit_cost_v2([*small, *big])["S1"]
    assert _bucket_of(8_000_000) not in model.buckets and _bucket_of(50_000) in model.buckets
    a, b = model.fallback
    assert model.predict(8_000_000, 10) == pytest.approx(max(0.0, a + b * 8_000_000))
    assert model.predict(8_000_000, 10) > model.predict(50_000, 10)


def test_a_held_out_stage_the_model_never_saw_counts_as_a_miss() -> None:
    """Review 2: a stage missing from the fitted half left both totals out (inflating the share), and a test half
    with only such stages raised in `within_band`."""
    train = [CostSample("S1", 10, 1, 1.0, group="a")]
    test = [CostSample("S1", 10, 1, 1.0, group="b"), CostSample("S2", 10, 1, 1.0, group="b")]
    assert holdout_band([*train, *test], fit_cost_v2) == 0.5  # S1 hit, S2 unpredictable -> a miss
    assert holdout_band([*train, CostSample("S2", 10, 1, 1.0, group="b")], fit_cost_v2) == 0.0


def _bucket_of(rows: int) -> int:
    return max(rows, 1).bit_length()


def test_the_v2_holdout_needs_groups_on_both_sides() -> None:
    with pytest.raises(ValueError, match="groups"):
        holdout_band([CostSample("S1", 10, 1, 1.0, group="only")], fit_cost_v2)
