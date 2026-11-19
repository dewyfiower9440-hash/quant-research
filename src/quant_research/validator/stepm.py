"""Romano-Wolf StepM (Phase 5 step 9; design v2.1 §8 "최종 다중검정: StepM 하나만"). Pure.

Hypotheses: H0_i: E[pnl_i] <= 0 for every S2+ trial of the same evaluation window (one multiple-testing
family). Statistics are studentised means; the joint stationary bootstrap (one index set for all series,
so cross-correlation is preserved) gives the distribution of the MAX studentised statistic over the
hypotheses still in play; step-down: reject everything above the 1-alpha quantile, recompute on the rest,
stop when nothing new is rejected. When the family is too large, pre-rule representatives are the medoids
of the ONC clusters (never the best performers, which are already inflated)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from quant_research.search.multiplicity import onc_clusters, pairwise_corr
from quant_research.validator.verdict import stationary_bootstrap_indices

F64 = NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class StepMResult:
    alpha: float
    statistics: tuple[float, ...]  # studentised means, one per hypothesis
    rejected: tuple[int, ...]  # indices rejected (sorted)
    steps: int
    critical_values: tuple[float, ...]  # one per step
    reps: int


def _studentised(pnl: F64, nan_fill: float = 0.0) -> F64:
    x = np.nan_to_num(pnl, nan=nan_fill)
    n = x.shape[0]
    mean = x.mean(axis=0)
    sd = x.std(axis=0, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(sd > 0, mean / (sd / np.sqrt(n)), -np.inf)
    return np.asarray(t, dtype=np.float64)


def stepm(pnl: F64, *, alpha: float = 0.05, block_len: int = 10, reps: int = 1000, seed: int = 0) -> StepMResult:
    """Romano-Wolf step-down on a (T, N) matrix of daily pnl (NaN = no observation that day -> 0)."""
    if pnl.ndim != 2 or pnl.shape[0] < 3 or pnl.shape[1] < 1:
        raise ValueError(f"pnl must be (T >= 3, N >= 1), got {pnl.shape}")
    if not 0 < alpha < 1:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    x = np.nan_to_num(pnl, nan=0.0)
    t_obs = _studentised(x)
    n_obs, n_hyp = x.shape
    rng = np.random.default_rng(seed)
    idx = stationary_bootstrap_indices(n_obs, block_len, reps, rng)
    # bootstrap studentised statistics centred at the sample mean (null re-centring)
    mean = x.mean(axis=0)
    boot = np.empty((reps, n_hyp))
    for r in range(reps):
        s = x[idx[r]]
        sd = s.std(axis=0, ddof=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            boot[r] = np.where(sd > 0, (s.mean(axis=0) - mean) / (sd / np.sqrt(n_obs)), -np.inf)
    remaining = [i for i in range(n_hyp) if np.isfinite(t_obs[i])]
    rejected: list[int] = []
    crits: list[float] = []
    steps = 0
    while remaining:
        steps += 1
        max_stat = boot[:, remaining].max(axis=1)
        crit = float(np.quantile(max_stat, 1 - alpha))
        crits.append(crit)
        new = [i for i in remaining if t_obs[i] > crit]
        if not new:
            break
        rejected += new
        remaining = [i for i in remaining if i not in new]
    return StepMResult(alpha, tuple(float(v) for v in t_obs), tuple(sorted(rejected)), steps, tuple(crits), reps)


def medoid_representatives(pnl: F64, *, max_hypotheses: int, seed: int = 0) -> list[int]:
    """Pre-rule representatives when N > max_hypotheses: the medoid (highest mean correlation to its own
    cluster) of each ONC cluster; chosen by structure, never by performance (§8)."""
    n = pnl.shape[1]
    if n <= max_hypotheses:
        return list(range(n))
    corr = pairwise_corr(pnl)
    cl = onc_clusters(corr, seed=seed)
    out: list[int] = []
    for label in sorted(set(cl.labels)):
        members = [i for i, lab in enumerate(cl.labels) if lab == label]
        if len(members) == 1:
            out.append(members[0])
            continue
        sub = corr[np.ix_(members, members)]
        avg = (sub.sum(axis=1) - 1.0) / (len(members) - 1)
        out.append(members[int(np.argmax(avg))])
    return sorted(out)


def family_wise_error(rejections: Sequence[StepMResult]) -> float:
    """Share of (null) simulations with at least one rejection — the FWER estimate for a test harness."""
    if not rejections:
        return float("nan")
    return sum(1 for r in rejections if r.rejected) / len(rejections)
