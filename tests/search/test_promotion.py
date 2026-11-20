"""Phase 4 step 5: fidelity-bias control. The top S1 candidates go to S2, and a seeded 5 % random sample of
the S1 rejects is promoted too (`promoted_random`), so the S1 filter's bias can be measured by the rank
correlation between S1 and S3 scores per family."""

from __future__ import annotations

import math

import pytest

from quant_research.search.promotion import rank_corr, select_for_s2
from quant_research.search.s0_stage import Accepted
from quant_research.search.s1 import S1Result
from quant_research.search.s1_stage import S1Scored
from quant_research.strategy.space import Point

POINT = Point.of("trend", {"fast": 24, "slow": 96}, {})


def _scored(trial_id: int, t: float | None) -> S1Scored:
    res = S1Result(24, None, None, 100, None, None, t, 100, ())  # enough samples: the test is about ranking
    return S1Scored(Accepted(POINT, f"h{trial_id}", f"k{trial_id}", trial_id), res, trial_id)


def test_the_top_k_by_score_are_selected_with_ties_broken_by_trial_id() -> None:
    items = [_scored(1, 0.5), _scored(2, 2.0), _scored(3, 2.0), _scored(4, -1.0), _scored(5, None)]
    sel = select_for_s2(items, top_k=2, promote_rate=0.0, seed=0)
    assert [s.trial_id for s in sel.top] == [2, 3]
    assert sel.promoted == []
    assert sorted(s.trial_id for s in sel.rest) == [1, 4, 5]


def test_an_undefined_score_never_makes_the_top_but_can_be_promoted() -> None:
    items = [_scored(1, None), _scored(2, None)]
    sel = select_for_s2(items, top_k=5, promote_rate=1.0, seed=0)
    assert sel.top == []
    assert [s.trial_id for s in sel.promoted] == [1, 2]


def test_the_random_promotion_rate_matches_five_percent_within_the_binomial_interval() -> None:
    n = 20_000
    items = [_scored(i, float(i % 7)) for i in range(1, n + 2)]
    sel = select_for_s2(items, top_k=1, promote_rate=0.05, seed=123)
    rejects = n
    rate = len(sel.promoted) / rejects
    half = 1.96 * math.sqrt(0.05 * 0.95 / rejects)
    assert 0.05 - half <= rate <= 0.05 + half
    assert all(s.trial_id not in {t.trial_id for t in sel.top} for s in sel.promoted)
    assert sel.seed == 123 and sel.promote_rate == 0.05


def test_the_promotion_is_deterministic_for_a_seed() -> None:
    items = [_scored(i, float(i)) for i in range(1, 500)]
    a = select_for_s2(items, top_k=10, promote_rate=0.05, seed=7)
    b = select_for_s2(items, top_k=10, promote_rate=0.05, seed=7)
    c = select_for_s2(items, top_k=10, promote_rate=0.05, seed=8)
    assert [s.trial_id for s in a.promoted] == [s.trial_id for s in b.promoted]
    assert [s.trial_id for s in a.promoted] != [s.trial_id for s in c.promoted]


@pytest.mark.parametrize(("top_k", "rate"), [(-1, 0.05), (1, -0.01), (1, 1.5)])
def test_malformed_selection_settings_are_rejected(top_k: int, rate: float) -> None:
    with pytest.raises(ValueError):
        select_for_s2([], top_k=top_k, promote_rate=rate, seed=0)


def test_rank_correlation_between_stages() -> None:
    assert rank_corr([1.0, 2.0, 3.0, 4.0], [10.0, 20.0, 30.0, 40.0]) == pytest.approx(1.0)
    assert rank_corr([1.0, 2.0, 3.0, 4.0], [4.0, 3.0, 2.0, 1.0]) == pytest.approx(-1.0)
    assert rank_corr([1.0, 2.0, 3.0], [1.0, 3.0, 2.0]) == pytest.approx(0.5)
    assert rank_corr([1.0, 2.0], [1.0, 2.0]) is None  # too few pairs
    assert rank_corr([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None  # no variation
    with pytest.raises(ValueError):
        rank_corr([1.0], [1.0, 2.0])
