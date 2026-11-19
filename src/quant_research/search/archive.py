"""MAP-Elites archive (Phase 4 step 9; design §8 "아카이브 elite에는 수축 추정", §10 "다양성"). Pure.

Behaviour descriptors (pre-registered bins): turnover (traded notional / capital over the window), average
holding time in hours, net exposure (mean signed weight). One elite per cell. Replacement is deterministic:
the candidate's SHRUNKEN score must exceed the incumbent's (ties keep the lower trial id). A candidate whose
probe (net target weight sampled at fixed decision points, `probe_vector`) correlates above `corr_cap` with
an existing elite is a semantic duplicate: it is refused, or replaces that elite when its shrunken score is
higher. Shrinkage pulls a score towards the archive-wide prior by its sample size: n / (n + k0)."""

from __future__ import annotations

import bisect
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

F64 = NDArray[np.float64]
PROBE_POINTS = 64

# Pre-registered descriptor bins (upper edges; the last bin is open). Initial priors, changed only by ADR.
TURNOVER_EDGES: tuple[float, ...] = (2.0, 10.0, 30.0, 100.0)
HOLDING_HOURS_EDGES: tuple[float, ...] = (2.0, 8.0, 24.0, 72.0, 168.0)
NET_EXPOSURE_EDGES: tuple[float, ...] = (-0.5, -0.1, 0.1, 0.5)


def exposure_metrics(weight: F64) -> dict[str, float]:
    """Mean signed and gross exposure of a (T, N) target-weight matrix (NaN weights count as flat)."""
    if weight.ndim != 2 or weight.shape[0] == 0:
        return {"net_exposure": 0.0, "gross_exposure": 0.0}
    w = np.nan_to_num(weight, nan=0.0)
    return {
        "net_exposure": float(np.mean(np.sum(w, axis=1))),
        "gross_exposure": float(np.mean(np.sum(np.abs(w), axis=1))),
    }


def probe_vector(weight: F64, points: int = PROBE_POINTS) -> list[float]:
    """Net target weight at `points` evenly spaced decision indices: the fixed probe set for correlation."""
    if weight.ndim != 2 or weight.shape[0] == 0 or points <= 0:
        return [0.0] * max(points, 0)
    net = np.sum(np.nan_to_num(weight, nan=0.0), axis=1)
    last = net.shape[0] - 1
    idx = [round(last * k / max(points - 1, 1)) for k in range(points)]
    return [float(net[i]) for i in idx]


def probe_corr(a: Sequence[float], b: Sequence[float]) -> float:
    x, y = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    if x.shape != y.shape or x.shape[0] < 3:
        return 0.0
    sx, sy = float(np.std(x)), float(np.std(y))
    if sx <= 0 or sy <= 0:
        return 0.0
    return float(np.mean((x - x.mean()) * (y - y.mean())) / (sx * sy))


@dataclass(frozen=True, slots=True)
class Descriptor:
    turnover: float
    holding_hours: float
    net_exposure: float

    @staticmethod
    def of(metrics: Mapping[str, object]) -> Descriptor:
        def num(key: str) -> float:
            v = metrics.get(key)
            return float(v) if isinstance(v, int | float) and math.isfinite(float(v)) else 0.0

        return Descriptor(num("turnover"), num("avg_holding_hours"), num("net_exposure"))

    def cell(self) -> tuple[int, int, int]:
        return (
            bisect.bisect_right(TURNOVER_EDGES, self.turnover),
            bisect.bisect_right(HOLDING_HOURS_EDGES, self.holding_hours),
            bisect.bisect_right(NET_EXPOSURE_EDGES, self.net_exposure),
        )


def n_cells() -> int:
    return (len(TURNOVER_EDGES) + 1) * (len(HOLDING_HOURS_EDGES) + 1) * (len(NET_EXPOSURE_EDGES) + 1)


@dataclass(frozen=True, slots=True)
class Elite:
    trial_id: int
    spec_hash: str
    family: str
    score: float  # raw score (e.g. sr_daily of the selection split)
    n_obs: int  # observations behind the score (days)
    descriptor: Descriptor
    probe: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class Offer:
    accepted: bool
    cell: tuple[int, int, int]
    replaced: int | None  # trial id of the elite that was replaced
    reason: str


