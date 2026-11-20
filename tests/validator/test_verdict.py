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

ACTIVE: dict[str, float] = {"entries": 200, "mean_exposure": 0.5}  # a holdout run that clearly traded


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
    strong = 0.01 + 0.01 * rng.standard_normal(700)  # SR ~ 1 per day: unambiguous
    noise = 0.01 * rng.standard_normal(700)
    rule = DecisionRule(bootstrap_reps=500)
    t_strong, t_noise = sr_test(strong, rule), sr_test(noise, rule)
    assert t_strong.reject and t_strong.p_value < rule.alpha_per_query and t_strong.se > 0
    assert not t_noise.reject
    stresses = {"cost x2": 0.5, "cost x3": 0.3, "lag+1": 0.6, "lagx2": 0.4}
    v = decide(strong, in_sample_sr=0.8, rule=rule, stressed_sr=stresses, breakeven=7.5, **ACTIVE)
    assert v.passed and v.reasons == () and v.detail["breakeven_cost_multiple"] == 7.5
    assert v.detail["rule_hash"] == rule.hash() and v.detail["days"] == 700
    bad = decide(noise, in_sample_sr=0.8, rule=rule, stressed_sr=stresses, **ACTIVE)
    assert not bad.passed and any(r.startswith("H0") for r in bad.reasons)


def test_shrinkage_floor_and_stress_gates_are_each_sufficient_to_fail() -> None:
    rng = np.random.default_rng(5)
    strong = 0.01 + 0.01 * rng.standard_normal(700)
    rule = DecisionRule(bootstrap_reps=400)
    sr = sharpe(strong)
    ok = {"cost x2": 0.5, "cost x3": 0.3, "lag+1": 0.6, "lagx2": 0.4}
    assert decide(strong, in_sample_sr=sr / rule.c + 0.01, rule=rule, stressed_sr=ok, **ACTIVE).passed is False  # floor
    missing_lag = {k: v for k, v in ok.items() if k != "lagx2"}
    v = decide(strong, in_sample_sr=sr, rule=rule, stressed_sr=missing_lag, **ACTIVE)
    assert not v.passed and v.reasons == ("stress lagx2: SR not maintained (None)",)
    negative_cost = {**ok, "cost x3": -0.1}
    assert not decide(strong, in_sample_sr=sr, rule=rule, stressed_sr=negative_cost, **ACTIVE).passed
    lenient = DecisionRule(bootstrap_reps=400, fail_on_cost_stress=False)
    assert decide(strong, in_sample_sr=sr, rule=lenient, stressed_sr=negative_cost, **ACTIVE).passed


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


OK_STRESS = {"cost x2": 0.5, "cost x3": 0.3, "lag+1": 0.6, "lagx2": 0.4}


@pytest.mark.parametrize(
    ("pnl_days", "active_days", "entries", "exposure", "why"),
    [
        (700, 10, 200, 0.5, "active pnl days"),  # 10 days with pnl: the sparse case that passed 33 % of nulls
        (700, 700, 12, 0.5, "entries"),
        (700, 700, 200, 0.01, "mean gross exposure"),
        (400, 400, 200, 0.5, "holdout days"),  # shorter than the calibrated 600 days (ADR-042 addendum)
    ],
)
def test_preconditions_make_a_verdict_insufficient_instead_of_a_spent_query(
    pnl_days: int, active_days: int, entries: int, exposure: float, why: str
) -> None:
    """ADR-042 (v2.2 A2): the SR test is anti-conservative on sparse or short pnl (measured 0.05-0.33 false
    positives at a nominal 0.0025); such a holdout gives no verdict and spends no query."""
    rng = np.random.default_rng(3)
    pnl = np.zeros(pnl_days)
    days = rng.choice(pnl_days, active_days, replace=False)
    pnl[days] = 0.02 + 0.01 * rng.standard_normal(active_days)  # strongly positive where active
    rule = DecisionRule(bootstrap_reps=300)
    v = decide(pnl, 0.1, rule, stressed_sr=OK_STRESS, entries=entries, mean_exposure=exposure)
    assert v.insufficient and not v.passed and any(why in r for r in v.reasons)
    assert v.detail["insufficient"] is True and v.detail["rule_hash"] == DecisionRule(bootstrap_reps=300).hash()


def test_unknown_trades_or_exposure_are_insufficient() -> None:
    pnl = 0.01 + 0.01 * np.random.default_rng(4).standard_normal(700)
    v = decide(pnl, 0.1, DecisionRule(bootstrap_reps=300), stressed_sr=OK_STRESS, entries=None, mean_exposure=None)
    assert v.insufficient and len(v.reasons) == 2


def test_psr_is_a_separate_gate_with_its_own_reason() -> None:
    """The skew/kurtosis-aware PSR must reject too (ADR-042). Measured: at these lengths the block bootstrap is
    almost always the stricter of the two (1 boundary case in 240 skewed series), so PSR is a second, independent
    guard; here both fail on noise and the PSR reason is reported separately."""
    noise = 0.01 * np.random.default_rng(8).standard_normal(700)
    with_psr = decide(noise, 0.0, DecisionRule(bootstrap_reps=300), stressed_sr=OK_STRESS, **ACTIVE)
    without = decide(noise, 0.0, DecisionRule(bootstrap_reps=300, require_psr=False), stressed_sr=OK_STRESS, **ACTIVE)
    assert any(r.startswith("PSR") for r in with_psr.reasons)
    assert not any(r.startswith("PSR") for r in without.reasons) and any(r.startswith("H0") for r in without.reasons)
    assert 0.0 <= float(str(with_psr.detail["psr"])) <= 1.0


@pytest.mark.parametrize("kind", ["dense", "t3", "ar0.3", "active50"])
def test_false_positive_rate_on_null_pnl_stays_near_alpha(kind: str) -> None:
    """A regression guard, not the calibration itself (that is docs/measurements/phase5-verdict-calibration.md at
    the production alpha/k): at alpha/k = 0.05 on 600-day null pnl the rate must stay within 1.5 x alpha plus the
    sampling error of 400 draws, and above a floor (a rule that never passes would be vacuous)."""
    rng = np.random.default_rng({"dense": 1, "t3": 2, "ar0.3": 3, "active50": 4}[kind])
    rule = DecisionRule(alpha=0.5, k=10, bootstrap_reps=200)
    n, sims, hits = 600, 400, 0
    for _ in range(sims):
        if kind == "dense":
            x = 0.01 * rng.standard_normal(n)
        elif kind == "t3":
            x = 0.01 * rng.standard_t(3, n)
        elif kind == "ar0.3":
            e = 0.01 * rng.standard_normal(n)
            x = np.empty(n)
            x[0] = e[0]
            for i in range(1, n):
                x[i] = 0.3 * x[i - 1] + e[i]
        else:
            x = np.where(rng.random(n) < 0.5, 0.01 * rng.standard_normal(n), 0.0)
        v = decide(x, 0.0, rule, stressed_sr=OK_STRESS, **ACTIVE)
        hits += int(v.passed)
    rate = hits / sims
    tolerance = 2.5 * math.sqrt(0.05 * 0.95 / sims)  # sampling error of the estimate (~0.027)
    assert 0.01 <= rate <= 1.5 * rule.alpha_per_query + tolerance, rate
