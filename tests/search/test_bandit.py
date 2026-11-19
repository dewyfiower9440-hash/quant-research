"""Phase 4 step 8: discounted, cost-normalised Thompson sampling over families with a minimum budget."""

from __future__ import annotations

import numpy as np
import pytest

from quant_research.search.bandit import (
    BanditPrior,
    FamilyBandit,
    cost_normalised_reward,
    deflated_performance,
)

FAMS = ("a", "b", "c")


def _simulate(
    bandit: FamilyBandit,
    means: dict[str, float],
    steps: int,
    seed: int,
    *,
    switch_at: int | None = None,
    switched: dict[str, float] | None = None,
) -> list[str]:
    rng = np.random.default_rng(seed)
    picks: list[str] = []
    for t in range(steps):
        m = means if switch_at is None or t < switch_at else (switched or means)
        f, _ = bandit.choose(FAMS, seed=seed, seq=t)
        bandit.update(f, m[f] + 0.1 * float(rng.standard_normal()))
        picks.append(f)
    return picks


def test_converges_to_the_best_family_under_fixed_rewards() -> None:
    b = FamilyBandit(FAMS, BanditPrior(gamma=1.0, sigma=0.3))
    picks = _simulate(b, {"a": 0.2, "b": 0.5, "c": 0.3}, 400, seed=1)
    assert picks[-200:].count("b") >= 150
    post = b.posterior()
    assert post["b"][0] > post["c"][0] > post["a"][0]
    assert post["b"][1] < post["a"][1]  # the most pulled arm has the tightest posterior


def test_discounting_adapts_faster_than_the_undiscounted_sampler_after_a_reward_switch() -> None:
    before = {"a": 0.6, "b": 0.2, "c": 0.2}
    after = {"a": 0.2, "b": 0.6, "c": 0.2}
    disc = FamilyBandit(FAMS, BanditPrior(gamma=0.9, sigma=0.3))
    flat = FamilyBandit(FAMS, BanditPrior(gamma=1.0, sigma=0.3))
    p_disc = _simulate(disc, before, 600, seed=2, switch_at=300, switched=after)
    p_flat = _simulate(flat, before, 600, seed=2, switch_at=300, switched=after)
    window = slice(300, 400)
    assert p_disc[window].count("b") > p_flat[window].count("b")
    assert p_disc[-100:].count("b") >= 70


def test_a_family_costing_twice_as_much_earns_half_the_reward() -> None:
    assert cost_normalised_reward(0.4, 2.0) == pytest.approx(cost_normalised_reward(0.4, 1.0) / 2)
    assert deflated_performance(0.3, 0.1) == pytest.approx(0.2)
    assert deflated_performance(0.05, 0.1) == 0.0  # below the null's expected max: no reward
    with pytest.raises(ValueError):
        cost_normalised_reward(1.0, 0.0)


def test_every_eligible_family_gets_its_minimum_budget_first() -> None:
    b = FamilyBandit(FAMS, BanditPrior(min_pulls=2.0, gamma=1.0))
    picks: list[str] = []
    for t in range(6):
        f, reason = b.choose(FAMS, seed=0, seq=t)
        assert reason.startswith("minimum budget")
        b.update(f, 1.0 if f == "a" else 0.0)
        picks.append(f)
    assert sorted(picks) == ["a", "a", "b", "b", "c", "c"]
    _, reason = b.choose(FAMS, seed=0, seq=6)
    assert reason.startswith("posterior draw")
    assert b.choose(("b",), seed=0, seq=7)[0] == "b"  # ineligible families are never chosen
    with pytest.raises(ValueError):
        b.choose((), seed=0, seq=8)


def test_snapshot_restores_the_same_decisions_and_rejects_bad_input() -> None:
    b = FamilyBandit(FAMS, BanditPrior(gamma=0.8))
    for t, f in enumerate(["a", "b", "b", "c"]):
        b.update(f, float(t))
    snap = b.snapshot()
    r = FamilyBandit.restore(FAMS, BanditPrior(gamma=0.8), snap)
    assert r.posterior() == b.posterior() and r.updates == 4
    assert r.choose(FAMS, seed=3, seq=9) == b.choose(FAMS, seed=3, seq=9)
    with pytest.raises(KeyError):
        b.update("zzz", 1.0)
    with pytest.raises(ValueError):
        b.update("a", float("nan"))
    with pytest.raises(ValueError):
        BanditPrior(gamma=0.0)
    with pytest.raises(ValueError):
        FamilyBandit(("a", "a"))
