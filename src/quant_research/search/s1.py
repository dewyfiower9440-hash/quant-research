"""S1 screening proxy (design v2.1 §8, ADR-023): cheap information-coefficient statistics of a candidate's
signal and positions against forward returns, computed on the point-in-time panel. Pure (no I/O).

- Label: per symbol, `exec_open[k + h] / exec_open[k] - 1`. `exec_open` is the first 1m open at
  `decision + latency`, i.e. the first possible fill AFTER the decision, so the label never overlaps the
  information the features saw. There is no label within h decisions of the panel end.
- IC: cross-sectional Spearman of signal vs label per decision (members only, at least
  `min_cross_section` names, non-constant), averaged over decisions; significance by a Newey-West HAC t
  with h lags (labels of consecutive decisions overlap by h - 1 bars).
- Event return (the event-style proxy; it works for every family): per decision, the mean of
  `position * label` over the names with a long (+1) or short (-1) condition; `event_mean_net` subtracts
  one round trip per position. Funding is not in the label: flagged `funding_not_modelled`.

These numbers rank candidates for S2; they are NOT evidence of an edge (costs uncalibrated, ADR-021)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

import numpy as np
import numpy.typing as npt
import polars as pl

from quant_research.backtest.panel import Panel
from quant_research.backtest.run import data_version
from quant_research.research.features import FeatureEvalRecord
from quant_research.strategy.canonical import expand
from quant_research.strategy.compile import compile_spec, evaluate, order
from quant_research.strategy.spec import Node, StrategySpec

F64 = npt.NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class S1Config:
    horizon_bars: int = 24
    round_trip_cost: float = 0.0016  # S0Context prior: 2 x taker 0.05% + 2 x 3 bps half-spread
    min_cross_section: int = 3

    def __post_init__(self) -> None:
        if self.horizon_bars < 1:
            raise ValueError(f"horizon_bars must be >= 1, got {self.horizon_bars}")
        if not (math.isfinite(self.round_trip_cost) and self.round_trip_cost >= 0):
            raise ValueError(f"round_trip_cost must be finite and >= 0, got {self.round_trip_cost}")
        if self.min_cross_section < 2:
            raise ValueError(f"min_cross_section must be >= 2, got {self.min_cross_section}")


@dataclass(frozen=True, slots=True)
class S1Result:
    horizon_bars: int
    ic_mean: float | None
    ic_hac_t: float | None
    ic_samples: int  # decisions with a defined IC
    event_mean: float | None
    event_mean_net: float | None
    event_hac_t: float | None
    event_samples: int  # decisions with at least one position
    flags: tuple[str, ...]

    def score(self) -> float:
        """Ranking key for S2 promotion: HAC t of the net event return (undefined -> -inf)."""
        return -math.inf if self.event_hac_t is None else self.event_hac_t

    @classmethod
    def from_metrics(cls, m: Mapping[str, object]) -> S1Result:
        """The result of a finished S1 trial (resume: reused instead of evaluated again)."""

        def opt(key: str) -> float | None:
            v = m.get(key)
            return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None

        def count(key: str) -> int:
            v = m.get(key)
            return int(v) if isinstance(v, int | float) and not isinstance(v, bool) else 0

        flags = m.get("flags")
        return cls(
            count("horizon_bars"),
            opt("ic_mean"),
            opt("ic_hac_t"),
            count("ic_samples"),
            opt("event_mean"),
            opt("event_mean_net"),
            opt("event_hac_t"),
            count("event_samples"),
            tuple(str(f) for f in cast(list[object], flags)) if isinstance(flags, list) else (),
        )

    def metrics(self) -> dict[str, object]:
        return {
            "horizon_bars": self.horizon_bars,
            "ic_mean": self.ic_mean,
            "ic_hac_t": self.ic_hac_t,
            "ic_samples": self.ic_samples,
            "event_mean": self.event_mean,
            "event_mean_net": self.event_mean_net,
            "event_hac_t": self.event_hac_t,
            "event_samples": self.event_samples,
            "flags": list(self.flags),
            "score": self.score(),
        }


def hac_t(x: F64, lags: int) -> float | None:
    """Newey-West (Bartlett) t statistic of the mean of x; None if undefined (< 2 samples, no variance)."""
    if lags < 0:
        raise ValueError(f"lags must be >= 0, got {lags}")
    t = int(x.shape[0])
    if t < 2 or bool(np.all(x == x[0])):  # a constant series has no variance (rounding would fake one)
        return None
    m = float(np.dot(x, np.ones(t))) / t
    d = x - m
    s = float(np.dot(d, d)) / t
    for lag in range(1, min(lags, t - 1) + 1):
        s += 2.0 * (1.0 - lag / (lags + 1)) * float(np.dot(d[lag:], d[:-lag])) / t
    if not s > 1e-300:
        return None
    return m / math.sqrt(s / t)


def forward_labels(frame: pl.DataFrame, horizon_bars: int) -> pl.DataFrame:
    """symbol, decision_ns, label (h-decision forward return from the first fill after the decision)."""
    nxt = pl.col("exec_open").shift(-horizon_bars).over("symbol")
    return (
        order(frame.select("symbol", "decision_ns", "exec_open"))
        .with_columns((nxt / pl.col("exec_open") - 1.0).alias("label"))
        .select("symbol", "decision_ns", "label")
    )


def _finite(col: str) -> pl.Expr:
    return pl.col(col).is_not_null() & pl.col(col).is_finite()


def ic_by_decision(frame: pl.DataFrame, *, min_cross_section: int) -> pl.DataFrame:
    """decision_ns, ic: Spearman(signal, label) per decision over rows where both are finite."""
    valid = frame.filter(_finite("signal") & _finite("label"))
    per = (
        valid.group_by("decision_ns")
        .agg(
            pl.len().alias("n"),
            pl.col("signal").n_unique().alias("distinct"),
            pl.col("label").n_unique().alias("distinct_label"),
            pl.corr("signal", "label", method="spearman").alias("ic"),
        )
        .filter(
            (pl.col("n") >= min_cross_section)
            & (pl.col("distinct") > 1)
            & (pl.col("distinct_label") > 1)
            & _finite("ic")
        )
        .sort("decision_ns")
    )
    return per.select("decision_ns", pl.col("ic").cast(pl.Float64))


def _series(df: pl.DataFrame, col: str) -> F64:
    return np.asarray(df[col].cast(pl.Float64).to_numpy(), dtype=np.float64)


# Costs are ADR-021 priors and funding cash flows are never part of the S1 label.
S1_FLAGS = ("cost_uncalibrated", "funding_not_modelled")


def _ic_stats(joined: pl.DataFrame, cfg: S1Config) -> tuple[float | None, float | None, int]:
    ic = ic_by_decision(joined, min_cross_section=cfg.min_cross_section)
    if ic.height == 0:
        return None, None, 0
    x = _series(ic, "ic")
    return float(np.dot(x, np.ones(x.shape[0]))) / x.shape[0], hac_t(x, cfg.horizon_bars), ic.height


def evaluate_s1(spec: StrategySpec, panel: Panel, cfg: S1Config) -> S1Result:
    full = order(panel.frame)
    compiled = compile_spec(spec, full).frame
    labels = forward_labels(full, cfg.horizon_bars)
    rows = (
        compiled.join(labels, on=["symbol", "decision_ns"])
        .join(full.select("symbol", "decision_ns", "member"), on=["symbol", "decision_ns"])
        .filter(pl.col("member"))
    )
    if panel.evaluate_from_ns is not None:
        rows = rows.filter(pl.col("decision_ns") >= panel.evaluate_from_ns)
    ic_mean, ic_t, ic_n = _ic_stats(rows.select("decision_ns", "signal", "label"), cfg)
    pos = pl.col("long").cast(pl.Int8) - pl.col("short").cast(pl.Int8)
    events = (
        rows.filter(_finite("label"))
        .with_columns(pos.alias("pos"))
        .filter(pl.col("pos") != 0)
        .group_by("decision_ns")
        .agg((pl.col("pos").cast(pl.Float64) * pl.col("label")).mean().alias("r"))
        .sort("decision_ns")
    )
    if events.height == 0:
        ev_mean = ev_net = ev_t = None
    else:
        r = _series(events, "r") - cfg.round_trip_cost
        ev_net = float(np.dot(r, np.ones(r.shape[0]))) / r.shape[0]
        ev_mean = ev_net + cfg.round_trip_cost
        ev_t = hac_t(r, cfg.horizon_bars)
    return S1Result(cfg.horizon_bars, ic_mean, ic_t, ic_n, ev_mean, ev_net, ev_t, events.height, S1_FLAGS)


class IcFeatureEvaluator:
    """`research.features.FeatureEvaluator`: IC of a point-in-time feature tree against forward returns."""

    def __init__(self, cfg: S1Config) -> None:
        self.cfg = cfg

    def evaluate(self, feature: Node, horizon_bars: int, panel: Panel) -> FeatureEvalRecord:
        cfg = S1Config(horizon_bars, self.cfg.round_trip_cost, self.cfg.min_cross_section)
        full = order(panel.frame)
        tree = expand(feature, {}, None)
        values = full.select("symbol", "decision_ns", "member").with_columns(evaluate(full, tree).alias("signal"))
        rows = values.join(forward_labels(full, horizon_bars), on=["symbol", "decision_ns"]).filter(pl.col("member"))
        if panel.evaluate_from_ns is not None:
            rows = rows.filter(pl.col("decision_ns") >= panel.evaluate_from_ns)
        ic_mean, ic_t, n = _ic_stats(rows.select("decision_ns", "signal", "label"), cfg)
        return FeatureEvalRecord(feature, horizon_bars, ic_mean, ic_t, n, data_version(panel.files))
