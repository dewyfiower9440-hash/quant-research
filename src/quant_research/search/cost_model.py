"""Per-stage evaluation cost model and peak-memory model (Phase 4 step 4). Pure.

- Time: `cost_s = a + b * rows * ops` per stage (least squares on measured trials); `rows` = panel rows
  evaluated, `ops` = nodes of the expanded signal / entry trees. A stage with fewer than two distinct sizes
  is not fitted (the caller keeps its prior).
- Memory: `peak_mb = c + d * rows` (least squares on measured runs); `guard_memory` refuses BEFORE a run.
- `plan_rows` shrinks the sample (rows) so that a batch fits a time budget.
These are initial models; they are re-fitted from new measurements and judged by `within_band`
(completion criterion: >= 90 % of held-out batches with prediction / actual in [0.5, 1.5])."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from quant_research.errors import AppError
from quant_research.strategy.canonical import Arg, expanded_trees
from quant_research.strategy.spec import Node, StrategySpec

BAND = (0.5, 1.5)


class MemoryBudgetExceeded(AppError):
    """A run whose predicted peak memory exceeds the limit (refused before it starts)."""


@dataclass(frozen=True, slots=True)
class CostSample:
    stage: str
    rows: int
    ops: int
    cost_s: float
    group: str = ""  # the batch (fold) the trial belongs to: held-out checks predict whole batches


@dataclass(frozen=True, slots=True)
class MemorySample:
    rows: int
    peak_mb: float


@dataclass(frozen=True, slots=True)
class LinearCost:
    a: float
    b: float

    def predict(self, rows: int, ops: int) -> float:
        return max(0.0, self.a + self.b * rows * ops)


@dataclass(frozen=True, slots=True)
class LinearMemory:
    c: float
    d: float

    def predict(self, rows: int) -> float:
        return max(0.0, self.c + self.d * rows)


def _ols(x: Sequence[float], y: Sequence[float]) -> tuple[float, float] | None:
    n = len(x)
    if n != len(y):
        raise ValueError(f"x and y differ in length ({n} != {len(y)})")
    if n < 2 or len(set(x)) < 2:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((v - mx) ** 2 for v in x)
    sxy = sum((u - mx) * (v - my) for u, v in zip(x, y, strict=True))
    slope = sxy / sxx
    return my - slope * mx, slope


def fit_cost(samples: Sequence[CostSample]) -> dict[str, LinearCost]:
    out: dict[str, LinearCost] = {}
    for stage in sorted({s.stage for s in samples}):
        mine = [s for s in samples if s.stage == stage]
        fit = _ols([float(s.rows * s.ops) for s in mine], [s.cost_s for s in mine])
        if fit is not None:
            out[stage] = LinearCost(*fit)
    return out


class CostPredictor(Protocol):
    def predict(self, rows: int, ops: int) -> float: ...


HALF_LIFE = 200  # trials (ADR-049 decision 4: the newest trials describe the current machine and panels best)
MIN_BUCKET = 10


@dataclass(frozen=True, slots=True)
class RowCost:
    """v2 (ADR-049 decision 4): `cost_s = a + b * rows` per panel size bucket (log2 of rows), ops ignored (A7:
    the trial cost was a per-panel fixed cost); buckets with too few trials use the stage-wide line."""

    buckets: Mapping[int, tuple[float, float]]
    fallback: tuple[float, float]

    def predict(self, rows: int, ops: int) -> float:
        a, b = self.buckets.get(_bucket(rows), self.fallback)
        return max(0.0, a + b * rows)


def _bucket(rows: int) -> int:
    return max(rows, 1).bit_length()


def _wls(x: Sequence[float], y: Sequence[float]) -> tuple[float, float]:
    """Least squares with recency weights over the given samples (the last weighs 1, HALF_LIFE samples earlier 1/2:
    in a bucket fit the half-life counts that bucket's trials). A single size gives the weighted mean as a fixed
    cost (checked on the values: the weighted mean of equal x differs from x by rounding, review step 6)."""
    n = len(x)
    w = [0.5 ** ((n - 1 - i) / HALF_LIFE) for i in range(n)]
    sw = sum(w)
    my = sum(wi * yi for wi, yi in zip(w, y, strict=True)) / sw
    if len(set(x)) < 2:
        return my, 0.0
    mx = sum(wi * xi for wi, xi in zip(w, x, strict=True)) / sw
    sxx = sum(wi * (xi - mx) ** 2 for wi, xi in zip(w, x, strict=True))
    if sxx <= 0:
        return my, 0.0
    slope = sum(wi * (xi - mx) * (yi - my) for wi, xi, yi in zip(w, x, y, strict=True)) / sxx
    return my - slope * mx, slope


def fit_cost_v2(samples: Sequence[CostSample]) -> dict[str, RowCost]:
    """Per stage, from samples in recording order (oldest first)."""
    out: dict[str, RowCost] = {}
    for stage in sorted({s.stage for s in samples}):
        mine = [s for s in samples if s.stage == stage]
        fallback = _wls([float(s.rows) for s in mine], [s.cost_s for s in mine])
        buckets: dict[int, tuple[float, float]] = {}
        for key in sorted({_bucket(s.rows) for s in mine}):
            part = [s for s in mine if _bucket(s.rows) == key]
            if len(part) >= MIN_BUCKET:
                buckets[key] = _wls([float(s.rows) for s in part], [s.cost_s for s in part])
        out[stage] = RowCost(buckets, fallback)
    return out


def holdout_band(
    samples: Sequence[CostSample], fit: Callable[[Sequence[CostSample]], Mapping[str, CostPredictor]]
) -> float:
    """The completion measure: fit on the first half of the batches (groups, ordered by their first trial; a
    resumed batch keeps its place), predict each later batch's total per stage, and return the share of
    (batch, stage) totals within BAND. A stage the fitted half never saw is a miss (predicted 0), not left out."""
    groups = list(dict.fromkeys(s.group for s in samples))
    if len(groups) < 2:
        raise ValueError(f"a held-out check needs at least two groups, got {len(groups)}")
    cut = len(groups) // 2
    train = set(groups[:cut])
    model = fit([s for s in samples if s.group in train])
    predicted: list[float] = []
    actual: list[float] = []
    for g in groups[cut:]:
        for stage in sorted({s.stage for s in samples if s.group == g}):
            mine = [s for s in samples if s.group == g and s.stage == stage]
            predicted.append(sum(model[stage].predict(s.rows, s.ops) for s in mine) if stage in model else 0.0)
            actual.append(sum(s.cost_s for s in mine))
    return within_band(predicted, actual)


def fit_memory(samples: Sequence[MemorySample]) -> LinearMemory:
    fit = _ols([float(s.rows) for s in samples], [s.peak_mb for s in samples])
    if fit is None:
        raise ValueError("a memory fit needs at least two runs of different sizes")
    return LinearMemory(*fit)


def guard_memory(model: LinearMemory, rows: int, *, limit_mb: float) -> None:
    predicted = model.predict(rows)
    if predicted > limit_mb:
        raise MemoryBudgetExceeded(
            f"predicted peak {predicted:.0f} MB for {rows} rows exceeds the limit {limit_mb:.0f} MB"
        )


def plan_rows(model: LinearCost, *, rows: int, ops: Sequence[int], budget_s: float) -> int:
    """Largest row count <= rows whose predicted batch time fits budget_s (0 if even 0 rows does not)."""
    fixed = model.a * len(ops)
    per_row = model.b * sum(ops)
    if fixed + per_row * rows <= budget_s:
        return rows
    if fixed >= budget_s or per_row <= 0:
        return 0
    return min(rows, int((budget_s - fixed) / per_row))


def within_band(predicted: Sequence[float], actual: Sequence[float]) -> float:
    """Share of batches whose prediction / actual ratio lies in BAND."""
    if len(predicted) != len(actual) or not actual:
        raise ValueError("predicted and actual must be non-empty and of equal length")
    lo, hi = BAND
    hits = sum(1 for p, a in zip(predicted, actual, strict=True) if a > 0 and lo <= p / a <= hi)
    return hits / len(actual)


def _nodes(arg: Arg | None) -> int:
    if isinstance(arg, Node):
        return 1 + sum(_nodes(a) for a in arg.args)
    return 0


def spec_ops(spec: StrategySpec) -> int:
    return sum(_nodes(tree) for tree in expanded_trees(spec).values())
