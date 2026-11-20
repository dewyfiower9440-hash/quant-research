"""Holdout decision rule (Phase 5 step 8; design v2.1 §9-2, §9-3, §9-5). Pure.

A submission passes only when ALL hold on the sealed holdout:
  1. H0: SR_holdout <= 0 is rejected at alpha / k (Bonferroni over the epoch's global query budget), with the
     standard error from a stationary block bootstrap (autocorrelated daily pnl, no iid assumption);
  2. the point estimate is >= c * SR_in_sample (pre-registered shrinkage factor, ADR-022: 0.5);
  3. stress: the sign of the SR survives cost x2 / x3 and lag +1 bar / x2 re-evaluations (§9-5: "lag +1봉과
     lag x2를 적용해도 결과가 유지되지 않으면 탈락"); the break-even cost multiple is reported.
The rule's parameters are hashed (`DecisionRule.hash`) and pre-registered with the submission BEFORE the
holdout is read. Research receives one bit; the detail stays inside the validator."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import cast

import numpy as np
from numpy.typing import NDArray

from quant_research.search.multiplicity import psr, sharpe_stats
from quant_research.validator.store import rule_hash

F64 = NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class DecisionRule:
    alpha: float = 0.05
    k: int = 20  # global holdout query budget per epoch (ADR-022)
    c: float = 0.5  # shrinkage factor on the in-sample SR (ADR-022)
    cost_multipliers: tuple[float, ...] = (2.0, 3.0)
    lag_stresses: tuple[str, ...] = ("lag+1", "lagx2")
    block_len: int = 10  # stationary bootstrap mean block length (days)
    bootstrap_reps: int = 2000
    seed: int = 0
    fail_on_cost_stress: bool = True  # the sign must survive every cost multiplier too
    # v2.2 A2 (ADR-042): preconditions; below them the holdout gives NO verdict and spends no query
    min_active_days: int = 60  # days with non-zero pnl
    min_entries: int = 30  # independent entries (a held position's rebalances are not new trades)
    min_mean_exposure: float = 0.05  # mean gross exposure / capital over the holdout run
    min_holdout_days: int = 600  # 250 d exceeded 1.5 x alpha/k (docs/measurements/phase5-verdict-calibration.md)
    require_psr: bool = True  # the skew/kurtosis-aware PSR must reject too
    # the meaning of the lag stresses (null_pipeline.stress_latencies); 2 = lagx2 at least two bars (B8 review)
    stress_version: int = 2

    def __post_init__(self) -> None:
        if (
            not 0 < self.alpha < 1
            or self.k < 1
            or not 0 <= self.c <= 1
            or self.block_len < 1
            or self.bootstrap_reps < 100
            or self.min_active_days < 0
            or self.min_entries < 0
            or self.min_mean_exposure < 0
            or self.min_holdout_days < 3
        ):
            raise ValueError(f"invalid decision rule {self}")

    @property
    def alpha_per_query(self) -> float:
        return self.alpha / self.k

    def hash(self) -> str:
        return rule_hash(cast(Mapping[str, object], asdict(self)))


def sharpe(x: F64) -> float:
    if x.shape[0] < 2:
        return math.nan
    sd = float(np.std(x, ddof=1))
    return float(np.mean(x)) / sd if sd > 0 else math.nan


def stationary_bootstrap_indices(n: int, block_len: int, reps: int, rng: np.random.Generator) -> NDArray[np.int64]:
    """Politis-Romano stationary bootstrap: geometric block lengths with mean `block_len`, circular wrap."""
    p = 1.0 / block_len
    starts = rng.integers(0, n, size=(reps, n))
    new_block = rng.random((reps, n)) < p
    new_block[:, 0] = True
    idx = np.empty((reps, n), dtype=np.int64)
    for t in range(n):
        if t == 0:
            idx[:, 0] = starts[:, 0]
            continue
        cont = (idx[:, t - 1] + 1) % n
        idx[:, t] = np.where(new_block[:, t], starts[:, t], cont)
    return idx


def bootstrap_sr(x: F64, rule: DecisionRule) -> F64:
    """SR replicates under the stationary bootstrap (deterministic for the rule's seed)."""
    n = x.shape[0]
    rng = np.random.default_rng(rule.seed)
    idx = stationary_bootstrap_indices(n, rule.block_len, rule.bootstrap_reps, rng)
    samples = x[idx]
    mean = samples.mean(axis=1)
    sd = samples.std(axis=1, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        sr = np.where(sd > 0, mean / sd, np.nan)
    return np.asarray(sr, dtype=np.float64)


@dataclass(frozen=True, slots=True)
class SrTest:
    sr: float
    se: float
    p_value: float  # one-sided, H0: SR <= 0, bootstrap distribution centred at the null
    alpha: float
    reject: bool


def sr_test(x: F64, rule: DecisionRule) -> SrTest:
    """H0: SR <= 0. The bootstrap distribution of (SR* - SR_hat) approximates SR_hat - SR; the one-sided
    p-value is P(SR* - SR_hat >= SR_hat), i.e. the chance of a value as large as observed when the true SR is 0."""
    if x.shape[0] < 3:
        return SrTest(math.nan, math.nan, 1.0, rule.alpha_per_query, False)
    sr_hat = sharpe(x)
    if math.isnan(sr_hat):
        return SrTest(math.nan, math.nan, 1.0, rule.alpha_per_query, False)
    reps = bootstrap_sr(x, rule)
    reps = reps[~np.isnan(reps)]
    if reps.shape[0] < 10:
        return SrTest(sr_hat, math.nan, 1.0, rule.alpha_per_query, False)
    se = float(np.std(reps, ddof=1))
    centred = reps - float(np.mean(reps))
    p = float((np.count_nonzero(centred >= sr_hat) + 1) / (reps.shape[0] + 1))
    return SrTest(sr_hat, se, p, rule.alpha_per_query, bool(sr_hat > 0 and p < rule.alpha_per_query))


def breakeven_cost_multiple(sr_at_cost: Callable[[float], float], *, lo: float = 1.0, hi: float = 50.0) -> float | None:
    """Smallest cost multiplier in [lo, hi] at which the SR reaches 0 (bisection); None if the SR is already
    <= 0 at lo or still > 0 at hi (reported as '> hi' by the caller)."""
    f_lo, f_hi = sr_at_cost(lo), sr_at_cost(hi)
    if not (f_lo > 0) or f_hi > 0:
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if sr_at_cost(mid) > 0:
            lo = mid
        else:
            hi = mid
    return hi


@dataclass(frozen=True, slots=True)
class Verdict:
    passed: bool
    reasons: tuple[str, ...]  # why it failed (empty when passed); internal
    detail: dict[str, object]  # internal report: never returned to research
    insufficient: bool = False  # a precondition failed: no verdict, the query is not spent (ADR-042)


def preconditions(pnl: F64, rule: DecisionRule, *, entries: float | None, mean_exposure: float | None) -> list[str]:
    """Why this holdout run cannot be judged (empty: it can). Unknown entries / exposure count as failing.
    `mean_exposure` is the mean TARGET gross weight (backtest metric `gross_exposure`), not the realised one
    (A2 review: a short time stop can hold 0 while the target stays on; `min_active_days` covers most of it)."""
    out: list[str] = []
    days = int(pnl.shape[0])
    if days < rule.min_holdout_days:
        out.append(f"holdout days {days} < {rule.min_holdout_days}")
    active = int(np.count_nonzero(pnl))
    if active < rule.min_active_days:
        out.append(f"active pnl days {active} < {rule.min_active_days}")
    if entries is None:
        out.append("entries unknown")
    elif entries < rule.min_entries:
        out.append(f"entries {entries:g} < {rule.min_entries}")
    if mean_exposure is None:
        out.append("mean gross exposure unknown")
    elif mean_exposure < rule.min_mean_exposure:
        out.append(f"mean gross exposure {mean_exposure:.4f} < {rule.min_mean_exposure}")
    return out


def decide(
    holdout_pnl: F64,
    in_sample_sr: float,
    rule: DecisionRule,
    *,
    stressed_sr: Mapping[str, float | None] | None = None,
    breakeven: float | None = None,
    entries: float | None,
    mean_exposure: float | None,
) -> Verdict:
    """The one-bit decision. `stressed_sr` maps a stress name (cost x2.0, cost x3.0, lag+1, lagx2) to the SR of the
    re-evaluated strategy on the holdout (None: the re-evaluation was impossible -> counts as not maintained)."""
    pnl = np.asarray(holdout_pnl, dtype=np.float64)
    missing = preconditions(pnl, rule, entries=entries, mean_exposure=mean_exposure)
    if missing:
        detail0: dict[str, object] = {
            "insufficient": True,
            "rule_hash": rule.hash(),
            "days": int(pnl.shape[0]),
            "active_days": int(np.count_nonzero(pnl)),
            "entries": entries,
            "mean_exposure": mean_exposure,
        }
        return Verdict(False, tuple(missing), detail0, insufficient=True)
    test = sr_test(pnl, rule)
    reasons: list[str] = []
    if not test.reject:
        reasons.append(f"H0 SR<=0 not rejected at alpha/k={rule.alpha_per_query:.5f} (p={test.p_value:.4f})")
    stats = sharpe_stats(pnl)
    psr_value = psr(stats.sr, 0.0, stats.skew, stats.kurt, stats.t) if math.isfinite(stats.sr) else 0.0
    if rule.require_psr and not psr_value > 1 - rule.alpha_per_query:
        reasons.append(f"PSR {psr_value:.5f} <= 1 - alpha/k (skew {stats.skew:.2f}, kurtosis {stats.kurt:.2f})")
    floor = rule.c * in_sample_sr
    if math.isnan(test.sr) or test.sr < floor:
        reasons.append(f"point estimate {test.sr:.4f} < c*SR_in = {floor:.4f}")
    stresses = dict(stressed_sr or {})
    required = [f"cost x{m:g}" for m in rule.cost_multipliers] if rule.fail_on_cost_stress else []
    required += list(rule.lag_stresses)
    for name in required:
        v = stresses.get(name)
        if v is None or not math.isfinite(v) or v <= 0:
            reasons.append(f"stress {name}: SR not maintained ({v})")
    detail: dict[str, object] = {
        "sr_holdout": test.sr,
        "se_bootstrap": test.se,
        "p_one_sided": test.p_value,
        "alpha_per_query": test.alpha,
        "in_sample_sr": in_sample_sr,
        "c_floor": floor,
        "stressed_sr": stresses,
        "breakeven_cost_multiple": breakeven,
        "rule_hash": rule.hash(),
        "days": int(pnl.shape[0]),
        "active_days": int(np.count_nonzero(pnl)),
        "entries": entries,
        "mean_exposure": mean_exposure,
        "psr": psr_value,
        "skew": stats.skew,
        "kurtosis": stats.kurt,
        "insufficient": False,
    }
    return Verdict(not reasons, tuple(reasons), detail)
