"""Per-stage evaluation cost model and peak-memory model (Phase 4 step 4). Pure.

- Time: `cost_s = a + b * rows * ops` per stage (least squares on measured trials); `rows` = panel rows
  evaluated, `ops` = nodes of the expanded signal / entry trees. A stage with fewer than two distinct sizes
  is not fitted (the caller keeps its prior).
- Memory: `peak_mb = c + d * rows` (least squares on measured runs); `guard_memory` refuses BEFORE a run.
- `plan_rows` shrinks the sample (rows) so that a batch fits a time budget.
These are initial models; they are re-fitted from new measurements and judged by `within_band`
(completion criterion: >= 90 % of held-out batches with prediction / actual in [0.5, 1.5])."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

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
