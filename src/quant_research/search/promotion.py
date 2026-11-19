"""S1 -> S2 promotion with fidelity-bias control (Phase 4 step 5). Pure.

The `top_k` candidates by S1 score (finite scores only; ties by trial id) are promoted. Each remaining
candidate is ALSO promoted with probability `promote_rate` (seeded; default 5 %) and marked
`promoted_random`, so the bias of the S1 filter can be measured: `rank_corr` of S1 vs S3 scores per family.
Every promoted candidate is evaluated (and counted as a trial) by the later stages like any other."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from quant_research.search.s1_stage import S1Scored

RANDOM_PROMOTION = "promoted_random"
TOP_PROMOTION = "top_k"


@dataclass(frozen=True, slots=True)
class Selection:
    top: list[S1Scored]
    promoted: list[S1Scored]  # random promotions drawn from the non-top candidates
    rest: list[S1Scored]  # not promoted; disjoint from `top` and `promoted`
    seed: int
    promote_rate: float

    def kind(self, trial_id: int) -> str:
        """`top_k` or `promoted_random` for a promoted S1 trial id (KeyError if it was not promoted)."""
        if any(s.trial_id == trial_id for s in self.top):
            return TOP_PROMOTION
        if any(s.trial_id == trial_id for s in self.promoted):
            return RANDOM_PROMOTION
        raise KeyError(f"S1 trial {trial_id} was not promoted")


def select_for_s2(items: Sequence[S1Scored], *, top_k: int, promote_rate: float, seed: int) -> Selection:
    if top_k < 0:
        raise ValueError(f"top_k must be >= 0, got {top_k}")
    if not 0.0 <= promote_rate <= 1.0:
        raise ValueError(f"promote_rate must be in [0, 1], got {promote_rate}")
    finite = sorted(
        (s for s in items if math.isfinite(s.result.score())), key=lambda s: (-s.result.score(), s.trial_id)
    )
    top = finite[:top_k]
    chosen = {s.trial_id for s in top}
    others = sorted((s for s in items if s.trial_id not in chosen), key=lambda s: s.trial_id)
    rng = np.random.default_rng(seed)
    draws = rng.random(len(others))
    promoted = [s for s, u in zip(others, draws, strict=True) if float(u) < promote_rate]
    picked = {s.trial_id for s in promoted}
    rest = [s for s in others if s.trial_id not in picked]
    return Selection(top, promoted, rest, seed, promote_rate)


def _ranks(x: Sequence[float]) -> list[float]:
    order = sorted(range(len(x)), key=lambda i: x[i])
    ranks = [0.0] * len(x)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and x[order[j + 1]] == x[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2.0  # average rank for ties
        i = j + 1
    return ranks


def rank_corr(a: Sequence[float], b: Sequence[float]) -> float | None:
    """Spearman correlation of paired scores (e.g. S1 vs S3 of the same candidates); None if undefined."""
    if len(a) != len(b):
        raise ValueError(f"paired scores differ in length ({len(a)} != {len(b)})")
    if len(a) < 3:
        return None
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    sab = sum((u - ma) * (v - mb) for u, v in zip(ra, rb, strict=True))
    saa = sum((u - ma) ** 2 for u in ra)
    sbb = sum((v - mb) ** 2 for v in rb)
    if saa == 0 or sbb == 0:
        return None
    return sab / math.sqrt(saa * sbb)
