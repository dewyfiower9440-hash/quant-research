"""Phase 4 step 7: N_eff by ONC only, DSR twice (N_eff and N_total) with the conservative one deciding,
E[max] / 95 % quantile reproduced by Monte-Carlo within 1 %, period units throughout."""

from __future__ import annotations

import math

import numpy as np
import pytest

from quant_research.search.multiplicity import (
    annualised,
    average_offdiag,
    dsr,
    dsr_report,
    expected_max_sr,
    max_sr_quantile,
    mc_max_sr,
    min_sr,
    min_sr_table,
    onc_clusters,
    onc_k_cap,
    pairwise_corr,
    psr,
    sharpe_stats,
    sr_variance,
)


def _block_corr(blocks: int, size: int, rho_in: float, rho_out: float = 0.0) -> np.ndarray:
    n = blocks * size
    c = np.full((n, n), rho_out)
    for b in range(blocks):
        s = slice(b * size, (b + 1) * size)
        c[s, s] = rho_in
    np.fill_diagonal(c, 1.0)
    return c


@pytest.mark.parametrize(("blocks", "size"), [(2, 5), (4, 5), (3, 8), (6, 4)])
def test_onc_recovers_the_number_of_blocks_from_a_block_correlation(blocks: int, size: int) -> None:
    cl = onc_clusters(_block_corr(blocks, size, 0.8), seed=1)
    assert cl.k == blocks
    for b in range(blocks):
        assert len({cl.labels[i] for i in range(b * size, (b + 1) * size)}) == 1


def test_onc_recovers_blocks_from_simulated_returns() -> None:
    rng = np.random.default_rng(3)
    t, blocks, size = 400, 4, 6
    factors = rng.standard_normal((t, blocks))
    cols = [factors[:, b] + 0.6 * rng.standard_normal(t) for b in range(blocks) for _ in range(size)]
    pnl = np.column_stack(cols)
    cl = onc_clusters(pairwise_corr(pnl), seed=0)
    assert cl.k == blocks


def test_fewer_than_three_trials_are_their_own_clusters_and_nan_overlap_is_handled() -> None:
    assert onc_clusters(np.eye(2)).n_eff == 2
    pnl = np.array([[1.0, np.nan], [2.0, 1.0], [3.0, 2.0], [4.0, 4.0], [np.nan, 5.0]])
    c = pairwise_corr(pnl)
    assert c[0, 0] == 1.0 and -1.0 <= c[0, 1] <= 1.0 and c[0, 1] == c[1, 0]
    assert average_offdiag(np.eye(3)) == 0.0


def test_expected_max_and_quantile_match_monte_carlo_within_one_percent() -> None:
    n, var = 400, 1.0 / 250
    e_mc, q95_mc = mc_max_sr(n, var, reps=40_000, seed=7)
    assert expected_max_sr(n, var) == pytest.approx(e_mc, rel=0.01)
    assert max_sr_quantile(n, var, 0.95) == pytest.approx(q95_mc, rel=0.01)
    assert expected_max_sr(1, var) == 0.0
    assert expected_max_sr(n, var, rho=0.5) == pytest.approx(expected_max_sr(n, var) * math.sqrt(0.5))


def test_sharpe_moments_and_mertens_variance() -> None:
    rng = np.random.default_rng(1)
    x = 0.001 + 0.01 * rng.standard_normal(2_000)
    st = sharpe_stats(x)
    assert st.sr == pytest.approx(float(np.mean(x) / np.std(x, ddof=1)))
    assert abs(st.skew) < 0.15 and 2.7 < st.kurt < 3.3
    assert st.variance() == pytest.approx(sr_variance(st.sr, st.skew, st.kurt, st.t))
    # normal returns: V[SR] ~ (1 + SR^2 / 2) / (T - 1)
    assert sr_variance(0.1, 0.0, 3.0, 1_001) == pytest.approx((1 + 0.005) / 1_000)
    with pytest.raises(ValueError):
        sharpe_stats([1.0, 1.0, 1.0])


def test_dsr_is_reported_twice_and_the_conservative_one_decides() -> None:
    st = sharpe_stats(0.08 + np.sin(np.arange(300)) * 0.5)  # a strong, non-normal series
    rep = dsr_report(st, n_eff=5, n_total=5_000, var_sr_trials=1.0 / 299)
    assert rep.sr0_total > rep.sr0_eff and rep.dsr_total < rep.dsr_eff
    assert rep.dsr_conservative == rep.dsr_total
    assert rep.passes(0.95) == (rep.dsr_total >= 0.95)
    assert dsr(st, 5, 1.0 / 299) == pytest.approx(rep.dsr_eff)
    with pytest.raises(ValueError):
        dsr_report(st, n_eff=10, n_total=5, var_sr_trials=0.01)


def test_min_sr_grows_with_trials_and_shrinks_with_sample_length_in_period_units() -> None:
    a = min_sr(10, 250, 365, 0.0, 3.0)
    b = min_sr(1_000, 250, 365, 0.0, 3.0)
    c = min_sr(10, 1_000, 365, 0.0, 3.0)
    assert b["min_sr_period"] > a["min_sr_period"] > c["min_sr_period"] > 0
    assert a["min_sr_annualised"] == pytest.approx(annualised(a["min_sr_period"], 365))
    assert psr(a["min_sr_period"], a["sr0_period"], 0.0, 3.0, 250) == pytest.approx(0.95, abs=1e-6)
    table = min_sr_table([10, 100], [250, 500], q=365)
    assert len(table) == 4 and {r["n_eff"] for r in table} == {10.0, 100.0}


def test_annualised_inputs_are_not_mixed_into_period_formulas() -> None:
    # The same daily series: DSR in period units must equal DSR from annualised display values converted
    # back; mixing (annualised SR with daily T) gives a different, wrong answer.
    st = sharpe_stats(0.02 + 0.3 * np.cos(np.arange(500) * 0.7))
    period = dsr(st, 50, st.variance())
    sr_ann = annualised(st.sr, 365)
    back = sr_ann / math.sqrt(365)
    assert back == pytest.approx(st.sr)
    mixed = psr(sr_ann, expected_max_sr(50, st.variance()), st.skew, st.kurt, st.t)
    assert period == pytest.approx(psr(back, expected_max_sr(50, st.variance()), st.skew, st.kurt, st.t))
    assert mixed != pytest.approx(period)


def test_onc_treats_a_perfectly_correlated_group_as_one_trial_and_caps_k_by_sqrt_n() -> None:
    """v2.2 A12 (A3 review note): k started at 2, so a group of identical strategies still counted N_eff = 2, and
    the fixed cap of 30 held N_eff <= 30 for any number of independent trials."""
    same = np.ones((12, 12))
    assert onc_clusters(same).n_eff == 1
    # the cap grows like 2 sqrt(n) but stops at 40: k-means x silhouette costs ~n^2.5 (n=400: 30 -> 45 s)
    assert onc_k_cap(5) == 4 and onc_k_cap(100) == 30 and onc_k_cap(300) == 35 and onc_k_cap(2_500) == 40
