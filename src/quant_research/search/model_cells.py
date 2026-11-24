"""Model cells of the campaign (design v2.3 sections 6-7, ADR-053 / ADR-054): fixed lists of points whose weights
are estimated on the FIT part of a selection block and written into the point as numbers.

A model cell does not search. Its few points (combo: equal weights and IC weights) go through the same funnel as
any other batch (S0, S1, S2 on the validation part of selection, S3 of the best one on the confirmation split,
the cross-window confirmation), and the validator replays a candidate from its spec alone: no model file exists.
The constituents use the canonical parameters of the registered space (the middle of each capped axis), never a
parameter picked by an earlier S2 or S3 (that would carry the selection bias into the combination). Pure apart
from reading the panel frames it is given."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import polars as pl

from quant_research.backtest.panel import Panel
from quant_research.search.s1 import forward_labels, hac_t, ic_by_decision
from quant_research.strategy.compile import evaluate, order
from quant_research.strategy.s0 import tree_lookback
from quant_research.strategy.space import Point, Space
from quant_research.strategy.spec import Node
from quant_research.strategy.templates import model_terms

WEIGHT_DIGITS = 4  # the registered weight axes have a 1e-4 grid
MIN_CROSS_SECTION = 3
# design v2.3 appendix A: the label of a model fit is the next 24 hours' return (the rebalance interval)
MODEL_HORIZON_BARS: dict[str, int] = {"1h": 24, "4h": 6, "1d": 1}
RIDGE_ALPHAS: tuple[float, ...] = (1.0, 10.0, 100.0)  # ADR-054, on cross-sectionally standardised features
F64 = npt.NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class TermFit:
    name: str
    ic_mean: float | None
    ic_hac_t: float | None  # None: fewer than two decisions with a defined IC, or no variance
    decisions: int


def fixed_params(space: Space) -> dict[str, float]:
    """The parameters a model space fixes (its single-valued axes: the constituents' canonical windows)."""
    return {a.name: a.values()[0] for a in space.params if len(a.values()) == 1}


def fixed_struct(space: Space) -> dict[str, str | int]:
    """The one portfolio construction of a model space (every structural axis has a single choice)."""
    if any(len(a.choices) != 1 for a in space.struct):
        raise ValueError(f"{space.family}: a model space fixes its portfolio construction")
    return {a.name: a.choices[0] for a in space.struct}


def term_names(space: Space) -> tuple[str, ...]:
    return tuple(name for name, _tree, _sign in model_terms(space.template_family, fixed_params(space)))


def fit_ics(space: Space, fit: Panel, *, horizon_bars: int) -> list[TermFit]:
    """IC of every oriented constituent against the `horizon_bars` forward return on the fit panel's evaluated
    member rows (the S1 label: from the first fill after the decision, so no overlap with the features)."""
    frame = order(fit.frame)
    keys = frame.select("symbol", "decision_ns", "member")
    labels = forward_labels(frame, horizon_bars)
    out: list[TermFit] = []
    for name, tree, sign in model_terms(space.template_family, fixed_params(space)):
        values = evaluate(frame, Node.model_validate(tree)) * sign
        rows = keys.with_columns(values.alias("signal")).join(labels, on=["symbol", "decision_ns"])
        rows = rows.filter(pl.col("member"))
        if fit.evaluate_from_ns is not None:
            rows = rows.filter(pl.col("decision_ns") >= fit.evaluate_from_ns)
        ic = ic_by_decision(rows.select("decision_ns", "signal", "label"), min_cross_section=MIN_CROSS_SECTION)
        if ic.height == 0:
            out.append(TermFit(name, None, None, 0))
            continue
        x = np.asarray(ic["ic"].to_numpy(), dtype=np.float64)
        out.append(TermFit(name, float(np.dot(x, np.ones(x.shape[0]))) / x.shape[0], hac_t(x, horizon_bars), ic.height))
    return out


def normalise(raw: Mapping[str, float]) -> dict[str, float] | None:
    """Weights on the registered grid that sum to one in absolute value; None when every weight rounds to zero."""
    total = sum(abs(v) for v in raw.values())
    if not total > 0 or not math.isfinite(total):
        return None
    out = {k: round(v / total, WEIGHT_DIGITS) for k, v in raw.items()}
    return out if any(v != 0 for v in out.values()) else None


def equal_weights(names: Sequence[str]) -> dict[str, float]:
    weights = normalise({n: 1.0 for n in names})
    if weights is None:
        raise ValueError("a combination needs at least one constituent")
    return weights


def ic_weights(fits: Sequence[TermFit]) -> dict[str, float] | None:
    """Each constituent's HAC t of its IC, negatives and undefined ones cut to zero, normalised; None when no
    constituent has a positive t on the fit part (the IC-weighted point is then not proposed)."""
    return normalise({f.name: max(0.0, f.ic_hac_t or 0.0) for f in fits})


def model_point(space: Space, weights: Mapping[str, float]) -> Point:
    """The registered point of a model space with these term weights."""
    params = fixed_params(space) | {f"w_{name}": float(weights.get(name, 0.0)) for name in term_names(space)}
    return Point.of(space.family, params, fixed_struct(space))


def longest_window(space: Space) -> int:
    """The longest window (bars) any term of the model reads: the embargo's feature lookback."""
    return max(
        tree_lookback(Node.model_validate(tree))
        for _n, tree, _s in model_terms(space.template_family, fixed_params(space))
    )


def training_rows(space: Space, fit: Panel, *, horizon_bars: int, embargo_bars: int) -> tuple[F64, F64]:
    """(X, y) of the ridge fit: one row per evaluated member (decision, symbol) of the fit part.
    X: every term's oriented value, standardised across the members of its decision. y: the cross-sectional rank
    of the `horizon_bars` forward return in [0, 1], centred. Rows with any missing value are dropped (never
    filled). Purge: a label needs the fill `horizon_bars` decisions ahead inside the fit part, so the last
    `horizon_bars` decisions have none. Embargo: the last `embargo_bars` decisions are dropped as well, so no
    training row shares its feature window or label interval with the validation part that follows."""
    if embargo_bars < 0:
        raise ValueError(f"embargo_bars must be >= 0, got {embargo_bars}")
    frame = order(fit.frame)
    terms = model_terms(space.template_family, fixed_params(space))
    # the term values are aligned with `frame`: attach them before any join (a join need not keep the row order)
    valued = frame.select("symbol", "decision_ns", "member").with_columns(
        *((evaluate(frame, Node.model_validate(tree)) * sign).alias(f"x_{name}") for name, tree, sign in terms)
    )
    work = valued.join(forward_labels(frame, horizon_bars), on=["symbol", "decision_ns"])
    work = work.filter(pl.col("member"))
    if fit.evaluate_from_ns is not None:
        work = work.filter(pl.col("decision_ns") >= fit.evaluate_from_ns)
    decisions = sorted(set(work["decision_ns"].to_list()))
    if embargo_bars:
        keep = decisions[: max(0, len(decisions) - embargo_bars)]
        work = work.filter(pl.col("decision_ns").is_in(keep))
    cols = [f"x_{name}" for name, _t, _s in terms]
    finite = pl.all_horizontal(*(pl.col(c).is_not_null() & pl.col(c).is_finite() for c in [*cols, "label"]))
    work = work.filter(finite)
    n = pl.len().over("decision_ns")
    work = work.filter(n >= MIN_CROSS_SECTION)
    std = [
        ((pl.col(c) - pl.col(c).mean().over("decision_ns")) / pl.col(c).std().over("decision_ns")).alias(c)
        for c in cols
    ]
    rank = ((pl.col("label").rank().over("decision_ns") - 1.0) / (n - 1.0) - 0.5).alias("y")
    work = work.with_columns(*std, rank).with_columns(*(pl.col(c).fill_nan(0.0).fill_null(0.0) for c in cols))
    x = np.asarray(work.select(cols).to_numpy(), dtype=np.float64)
    y = np.asarray(work["y"].to_numpy(), dtype=np.float64)
    return x, y


def ridge_coefficients(x: F64, y: F64, alpha: float) -> F64:
    """argmin |y - X b|^2 / N + alpha |b|^2 (no intercept: X and y are centred per decision). The penalty is on
    the per-row scale, where X'X / N is close to the features' correlation matrix: added to X'X itself, the
    registered alphas vanished against 150,000 training rows and gave one point (review, step 7). Deterministic."""
    if alpha <= 0 or not math.isfinite(alpha):
        raise ValueError(f"alpha must be finite and > 0, got {alpha}")
    if x.ndim != 2 or x.shape[0] != y.shape[0] or x.shape[0] == 0:
        raise ValueError(f"ridge needs rows: X {x.shape}, y {y.shape}")
    n = float(x.shape[0])
    gram = x.T @ x / n + alpha * np.eye(x.shape[1])
    return np.asarray(np.linalg.solve(gram, x.T @ y / n), dtype=np.float64)


MIN_TRAINING_ROWS = 100


def ridge_points(
    space: Space, fit: Panel, *, horizon_bars: int, embargo_bars: int | None = None
) -> tuple[list[Point], dict[str, object]]:
    """ADR-054: one point per alpha (coefficients normalised to the weight grid; equal points once) and what
    was estimated. No point when the fit part leaves fewer than MIN_TRAINING_ROWS rows after purge and embargo."""
    names = term_names(space)
    embargo = max(horizon_bars, longest_window(space)) if embargo_bars is None else embargo_bars
    x, y = training_rows(space, fit, horizon_bars=horizon_bars, embargo_bars=embargo)
    record: dict[str, object] = {"rows": int(x.shape[0]), "embargo_bars": embargo, "alphas": {}}
    points: list[Point] = []
    if x.shape[0] < MIN_TRAINING_ROWS:
        return points, record
    fitted: dict[str, object] = {}
    for alpha in RIDGE_ALPHAS:
        coef = ridge_coefficients(x, y, alpha)
        weights = normalise({name: float(c) for name, c in zip(names, coef, strict=True)})
        fitted[f"{alpha:g}"] = weights
        if weights is None:
            continue
        point = model_point(space, weights)
        if point not in points:
            points.append(point)
    record["alphas"] = fitted
    return points, record


def model_points(space: Space, fit: Panel) -> tuple[list[Point], dict[str, object]]:
    """The fixed points of a model cell for this fit part."""
    horizon = MODEL_HORIZON_BARS[space.timeframe]
    if space.template_family == "combo":
        return combo_points(space, fit, horizon_bars=horizon)
    if space.template_family == "ridge":
        return ridge_points(space, fit, horizon_bars=horizon)
    raise ValueError(f"{space.family} is not a model space")


def combo_points(space: Space, fit: Panel, *, horizon_bars: int) -> tuple[list[Point], dict[str, object]]:
    """ADR-053: (`lin_eq`, `lin_ic`) and what was estimated (recorded with the batch). `lin_ic` is left out when
    it has no positive IC to weight by or equals the equal-weight point."""
    names = term_names(space)
    fits = fit_ics(space, fit, horizon_bars=horizon_bars)
    eq = equal_weights(names)
    ic = ic_weights(fits)
    points = [model_point(space, eq)]
    if ic is not None and model_point(space, ic) != points[0]:
        points.append(model_point(space, ic))
    record: dict[str, object] = {
        "lin_eq": eq,
        "lin_ic": ic,
        "fit": [{"term": f.name, "ic_mean": f.ic_mean, "ic_hac_t": f.ic_hac_t, "decisions": f.decisions} for f in fits],
    }
    return points, record
