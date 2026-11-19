"""Multiple-testing accounting for the search (Phase 4 step 7; design v2.1 §8). Pure, numpy only.

- N_eff: ONC clustering (López de Prado) of the trial correlation matrix — the only N_eff estimator used
  (eigenvalue-based counts are unstable when trials outnumber observations).
- Sharpe statistics stay in PERIOD units (daily when the pnl vector is daily). Annualisation happens only
  for display via `annualised(sr, q)`; the DSR inputs (N, V[SR], skew, kurtosis, T) are never annualised.
- V[SR]: Mertens / Bailey-López de Prado non-normal formula, (1 - gamma3·SR + (gamma4-1)/4·SR²) / (T-1).
- E[max SR] under the null for N trials (Bailey-López de Prado with the Euler-Mascheroni correction), and
  the exact 95 % quantile Φ⁻¹(0.95^(1/N)); equicorrelated trials shrink the max by sqrt(1-rho).
- DSR is reported twice (N_eff over S2+ and N_total including S1) and the approval rule takes the lower.
- `min_sr`: the smallest period SR with DSR >= level; a reference table over (N_eff, T) for reports.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np
from numpy.typing import NDArray

F64 = NDArray[np.float64]
EULER_GAMMA = 0.5772156649015329
_N = NormalDist()


# ----------------------------------------------------------------------------------------------- Sharpe moments
@dataclass(frozen=True, slots=True)
class SharpeStats:
    """Period-unit moments of one return series (T observations)."""

    sr: float
    skew: float  # gamma3
    kurt: float  # gamma4, NOT excess (normal = 3)
    t: int

    def variance(self) -> float:
        return sr_variance(self.sr, self.skew, self.kurt, self.t)


def sharpe_stats(x: Sequence[float] | F64) -> SharpeStats:
    arr = np.asarray(x, dtype=np.float64)
    t = int(arr.shape[0])
    if t < 3:
        raise ValueError(f"need at least 3 observations, got {t}")
    m = float(np.mean(arr))
    d = arr - m
    s2 = float(np.dot(d, d)) / (t - 1)
    if s2 <= 0:
        raise ValueError("the series has no variance")
    s = math.sqrt(s2)
    skew = float(np.mean(d**3)) / s**3
    kurt = float(np.mean(d**4)) / s**4
    return SharpeStats(m / s, skew, kurt, t)


def sr_variance(sr: float, skew: float, kurt: float, t: int) -> float:
    """Mertens (1 - gamma3·SR + (gamma4-1)/4·SR²) / (T-1); gamma4 non-excess. Floored at a tiny positive value."""
    if t < 2:
        raise ValueError(f"T must be >= 2, got {t}")
    return max(1e-12, (1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr) / (t - 1))


def annualised(sr_period: float, q: int) -> float:
    """Display only: period SR x sqrt(q), q = observations per year (365 for daily)."""
    if q <= 0:
        raise ValueError(f"q must be > 0, got {q}")
    return sr_period * math.sqrt(q)


# ----------------------------------------------------------------------------------------------- max of N trials
def expected_max_sr(n: int, var_sr: float, *, rho: float = 0.0) -> float:
    """E[max of N SR estimates] under SR = 0 (Bailey-LdP): sqrt(V)·((1-gamma)Φ⁻¹(1-1/N) + gammaΦ⁻¹(1-1/(N·e)));
    for equicorrelated trials (rho) the max shrinks by sqrt(1-rho)."""
    if n < 1:
        raise ValueError(f"N must be >= 1, got {n}")
    if not 0.0 <= rho < 1.0:
        raise ValueError(f"rho must be in [0, 1), got {rho}")
    if n == 1:
        return 0.0
    z = (1 - EULER_GAMMA) * _N.inv_cdf(1 - 1 / n) + EULER_GAMMA * _N.inv_cdf(1 - 1 / (n * math.e))
    return math.sqrt(var_sr) * math.sqrt(1 - rho) * z


def max_sr_quantile(n: int, var_sr: float, level: float, *, rho: float = 0.0) -> float:
    """Exact quantile of the max of N iid N(0, V) estimates: sqrt(V)·Φ⁻¹(level^(1/N)) (xsqrt(1-rho))."""
    if n < 1 or not 0.0 < level < 1.0:
        raise ValueError(f"need N >= 1 and 0 < level < 1 (N={n}, level={level})")
    return math.sqrt(var_sr) * math.sqrt(1 - rho) * _N.inv_cdf(level ** (1 / n))


def mc_max_sr(n: int, var_sr: float, *, reps: int, seed: int) -> tuple[float, float]:
    """Monte-Carlo (E[max], 95 % quantile) of the max of N iid N(0, V) draws — the test oracle."""
    rng = np.random.default_rng(seed)
    mx = np.max(rng.standard_normal((reps, n)), axis=1) * math.sqrt(var_sr)
    return float(np.mean(mx)), float(np.quantile(mx, 0.95))


# ----------------------------------------------------------------------------------------------- PSR / DSR
def psr(sr: float, sr0: float, skew: float, kurt: float, t: int) -> float:
    """Probabilistic SR: P(true SR > sr0) = Φ((SR - SR0)·sqrt(T-1) / sqrt(1 - gamma3·SR + (gamma4-1)/4·SR²))."""
    denom = math.sqrt(max(1e-12, 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr))
    return _N.cdf((sr - sr0) * math.sqrt(t - 1) / denom)


def dsr(stats: SharpeStats, n_trials: int, var_sr_trials: float, *, rho: float = 0.0) -> float:
    """Deflated SR: PSR against SR0 = E[max of n_trials] (period units throughout)."""
    return psr(stats.sr, expected_max_sr(n_trials, var_sr_trials, rho=rho), stats.skew, stats.kurt, stats.t)


@dataclass(frozen=True, slots=True)
class DsrReport:
    sr: float  # period units
    t: int
    n_eff: int
    n_total: int
    sr0_eff: float
    sr0_total: float
    dsr_eff: float
    dsr_total: float

    @property
    def dsr_conservative(self) -> float:
        return min(self.dsr_eff, self.dsr_total)

    def passes(self, level: float) -> bool:
        return self.dsr_conservative >= level


def dsr_report(stats: SharpeStats, *, n_eff: int, n_total: int, var_sr_trials: float, rho: float = 0.0) -> DsrReport:
    """DSR twice (§8): against N_eff (S2+ clusters) and against N_total (S1 included); approval uses the lower."""
    if n_total < n_eff or n_eff < 1:
        raise ValueError(f"need 1 <= n_eff <= n_total, got n_eff={n_eff}, n_total={n_total}")
    sr0_eff = expected_max_sr(n_eff, var_sr_trials, rho=rho)
    sr0_total = expected_max_sr(n_total, var_sr_trials, rho=rho)
    return DsrReport(
        stats.sr,
        stats.t,
        n_eff,
        n_total,
        sr0_eff,
        sr0_total,
        psr(stats.sr, sr0_eff, stats.skew, stats.kurt, stats.t),
        psr(stats.sr, sr0_total, stats.skew, stats.kurt, stats.t),
    )


def min_sr(
    n_eff: int, t_split: int, q: int, skew: float, kurt: float, rho: float = 0.0, *, level: float = 0.95
) -> dict[str, float]:
    """Smallest period SR whose DSR against E[max of n_eff] reaches `level` (bisection), plus its annualised
    display value. V[SR] of the trials is evaluated at SR = 0 (the null), as in the E[max] derivation."""
    if t_split < 3:
        raise ValueError(f"T_split must be >= 3, got {t_split}")
    var0 = sr_variance(0.0, skew, kurt, t_split)
    sr0 = expected_max_sr(n_eff, var0, rho=rho)
    lo, hi = sr0, sr0 + 10.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if psr(mid, sr0, skew, kurt, t_split) >= level:
            hi = mid
        else:
            lo = mid
    return {"sr0_period": sr0, "min_sr_period": hi, "min_sr_annualised": annualised(hi, q), "level": level}


def min_sr_table(
    n_effs: Sequence[int], t_splits: Sequence[int], *, q: int, skew: float = 0.0, kurt: float = 3.0, rho: float = 0.0
) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    for n in n_effs:
        for t in t_splits:
            rows.append({"n_eff": float(n), "t_split": float(t), **min_sr(n, t, q, skew, kurt, rho)})
    return rows


# ----------------------------------------------------------------------------------------------- correlation / ONC
def pairwise_corr(pnl: F64) -> F64:
    """Correlation of columns with NaN-aware pairwise overlap (>= 3 common rows, else 0); diagonal 1."""
    n = pnl.shape[1]
    out = np.eye(n)
    for i in range(n):
        for j in range(i + 1, n):
            mask = ~(np.isnan(pnl[:, i]) | np.isnan(pnl[:, j]))
            if int(np.count_nonzero(mask)) < 3:
                continue
            a, b = pnl[mask, i], pnl[mask, j]
            sa, sb = float(np.std(a)), float(np.std(b))
            if sa <= 0 or sb <= 0:
                continue
            c = float(np.mean((a - a.mean()) * (b - b.mean()))) / (sa * sb)
            out[i, j] = out[j, i] = max(-1.0, min(1.0, c))
    return out


def _kmeans(x: F64, k: int, rng: np.random.Generator, *, iters: int = 100) -> NDArray[np.int64]:
    n = x.shape[0]
    centroids = x[rng.choice(n, size=k, replace=False)].copy()
    labels = np.zeros(n, dtype=np.int64)
    for _ in range(iters):
        d = ((x[:, None, :] - centroids[None, :, :]) ** 2).sum(axis=2)
        new = np.asarray(np.argmin(d, axis=1), dtype=np.int64)
        for c in range(k):  # keep every cluster non-empty: move the farthest point into an empty one
            if not np.any(new == c):
                far = int(np.argmax(np.min(d, axis=1)))
                new[far] = c
        if np.array_equal(new, labels):
            break
        labels = new
        for c in range(k):
            centroids[c] = x[labels == c].mean(axis=0)
    return labels


def silhouette(dist: F64, labels: NDArray[np.int64]) -> F64:
    n = dist.shape[0]
    s = np.zeros(n)
    for i in range(n):
        lab = [int(v) for v in labels]
        same = np.array([lab[j] == lab[i] and j != i for j in range(n)], dtype=np.bool_)
        if int(np.count_nonzero(same)) == 0:
            continue  # singleton: 0
        a = float(dist[i, same].mean())
        b = min(float(dist[i, labels == c].mean()) for c in np.unique(labels) if c != labels[i])
        s[i] = (b - a) / max(a, b) if max(a, b) > 0 else 0.0
    return s


@dataclass(frozen=True, slots=True)
class Clustering:
    labels: tuple[int, ...]
    k: int
    quality: float  # mean(silhouette) / std(silhouette)

    @property
    def n_eff(self) -> int:
        return self.k


def onc_clusters(corr: F64, *, seed: int = 0, max_k: int | None = None, n_init: int = 10) -> Clustering:
    """ONC: k-means on the correlation distance sqrt(0.5·(1-rho)) for k = 2..max_k, pick the k (and init)
    with the highest silhouette quality mean/std. Fewer than 3 trials form one cluster (N_eff = N)."""
    n = corr.shape[0]
    if corr.shape != (n, n):
        raise ValueError(f"corr must be square, got {corr.shape}")
    if n < 3:
        return Clustering(tuple(range(n)), n, math.inf)
    dist = np.sqrt(np.clip(0.5 * (1.0 - corr), 0.0, 1.0))
    np.fill_diagonal(dist, 0.0)
    rng = np.random.default_rng(seed)
    best: tuple[float, int, NDArray[np.int64]] | None = None
    top = n - 1 if max_k is None else min(max_k, n - 1)
    for k in range(2, top + 1):
        for _ in range(n_init):
            labels = _kmeans(dist, k, rng)
            s = silhouette(dist, labels)
            # mean / std; the tiny floor keeps exact block structures (std 0) comparable by their mean
            q = float(np.mean(s)) / (float(np.std(s)) + 1e-9)
            if best is None or q > best[0]:
                best = (q, k, labels)
    if best is None:  # top < 2: nothing to compare, every trial is its own cluster
        return Clustering(tuple(range(n)), n, math.inf)
    q, k, labels = best
    # canonical label order: by first appearance
    order: dict[int, int] = {}
    canon = tuple(order.setdefault(int(v), len(order)) for v in labels)
    return Clustering(canon, k, q)


def average_offdiag(corr: F64) -> float:
    n = corr.shape[0]
    if n < 2:
        return 0.0
    return float((corr.sum() - np.trace(corr)) / (n * (n - 1)))
