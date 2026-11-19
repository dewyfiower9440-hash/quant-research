"""Phase 5 step 8: the holdout decision rule — bootstrap SE, H0 SR<=0 at alpha/k, c * SR_in floor, stress gates,
break-even cost multiple; null rejection rate ~ alpha; deterministic for a seed."""

from __future__ import annotations

import math

import numpy as np
import pytest

from quant_research.validator.verdict import (
    DecisionRule,
    bootstrap_sr,
    breakeven_cost_multiple,
    decide,
    sharpe,
    sr_test,
    stationary_bootstrap_indices,
)


def test_rule_is_hashed_and_validated() -> None:
    r = DecisionRule()
    assert r.alpha_per_query == pytest.approx(0.0025) and len(r.hash()) == 64
    assert DecisionRule(c=0.4).hash() != r.hash()
    with pytest.raises(ValueError):
        DecisionRule(k=0)


def test_stationary_bootstrap_blocks_wrap_and_are_deterministic() -> None:
    rng = np.random.default_rng(1)
    idx = stationary_bootstrap_indices(50, 5, 100, rng)
    assert idx.shape == (100, 50) and idx.min() >= 0 and idx.max() < 50
    # within a block the index advances by one (mod n)
    steps = (idx[:, 1:] - idx[:, :-1]) % 50
    assert np.mean(steps == 1) > 0.6  # mean block length 5 -> ~80 % continuations
    x = np.sin(np.arange(200) * 0.3) * 0.01 + 0.002
    a = bootstrap_sr(x, DecisionRule(bootstrap_reps=300, seed=3))
    b = bootstrap_sr(x, DecisionRule(bootstrap_reps=300, seed=3))
    assert np.array_equal(a, b) and a.shape == (300,)


def test_hand_cases_strong_signal_passes_and_noise_fails() -> None:
    rng = np.random.default_rng(2)
    strong = 0.01 + 0.01 * rng.standard_normal(250)  # SR ~ 1 per day: unambiguous
    noise = 0.01 * rng.standard_normal(250)
    rule = DecisionRule(bootstrap_reps=500)
    t_strong, t_noise = sr_test(strong, rule), sr_test(noise, rule)
    assert t_strong.reject and t_strong.p_value < rule.alpha_per_query and t_strong.se > 0
    assert not t_noise.reject
    stresses = {"cost x2": 0.5, "cost x3": 0.3, "lag+1": 0.6, "lagx2": 0.4}
    v = decide(strong, in_sample_sr=0.8, rule=rule, stressed_sr=stresses, breakeven=7.5)
    assert v.passed and v.reasons == () and v.detail["breakeven_cost_multiple"] == 7.5
    assert v.detail["rule_hash"] == rule.hash() and v.detail["days"] == 250
    bad = decide(noise, in_sample_sr=0.8, rule=rule, stressed_sr=stresses)
    assert not bad.passed and any(r.startswith("H0") for r in bad.reasons)


def test_shrinkage_floor_and_stress_gates_are_each_sufficient_to_fail() -> None:
    rng = np.random.default_rng(5)
    strong = 0.01 + 0.01 * rng.standard_normal(250)
    rule = DecisionRule(bootstrap_reps=400)
    sr = sharpe(strong)
    ok = {"cost x2": 0.5, "cost x3": 0.3, "lag+1": 0.6, "lagx2": 0.4}
    assert decide(strong, in_sample_sr=sr / rule.c + 0.01, rule=rule, stressed_sr=ok).passed is False  # floor
    missing_lag = {k: v for k, v in ok.items() if k != "lagx2"}
    v = decide(strong, in_sample_sr=sr, rule=rule, stressed_sr=missing_lag)
    assert not v.passed and v.reasons == ("stress lagx2: SR not maintained (None)",)
    negative_cost = {**ok, "cost x3": -0.1}
    assert not decide(strong, in_sample_sr=sr, rule=rule, stressed_sr=negative_cost).passed
    lenient = DecisionRule(bootstrap_reps=400, fail_on_cost_stress=False)
    assert decide(strong, in_sample_sr=sr, rule=lenient, stressed_sr=negative_cost).passed


def test_null_rejection_rate_matches_alpha() -> None:
    rng = np.random.default_rng(11)
    rule = DecisionRule(alpha=0.05, k=1, bootstrap_reps=300, block_len=3)
    n_series = 300
    rejections = sum(sr_test(0.01 * rng.standard_normal(120), rule).reject for _ in range(n_series))
    rate = rejections / n_series
    half = 2.5 * math.sqrt(0.05 * 0.95 / n_series)  # ~0.031
    assert 0.05 - half <= rate <= 0.05 + half + 0.02  # one-sided bootstrap test: no gross anti-conservatism


def test_breakeven_cost_multiple_by_bisection() -> None:
    assert breakeven_cost_multiple(lambda m: 1.0 - 0.2 * m) == pytest.approx(5.0, abs=1e-6)
    assert breakeven_cost_multiple(lambda m: -1.0) is None  # already negative at 1x
    assert breakeven_cost_multiple(lambda m: 1.0) is None  # still positive at 50x: reported as > 50
    assert math.isnan(sharpe(np.array([1.0])))
