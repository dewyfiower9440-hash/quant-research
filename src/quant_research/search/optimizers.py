"""Searchers over a pre-registered space (Phase 4 step 6): a Random baseline and TPE (Optuna, pinned).

Both expose the same ask/tell interface. `propose` turns asked points into `Candidate`s exactly like
`strategy.space.generate`, so the S0 stage records every proposal as one trial (constraint and S0 refusals
included) — the accounting invariant "one fitness evaluation = one trial" holds for Optuna's internal
evaluations because Optuna never evaluates anything itself: every `ask` is answered by a `tell` after the
candidate went through the recorded stages. A refused candidate is told as PRUNED (no value)."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, cast

import numpy as np
import optuna
from optuna.distributions import BaseDistribution, CategoricalDistribution, FloatDistribution, IntDistribution
from optuna.samplers import TPESampler
from optuna.trial import Trial, TrialState

from quant_research.strategy.s0 import check
from quant_research.strategy.space import (
    Candidate,
    Choice,
    FloatAxis,
    IntAxis,
    Point,
    Space,
    build_point,
    s0_context,
)

optuna.logging.set_verbosity(optuna.logging.WARNING)

STRUCT_PREFIX = "struct:"


class Searcher(Protocol):
    name: str

    def ask(self) -> Point: ...

    def tell(self, point: Point, score: float | None) -> None:
        """Report the fitness of an asked point; None means the point was refused (constraint / S0 / failure)."""
        ...

    def asked(self) -> int: ...


def distributions(space: Space) -> dict[str, BaseDistribution]:
    out: dict[str, BaseDistribution] = {}
    for axis in space.params:
        if isinstance(axis, IntAxis):
            out[axis.name] = IntDistribution(axis.lo, axis.hi, step=axis.step)
        else:
            out[axis.name] = FloatDistribution(axis.lo, axis.hi, step=axis.step)
    for axis in space.struct:
        out[STRUCT_PREFIX + axis.name] = CategoricalDistribution(list(axis.values()))
    return out


def point_of(space: Space, params: Mapping[str, object]) -> Point:
    """A registered point from Optuna's suggested params (snapped to the axis grid)."""
    nums: dict[str, float] = {}
    for axis in space.params:
        raw = params[axis.name]
        value = float(cast(float, raw))
        if isinstance(axis, FloatAxis):
            value = round(axis.lo + round((value - axis.lo) / axis.step) * axis.step, 10)
        nums[axis.name] = value
    struct = {a.name: cast(Choice, params[STRUCT_PREFIX + a.name]) for a in space.struct}
    return Point.of(space.family, nums, struct)


def candidate_of(space: Space, point: Point) -> Candidate:
    """Constraint and S0 check of a point, as `generate` does; never raises for a registered point."""
    if (why := space.violation(point.params_dict())) is not None:
        return Candidate(point, None, (f"constraint: {why}",))
    spec = build_point(space, point)
    reasons = tuple(check(spec, s0_context(space)))
    return Candidate(point, None if reasons else spec, reasons)


@dataclass
class RandomSearcher:
    """Uniform draws per axis (the baseline); `tell` is accepted and ignored."""

    space: Space
    seed: int
    name: str = "random"
    _rng: np.random.Generator = field(init=False, repr=False)
    _asked: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._rng = np.random.default_rng(self.seed)

    def ask(self) -> Point:
        params = {a.name: a.values()[int(self._rng.integers(len(a.values())))] for a in self.space.params}
        struct = {a.name: a.values()[int(self._rng.integers(len(a.values())))] for a in self.space.struct}
        self._asked += 1
        return Point.of(self.space.family, params, struct)

    def tell(self, point: Point, score: float | None) -> None:
        return None

    def asked(self) -> int:
        return self._asked


@dataclass
class TpeSearcher:
    """Optuna TPE over the registered axes (maximise). Every asked trial must be told exactly once."""

    space: Space
    seed: int
    n_startup_trials: int = 10
    name: str = "tpe"
    _study: optuna.Study = field(init=False, repr=False)
    _pending: dict[Point, list[Trial]] = field(init=False, repr=False)
    _dists: dict[str, BaseDistribution] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        sampler = TPESampler(seed=self.seed, n_startup_trials=self.n_startup_trials, multivariate=True)
        self._study = optuna.create_study(direction="maximize", sampler=sampler)
        self._pending = {}
        self._dists = distributions(self.space)

    def ask(self) -> Point:
        trial = self._study.ask(fixed_distributions=self._dists)
        point = point_of(self.space, cast(Mapping[str, object], trial.params))
        self._pending.setdefault(point, []).append(trial)
        return point

    def tell(self, point: Point, score: float | None) -> None:
        trials = self._pending.get(point)
        if not trials:
            raise KeyError(f"point was not asked (or already told): {point}")
        trial = trials.pop(0)
        if not trials:
            del self._pending[point]
        if score is None or not math.isfinite(score):
            self._study.tell(trial, state=TrialState.PRUNED)
        else:
            self._study.tell(trial, score)

    def asked(self) -> int:
        return len(self._study.trials)

    def pending(self) -> int:
        return sum(len(t) for t in self._pending.values())

    def internal_evaluations(self) -> int:
        """Finished Optuna trials (COMPLETE + PRUNED): must equal the trials the ledger recorded for them."""
        return sum(1 for t in self._study.trials if t.state in (TrialState.COMPLETE, TrialState.PRUNED))


def propose(space: Space, searcher: Searcher, count: int) -> list[Candidate]:
    """`count` candidates from the searcher, checked like `generate` (refusals kept with reasons)."""
    if count < 0:
        raise ValueError(f"count must be >= 0, got {count}")
    return [candidate_of(space, searcher.ask()) for _ in range(count)]


def tell_all(searcher: Searcher, results: Sequence[tuple[Point, float | None]]) -> None:
    for point, score in results:
        searcher.tell(point, score)
