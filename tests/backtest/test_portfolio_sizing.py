"""v2.5 step 8a (ADR-059): the portfolio-level volatility target. The names with a signal share the target under one
average pairwise correlation; a per-name cap (hard limit D-09) and the gross cap come after."""

from __future__ import annotations

import math

import numpy as np
import numpy.typing as npt
import pytest

from quant_research.backtest.market import (
    NAME_CAP,
    RHO_MAX,
    mean_pairwise_correlation,
    portfolio_warmup_bars,
    portfolio_weights,
    rho_min_bars,
)

F64 = npt.NDArray[np.float64]


def _desire(rows: list[list[int]]) -> npt.NDArray[np.int8]:
    return np.array(rows, dtype=np.int8)


def _weights(rows: list[list[int]], sigma: list[float], rho: float, *, target: float = 0.05, lev: float = 1.0) -> F64:
    d = _desire(rows)
    sig = np.tile(np.array(sigma, dtype=np.float64), (d.shape[0], 1))
    return portfolio_weights(d, sig, np.full(d.shape[0], rho), target=target, max_leverage=lev)


def _book_vol(w: F64, sigma: list[float], rho: float) -> float:
    """Volatility of a weight row under the equicorrelation model the sizing assumes."""
    s = np.array(sigma)
    cov = np.outer(s, s) * (rho + (1.0 - rho) * np.eye(len(sigma)))
    return float(math.sqrt(w @ cov @ w))


def test_a_single_name_takes_the_whole_target_up_to_the_name_cap() -> None:
    w = _weights([[1, 0, 0]], [0.8, 0.8, 0.8], 0.5, target=0.05)
    assert w[0].tolist() == pytest.approx([0.05 / 0.8, 0.0, 0.0])
    capped = _weights([[1, 0, 0]], [0.8, 0.8, 0.8], 0.5, target=0.15)
    assert capped[0, 0] == NAME_CAP == 0.10  # 0.15 / 0.8 would be 0.1875: the per-name hard limit holds
    short = _weights([[0, -1, 0]], [0.8, 0.8, 0.8], 0.5, target=0.15)
    assert short[0].tolist() == [0.0, -NAME_CAP, 0.0]


@pytest.mark.parametrize("rho", [0.0, 0.3, 0.9])
@pytest.mark.parametrize("rows", [[[1, 1, 1, 1]], [[1, 1, 0, -1]], [[1, -1, 1, -1]], [[-1, -1, 0, 0]]])
def test_the_book_volatility_is_the_target_under_the_assumed_correlation(rows: list[list[int]], rho: float) -> None:
    sigma = [0.6, 0.9, 1.2, 1.5]
    w = _weights(rows, sigma, rho, target=0.02)  # small enough that no cap binds
    assert np.max(np.abs(w)) < NAME_CAP
    assert _book_vol(w[0], sigma, rho) == pytest.approx(0.02)
    assert np.sign(w[0]).tolist() == rows[0]  # a name without a signal has no weight, the others keep their side


def test_equal_longs_and_shorts_scale_by_the_uncorrelated_part_only() -> None:
    w = _weights([[1, -1, 1, -1]], [1.0, 1.0, 1.0, 1.0], 0.75, target=0.02)
    assert np.abs(w[0]).tolist() == pytest.approx([0.02 / math.sqrt(4 * 0.25)] * 4)  # v = sqrt(k (1 - rho))


def test_fewer_names_with_a_signal_carry_more_weight_each() -> None:
    sigma = [1.0] * 10
    one = _weights([[1] + [0] * 9], sigma, 0.5, target=0.05)[0, 0]
    ten = _weights([[1] * 10], sigma, 0.5, target=0.05)[0, 0]
    assert one == pytest.approx(0.05) and ten == pytest.approx(0.05 / math.sqrt(10 + 0.5 * 90))
    assert one > 7 * ten  # the old rule gave the single name a tenth of the target instead


