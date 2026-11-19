"""Phase 5 step 9: Romano-Wolf StepM controls the FWER under the null and detects a planted mean; medoid
representatives are chosen by cluster structure, not by performance."""

from __future__ import annotations

import numpy as np
import pytest

from quant_research.validator.stepm import StepMResult, family_wise_error, medoid_representatives, stepm


def test_null_simulations_keep_the_family_wise_error_at_or_below_alpha() -> None:
    rng = np.random.default_rng(1)
    results: list[StepMResult] = []
    for _ in range(60):
        common = rng.standard_normal((150, 1))
        pnl = 0.6 * common + 0.8 * rng.standard_normal((150, 12))  # correlated nulls
        results.append(stepm(pnl, alpha=0.05, block_len=5, reps=300, seed=int(rng.integers(10**6))))
    fwer = family_wise_error(results)
    assert fwer <= 0.05 + 2.5 * np.sqrt(0.05 * 0.95 / 60)  # <= ~0.12 with 60 simulations


def test_a_planted_positive_mean_is_rejected_and_nulls_are_not() -> None:
    rng = np.random.default_rng(7)
    pnl = rng.standard_normal((300, 8))
    pnl[:, 3] += 0.45  # strong planted alpha (t ~ 7.8)
    res = stepm(pnl, alpha=0.05, block_len=5, reps=500, seed=2)
    assert 3 in res.rejected and len(res.rejected) <= 2
    assert res.steps >= 1 and len(res.critical_values) == res.steps
    again = stepm(pnl, alpha=0.05, block_len=5, reps=500, seed=2)
    assert again == res  # deterministic for a seed


def test_input_validation_and_nan_handling() -> None:
    with pytest.raises(ValueError):
        stepm(np.zeros((2, 3)))
    with pytest.raises(ValueError):
        stepm(np.zeros((10, 3)), alpha=1.5)
    pnl = np.random.default_rng(3).standard_normal((50, 3))
    pnl[:10, 0] = np.nan  # a shorter series: missing days count as flat
    res = stepm(pnl, reps=200)
    assert len(res.statistics) == 3 and all(np.isfinite(s) for s in res.statistics)


def test_medoid_representatives_follow_clusters_not_performance() -> None:
    rng = np.random.default_rng(4)
    t = 200
    f1, f2 = rng.standard_normal(t), rng.standard_normal(t)
    cols = [f1 + 0.3 * rng.standard_normal(t) for _ in range(5)] + [f2 + 0.3 * rng.standard_normal(t) for _ in range(5)]
    pnl = np.column_stack(cols)
    pnl[:, 9] += 5.0  # the best performer sits in cluster 2 but is not automatically the representative
    reps = medoid_representatives(pnl, max_hypotheses=4, seed=0)
    assert len(reps) == 2 and reps[0] < 5 <= reps[1]
    assert medoid_representatives(pnl, max_hypotheses=10) == list(range(10))