@dataclass
class Archive:
    k0: float = 30.0  # shrinkage pseudo-observations
    prior_mean: float = 0.0  # prior score when the archive is empty; else the archive mean
    corr_cap: float = 0.9
    cells: dict[tuple[int, int, int], Elite] = field(default_factory=lambda: dict[tuple[int, int, int], Elite]())
    offers: int = 0

    def __post_init__(self) -> None:
        if self.k0 < 0 or not 0.0 < self.corr_cap <= 1.0:
            raise ValueError(f"invalid archive settings k0={self.k0}, corr_cap={self.corr_cap}")

    def _prior(self) -> float:
        return float(np.mean([e.score for e in self.cells.values()])) if self.cells else self.prior_mean

    def shrunk(self, e: Elite) -> float:
        w = e.n_obs / (e.n_obs + self.k0) if e.n_obs + self.k0 > 0 else 0.0
        return w * e.score + (1.0 - w) * self._prior()

    def _better(self, cand: Elite, inc: Elite) -> bool:
        a, b = self.shrunk(cand), self.shrunk(inc)
        return a > b or (a == b and cand.trial_id < inc.trial_id)

    def offer(self, cand: Elite) -> Offer:
        self.offers += 1
        if not math.isfinite(cand.score):
            return Offer(False, cand.descriptor.cell(), None, "undefined score")
        cell = cand.descriptor.cell()
        # semantic duplicate check against every elite (fixed probe set)
        for key, e in self.cells.items():
            c = probe_corr(cand.probe, e.probe)
            if (c > self.corr_cap and e.spec_hash != cand.spec_hash) or e.spec_hash == cand.spec_hash:
                if self._better(cand, e):
                    del self.cells[key]
                    self.cells[cell] = (
                        cand if cell not in self.cells or self._better(cand, self.cells[cell]) else self.cells[cell]
                    )
                    return Offer(
                        self.cells.get(cell) is cand, cell, e.trial_id, f"replaced correlated elite (rho={c:.3f})"
                    )
                return Offer(False, cell, None, f"semantic duplicate of trial {e.trial_id} (rho={c:.3f})")
        inc = self.cells.get(cell)
        if inc is None:
            self.cells[cell] = cand
            return Offer(True, cell, None, "empty cell")
        if self._better(cand, inc):
            self.cells[cell] = cand
            return Offer(True, cell, inc.trial_id, f"shrunk {self.shrunk(cand):.4f} > {self.shrunk(inc):.4f}")
        return Offer(False, cell, None, f"incumbent trial {inc.trial_id} keeps the cell")

    def fill_rate(self) -> float:
        return len(self.cells) / n_cells()

    def diversity(self) -> dict[str, float]:
        """Occupied cells, distinct families and mean pairwise probe correlation among elites."""
        elites = list(self.cells.values())
        pairs = [probe_corr(a.probe, b.probe) for i, a in enumerate(elites) for b in elites[i + 1 :]]
        return {
            "cells": float(len(elites)),
            "fill_rate": self.fill_rate(),
            "families": float(len({e.family for e in elites})),
            "mean_pairwise_corr": float(np.mean(pairs)) if pairs else 0.0,
        }

    def ranked(self) -> list[Elite]:
        return sorted(self.cells.values(), key=lambda e: (-self.shrunk(e), e.trial_id))

    def to_json(self) -> str:
        body = {
            "k0": self.k0,
            "prior_mean": self.prior_mean,
            "corr_cap": self.corr_cap,
            "offers": self.offers,
            "elites": [
                {
                    "cell": list(k),
                    "trial_id": e.trial_id,
                    "spec_hash": e.spec_hash,
                    "family": e.family,
                    "score": e.score,
                    "n_obs": e.n_obs,
                    "descriptor": [e.descriptor.turnover, e.descriptor.holding_hours, e.descriptor.net_exposure],
                    "probe": list(e.probe),
                }
                for k, e in sorted(self.cells.items())
            ],
        }
        return json.dumps(body, sort_keys=True)

    @staticmethod
    def from_json(text: str) -> Archive:
        body = json.loads(text)
        a = Archive(float(body["k0"]), float(body["prior_mean"]), float(body["corr_cap"]))
        a.offers = int(body.get("offers", 0))
        for raw in body.get("elites", []):
            d = raw["descriptor"]
            e = Elite(
                int(raw["trial_id"]),
                str(raw["spec_hash"]),
                str(raw["family"]),
                float(raw["score"]),
                int(raw["n_obs"]),
                Descriptor(float(d[0]), float(d[1]), float(d[2])),
                tuple(float(v) for v in raw["probe"]),
            )
            a.cells[tuple(int(v) for v in raw["cell"])] = e  # type: ignore[assignment]
        return a
