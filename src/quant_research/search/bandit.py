"""Per-family Thompson sampling with discounting and cost-normalised rewards (Phase 4 step 8; design §8).

Reward of one search batch for a family = DSR-deflated performance of the SEPARATE validation split inside
the selection period, divided by the measured cost (seconds). In-sample pass rates are never a reward.
Posterior: normal-normal with known noise scale and an explicit prior (mu0, pseudo-count k0); the sufficient
statistics are discounted by `gamma` at every update (gamma = 1 is the undiscounted sampler), so a family
whose reward regime changed is re-learned instead of being locked in. A per-family minimum budget
(`min_pulls`) is enforced before any posterior draw (design §10 "계열별 최소 할당"). Pure; seeded draws."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

import numpy as np


@dataclass(frozen=True, slots=True)
class BanditPrior:
    mu0: float = 0.0  # prior mean reward (per cost second)
    k0: float = 1.0  # prior pseudo-count
    sigma: float = 1.0  # reward noise scale (known-variance normal model)
    gamma: float = 0.95  # discount per update; 1.0 = no discounting
    min_pulls: float = 1.0  # minimum (discounted) pulls every eligible family must have before sampling

    def __post_init__(self) -> None:
        if self.k0 <= 0 or self.sigma <= 0 or not 0.0 < self.gamma <= 1.0 or self.min_pulls < 0:
            raise ValueError(f"invalid bandit prior {self}")


@dataclass(frozen=True, slots=True)
class ArmState:
    pulls: float = 0.0  # discounted count
    reward_sum: float = 0.0  # discounted sum of rewards

    def posterior(self, prior: BanditPrior) -> tuple[float, float]:
        k = prior.k0 + self.pulls
        mean = (prior.mu0 * prior.k0 + self.reward_sum) / k
        return mean, prior.sigma / math.sqrt(k)


def cost_normalised_reward(performance: float, cost_s: float) -> float:
    """Reward per second of evaluation cost; a family that costs twice as much earns half the reward."""
    if cost_s <= 0:
        raise ValueError(f"cost_s must be > 0, got {cost_s}")
    return performance / cost_s


def deflated_performance(sr_validation: float, sr0: float) -> float:
    """DSR-style deflation in SR units: the validation-split SR net of the expected max under the null
    (`multiplicity.expected_max_sr` of the trials that produced the candidate); floored at 0."""
    return max(0.0, sr_validation - sr0)


@dataclass
class FamilyBandit:
    families: tuple[str, ...]
    prior: BanditPrior = field(default_factory=BanditPrior)
    arms: dict[str, ArmState] = field(default_factory=lambda: dict[str, ArmState]())
    updates: int = 0

    def __post_init__(self) -> None:
        if len(set(self.families)) != len(self.families) or not self.families:
            raise ValueError("families must be distinct and non-empty")
        for f in self.families:
            self.arms.setdefault(f, ArmState())

    def update(self, family: str, reward: float) -> None:
        """Discount every arm, then add this observation (so the newest reward carries weight 1)."""
        if family not in self.arms:
            raise KeyError(f"unknown family {family!r}")
        if not math.isfinite(reward):
            raise ValueError(f"reward must be finite, got {reward}")
        g = self.prior.gamma
        for f, a in self.arms.items():
            self.arms[f] = ArmState(a.pulls * g, a.reward_sum * g)
        a = self.arms[family]
        self.arms[family] = ArmState(a.pulls + 1.0, a.reward_sum + reward)
        self.updates += 1

    def posterior(self) -> dict[str, tuple[float, float]]:
        return {f: self.arms[f].posterior(self.prior) for f in self.families}

    def choose(self, eligible: Sequence[str], *, seed: int, seq: int) -> tuple[str, str]:
        """(family, reason). Under-sampled families first (minimum budget), else the highest posterior draw."""
        rng = np.random.default_rng([seed, seq])
        elig = [f for f in eligible if f in self.arms]
        if not elig:
            raise ValueError("no eligible family")
        starving = [f for f in elig if self.arms[f].pulls < self.prior.min_pulls]
        if starving:
            pick = starving[int(rng.integers(len(starving)))]
            return pick, f"minimum budget: {pick} has {self.arms[pick].pulls:.2f} < {self.prior.min_pulls} pulls"
        draws: dict[str, float] = {}
        for f in elig:
            mean, sd = self.arms[f].posterior(self.prior)
            draws[f] = mean + sd * float(rng.standard_normal())
        pick = max(draws, key=lambda f: draws[f])
        return pick, f"posterior draw {draws[pick]:.4f} highest among {elig}"

    def snapshot(self) -> dict[str, object]:
        return {
            "prior": {
                "mu0": self.prior.mu0,
                "k0": self.prior.k0,
                "sigma": self.prior.sigma,
                "gamma": self.prior.gamma,
                "min_pulls": self.prior.min_pulls,
            },
            "updates": self.updates,
            "arms": {f: {"pulls": a.pulls, "reward_sum": a.reward_sum} for f, a in self.arms.items()},
        }

    @staticmethod
    def restore(families: Sequence[str], prior: BanditPrior, snapshot: Mapping[str, object]) -> FamilyBandit:
        b = FamilyBandit(tuple(families), prior)
        arms_raw = snapshot.get("arms")
        arms: Mapping[str, object] = cast(Mapping[str, object], arms_raw) if isinstance(arms_raw, Mapping) else {}
        for f, raw in arms.items():
            if isinstance(raw, Mapping) and f in b.arms:
                rec = cast(Mapping[str, object], raw)
                b.arms[f] = ArmState(
                    float(cast(float, rec.get("pulls", 0.0))), float(cast(float, rec.get("reward_sum", 0.0)))
                )
        upd = snapshot.get("updates", 0)
        b.updates = int(upd) if isinstance(upd, int) else 0
        return b