def test_the_gross_cap_scales_the_whole_row_after_the_name_cap() -> None:
    w = _weights([[1] * 6], [0.2] * 6, 0.0, target=0.15, lev=0.3)
    assert np.abs(w[0]).tolist() == pytest.approx([0.05] * 6)  # each name capped at 0.10 first, then 0.6 -> 0.3
    assert float(np.abs(w).sum()) == pytest.approx(0.3)


def test_no_weight_without_a_correlation_or_a_volatility_estimate() -> None:
    d = _desire([[1, -1, 0], [1, -1, 0], [0, 0, 0]])
    sigma = np.array([[1.0, 1.0, 1.0], [1.0, math.nan, 1.0], [1.0, 1.0, 1.0]])
    w = portfolio_weights(d, sigma, np.array([math.nan, 0.5, 0.5]), target=0.05, max_leverage=1.0)
    assert w[0].tolist() == [0.0, 0.0, 0.0]  # no correlation estimate on this bar
    assert w[1].tolist() == pytest.approx([0.05, 0.0, 0.0])  # the name without a volatility is not counted in k
    assert w[2].tolist() == [0.0, 0.0, 0.0]  # nothing has a signal
    assert np.isfinite(w).all()


def _correlated(rng: np.random.Generator, bars: int, names: int, rho: float) -> F64:
    common = rng.standard_normal((bars, 1))
    own = rng.standard_normal((bars, names))
    return math.sqrt(rho) * common + math.sqrt(1.0 - rho) * own  # unit variance, pairwise correlation rho


@pytest.mark.parametrize("rho", [0.0, 0.3, 0.7])
def test_the_average_pairwise_correlation_is_recovered_from_standardised_returns(rho: float) -> None:
    x = _correlated(np.random.default_rng(7), 4_000, 30, rho)
    est = mean_pairwise_correlation(x, np.ones(x.shape, dtype=np.bool_), 2_000)
    assert est[-1] == pytest.approx(rho, abs=0.05)


def test_the_correlation_needs_its_minimum_bars_and_never_reads_later_rows() -> None:
    rng = np.random.default_rng(3)
    x = _correlated(rng, 120, 8, 0.4)
    member = np.ones(x.shape, dtype=np.bool_)
    est = mean_pairwise_correlation(x, member, 40)
    need = rho_min_bars(40)
    assert need == 10 and rho_min_bars(8) == 5
    assert np.isnan(est[: need - 1]).all() and np.isfinite(est[need - 1 :]).all()
    later = x.copy()
    later[60:] = _correlated(rng, 60, 8, 0.9)
    changed = mean_pairwise_correlation(later, member, 40)
    assert np.array_equal(changed[:60], est[:60], equal_nan=True)  # point-in-time: rows < 60 saw nothing new
    assert not np.array_equal(changed[60:], est[60:])  # (the later rows did change)
    assert portfolio_warmup_bars(60) == 75 and portfolio_warmup_bars(20) == 25


def test_the_correlation_is_clipped_and_counts_members_with_a_value_only() -> None:
    bars = 50
    opposite = np.tile(np.array([[1.0, -1.0]]), (bars, 1))  # two names always opposite: -1 before the clip
    assert mean_pairwise_correlation(opposite, np.ones(opposite.shape, dtype=np.bool_), 20)[-1] == 0.0
    same = np.ones((bars, 3))
    assert mean_pairwise_correlation(same, np.ones(same.shape, dtype=np.bool_), 20)[-1] == RHO_MAX == 0.95
    # a third name that moves against the two is not a member: it must not enter the cross-section
    x = np.column_stack([np.ones(bars), np.ones(bars), -np.ones(bars)])
    member = np.column_stack([np.ones(bars, bool), np.ones(bars, bool), np.zeros(bars, bool)])
    assert mean_pairwise_correlation(x, member, 20)[-1] == RHO_MAX
    assert mean_pairwise_correlation(x, np.ones(x.shape, dtype=np.bool_), 20)[-1] == 0.0  # (-1/3 before the clip)
    lonely = np.column_stack([np.ones(bars), np.full(bars, math.nan)])  # one value per bar: no pair, no estimate
    assert np.isnan(mean_pairwise_correlation(lonely, np.ones(lonely.shape, dtype=np.bool_), 20)).all()
