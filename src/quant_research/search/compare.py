"""Random vs TPE under the same cost budget on a synthetic landscape with a planted optimum (Phase 4 step 6).

Pure. The landscape is a smooth bump around a planted point in normalised axis coordinates plus seeded
noise; a constraint violation is refused (None) like the real S0. Cost per evaluation comes from the
step-4 cost model (`a + b * rows * ops`, ops of the candidate's spec), so a searcher that proposes heavier
specs burns its budget faster. Metrics: recovery (did the run find a score within `tolerance` of the planted
optimum), best score, evaluations and cost. FWER and the N_eff increment are reported by the step-7
multiple-testing module once that exists; this module reports recovery and cost only."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

from quant_research.search.cost_model import LinearCost, spec_ops
from quant_research.search.optimizers import RandomSearcher, Searcher, TpeSearcher, candidate_of
from quant_research.strategy.space import Candidate, Choice, Point, Space

Objective = Callable[[Candidate], float | None]


@dataclass(frozen=True, slots=True)
class Landscape:
    space: Space
    target: Point
    width: float  # bump width in normalised [0, 1] axis units
    noise: float
    seed: int

    def _coords(self, point: Point) -> list[float]:
        out: list[float] = []
        params = point.params_dict()
        for axis in self.space.params:
            values = axis.values()
            out.append(values.index(params[axis.name]) / max(1, len(values) - 1))
        struct = point.struct_dict()
        for axis in self.space.struct:
            choices: tuple[Choice, ...] = axis.values()
            out.append(choices.index(struct[axis.name]) / max(1, len(choices) - 1))
        return out

    def score(self, cand: Candidate) -> float | None:
        if cand.spec is None:
            return None
        d = np.asarray(self._coords(cand.point)) - np.asarray(self._coords(self.target))
        base = math.exp(-float(np.dot(d, d)) / (2 * self.width**2))
        if self.noise <= 0:
            return base
        rng = np.random.default_rng((self.seed, hash(cand.point) & 0xFFFFFFFF))
        return base + self.noise * float(rng.standard_normal())


def planted_landscape(space: Space, *, seed: int, width: float = 0.25, noise: float = 0.05) -> Landscape:
    """A landscape whose planted optimum is a registered point that passes the constraint and S0."""
    rng = np.random.default_rng(seed)
    for _ in range(10_000):
        params = {a.name: a.values()[int(rng.integers(len(a.values())))] for a in space.params}
        struct = {a.name: a.values()[int(rng.integers(len(a.values())))] for a in space.struct}
        point = Point.of(space.family, params, struct)
        if candidate_of(space, point).spec is not None:
            return Landscape(space, point, width, noise, seed)
    raise ValueError(f"{space.family}: no feasible planted optimum found")


@dataclass(frozen=True, slots=True)
class SearchRun:
    searcher: str
    seed: int
    evaluations: int  # asked and told (refusals included: they are trials too)
    refused: int
    distinct_points: int  # feasible, distinct candidates explored (how much of the space the budget bought)
    cost_s: float
    best: float | None
    best_point: Point | None

    def recovered(self, optimum: float, tolerance: float) -> bool:
        return self.best is not None and self.best >= optimum - tolerance


def run_search(
    searcher: Searcher, space: Space, objective: Objective, cost: LinearCost, *, rows: int, budget_s: float
) -> SearchRun:
    """Ask/tell until the predicted cumulative cost would exceed the budget. Every ask is told once."""
    spent = 0.0
    n = refused = 0
    seen: set[Point] = set()
    best: float | None = None
    best_point: Point | None = None
    while True:
        point = searcher.ask()
        cand = candidate_of(space, point)
        ops = 0 if cand.spec is None else spec_ops(cand.spec)
        step_cost = cost.predict(rows, ops) if cand.spec is not None else cost.a
        if spent + step_cost > budget_s:
            searcher.tell(point, None)  # asked, so it is told; the run stops before evaluating it
            break
        score = objective(cand)
        searcher.tell(point, score)
        spent += step_cost
        n += 1
        if score is None:
            refused += 1
        else:
            seen.add(point)
            if best is None or score > best:
                best, best_point = score, point
    return SearchRun(searcher.name, getattr(searcher, "seed", -1), n, refused, len(seen), spent, best, best_point)


@dataclass(frozen=True, slots=True)
class Comparison:
    family: str
    budget_s: float
    optimum: float
    tolerance: float
    random: tuple[SearchRun, ...]
    tpe: tuple[SearchRun, ...]

    def recovery(self, runs: Sequence[SearchRun]) -> float:
        return sum(1 for r in runs if r.recovered(self.optimum, self.tolerance)) / len(runs) if runs else math.nan

    def summary(self) -> dict[str, object]:
        return {
            "family": self.family,
            "budget_s": self.budget_s,
            "seeds": len(self.random),
            "random": {
                "recovery": self.recovery(self.random),
                "evaluations": [r.evaluations for r in self.random],
                "distinct_points": [r.distinct_points for r in self.random],
                "best": [r.best for r in self.random],
            },
            "tpe": {
                "recovery": self.recovery(self.tpe),
                "evaluations": [r.evaluations for r in self.tpe],
                "distinct_points": [r.distinct_points for r in self.tpe],
                "best": [r.best for r in self.tpe],
            },
        }


def compare(
    space: Space,
    landscape: Landscape,
    cost: LinearCost,
    *,
    rows: int,
    budget_s: float,
    seeds: Sequence[int],
    tolerance: float = 0.1,
    n_startup_trials: int = 10,
) -> Comparison:
    """Same landscape, same cost budget, same seeds for both searchers."""
    optimum = 1.0  # the bump's peak (noise excluded)
    random_runs = tuple(
        run_search(RandomSearcher(space, s), space, landscape.score, cost, rows=rows, budget_s=budget_s) for s in seeds
    )
    tpe_runs = tuple(
        run_search(
            TpeSearcher(space, s, n_startup_trials=n_startup_trials),
            space,
            landscape.score,
            cost,
            rows=rows,
            budget_s=budget_s,
        )
        for s in seeds
    )
    return Comparison(space.family, budget_s, optimum, tolerance, random_runs, tpe_runs)
