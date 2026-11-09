"""Director policy (design v2.1 §10): which family to research next and how the last failure shapes it.

- Family choice: Thompson sampling on a Beta(1, 1) posterior per family, where a success is a cycle whose
  confirmation-split run passed the Phase 3 bar (not a validation). The posterior decides; an LLM suggestion
  that disagrees is recorded but not followed ("사후분포와 LLM 제안이 다르면 사후분포를 우선").
- `policy=uniform` (ablation switch, §9-9): every eligible family with equal probability.
- Cause codes change the next cycle: weak signal / regime / infeasible -> leave the family; cost erosion /
  capacity / overfit -> stay once with a different grid point (cooldown blocks the same structure);
  missing data -> the family is blocked until the data exists (data_request recorded).
Randomness comes from a seeded generator per (seed, cycle) so a run is reproducible.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np

from quant_research.research.memory import CauseCode
from quant_research.strategy.templates import FAMILIES

PolicyMode = Literal["posterior", "uniform"]
LEAVE: frozenset[CauseCode] = frozenset({"weak_signal", "regime_dependent", "infeasible", "prior_knowledge"})
STAY: frozenset[CauseCode] = frozenset({"cost_erosion", "capacity", "overfit"})


@dataclass(frozen=True, slots=True)
class Choice:
    family: str
    reason: str
    posterior: dict[str, tuple[float, float]]


def choose_family(
    *,
    mode: PolicyMode,
    outcomes: Mapping[str, tuple[int, int]],  # family -> (successes, failures)
    blocked: Iterable[str],
    last_family: str | None,
    last_causes: Sequence[CauseCode],
    consecutive_in_family: int = 0,
    seed: int,
    seq: int,
) -> Choice:
    rng = np.random.default_rng([seed, seq])
    blocked_set = set(blocked)
    eligible = [f for f in FAMILIES if f not in blocked_set]
    if not eligible:
        eligible = list(FAMILIES)  # everything blocked: fall back rather than stall (recorded in the reason)
    post = {f: (1.0 + outcomes.get(f, (0, 0))[0], 1.0 + outcomes.get(f, (0, 0))[1]) for f in FAMILIES}
    causes = set(last_causes)
    stay_ok = consecutive_in_family < 2  # "stay once": at most two cycles in a row on the same family
    if last_family is not None and last_family in eligible and causes & STAY and not causes & LEAVE and stay_ok:
        return Choice(last_family, f"stay in {last_family} once after {sorted(causes & STAY)}", post)
    leave = causes & LEAVE or (causes & STAY and not stay_ok)
    if last_family is not None and leave and len(eligible) > 1 and last_family in eligible:
        eligible.remove(last_family)
    if mode == "uniform":
        pick = eligible[int(rng.integers(len(eligible)))]
        return Choice(pick, f"uniform over {eligible}", post)
    draws = {f: float(rng.beta(*post[f])) for f in eligible}
    pick = max(draws, key=lambda f: draws[f])
    return Choice(pick, f"posterior draw {draws[pick]:.3f} highest among {eligible}", post)
