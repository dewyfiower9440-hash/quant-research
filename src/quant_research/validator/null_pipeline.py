"""End-to-end null validation and power harness (Phase 5 step 10; design v2.1 §9-9).

Synthetic markets are built from a real panel's hourly bars: per symbol the log returns are resampled with
a stationary block bootstrap (and optionally shuffled across time), which keeps marginal distributions and
short-range dependence but destroys any predictable structure (the NULL market). Planted alphas add either a
random drift on a random subset of hours (`random`) or a mean-reversion mechanism (`mechanism`: tomorrow's
return leans against today's). One replication runs the reduced search (S0 -> S1 -> S2 -> S3) on the
selection / confirmation splits, then evaluates the best candidate on the synthetic HOLDOUT with the
validator's decision rule including the cost x2/x3 and lag +1/x2 stress re-runs. Over R replications of a
null market the share with >= 1 pass is the end-to-end FWER (ADR-022 target 0.05, R = 100 initial)."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Literal

import numpy as np
import polars as pl
from numpy.typing import NDArray

from quant_research.backtest.costs import CostModel
from quant_research.backtest.panel import (
    MINUTE_NS,
    Panel,
    PanelConfig,
    assemble,
    decision_grid,
    settlements,
    slice_panel,
)
from quant_research.backtest.run import RunConfig, RunOutcome, run_backtest, unblob
from quant_research.catalog.store import Catalog
from quant_research.research.ledger import TrialLedger
from quant_research.search.optimizers import RandomSearcher, Searcher, TpeSearcher
from quant_research.search.pipeline import SearchBudget, run_family_search
from quant_research.search.s1 import S1Config
from quant_research.strategy.space import SPACES, build_point
from quant_research.strategy.spec import StrategySpec
from quant_research.validator.store import ValidatorStore
from quant_research.validator.verdict import DecisionRule, Verdict, decide, sharpe, stationary_bootstrap_indices

F64 = NDArray[np.float64]
HOUR_NS = 3_600 * 10**9
DAY_NS = 24 * HOUR_NS
Alpha = Literal["none", "random", "mechanism"]


@dataclass(frozen=True, slots=True)
class SyntheticSpec:
    alpha: Alpha = "none"
    strength: float = 0.0  # per-hour drift (random) or mean-reversion coefficient (mechanism)
    block_len: int = 24  # hours per bootstrap block
    shuffle: bool = False  # additionally permute whole blocks (a stronger null)


def hourly_closes(panel: Panel) -> tuple[list[str], NDArray[np.int64], F64]:
    """(symbols, decision grid, close matrix T x N) from a panel's as-of bars (NaN where no bar)."""
    frame = panel.frame.select("symbol", "decision_ns", "close").sort(["decision_ns", "symbol"])
    symbols = sorted(set(frame["symbol"].to_list()))
    wide = frame.pivot(on="symbol", index="decision_ns", values="close").sort("decision_ns")
    grid = wide["decision_ns"].to_numpy().astype(np.int64)
    closes = np.column_stack([wide[s].to_numpy().astype(np.float64) for s in symbols])
    return symbols, grid, closes


def synthetic_returns(closes: F64, spec: SyntheticSpec, rng: np.random.Generator) -> F64:
    """Block-bootstrapped log returns (T-1 x N) with the planted alpha applied."""
    logp = np.log(np.where(np.isnan(closes), np.nan, closes))
    rets = np.diff(logp, axis=0)
    rets = np.nan_to_num(rets, nan=0.0)
    t = rets.shape[0]
    idx = stationary_bootstrap_indices(t, spec.block_len, 1, rng)[0]
    if spec.shuffle:
        idx = rng.permutation(idx)
    out = rets[idx].copy()
    if spec.alpha == "random" and spec.strength:
        picks = rng.integers(0, 5, size=t)  # a random fifth of the hours carries a drift
        hours = np.equal(picks, 0)
        out[hours] += spec.strength
    elif spec.alpha == "mechanism" and spec.strength:
        for k in range(1, t):  # tomorrow leans against today: a mean-reversion mechanism
            out[k] -= spec.strength * out[k - 1]
    return out


def synthetic_panel(
    base: Panel, spec: SyntheticSpec, *, seed: int, start_price: float = 100.0, latency_ns: int | None = None
) -> Panel:
    """A panel on the SAME decision grid as `base`, rebuilt from synthetic hourly bars (one bar = one hour).
    The same seed gives the same synthetic market at any `latency_ns` (the lag stress re-assembles it)."""
    symbols, grid, closes = hourly_closes(base)
    rng = np.random.default_rng(seed)
    rets = synthetic_returns(closes, spec, rng)
    prices = start_price * np.exp(np.vstack([np.zeros((1, len(symbols))), np.cumsum(rets, axis=0)]))
    tf = base.config.timeframe_ns
    frames: list[pl.DataFrame] = []
    for j, s in enumerate(symbols):
        p = prices[:, j]
        nxt = np.append(p[1:], p[-1])
        frames.append(
            pl.DataFrame(
                {
                    "symbol": [s] * len(grid),
                    "event_ns": grid - tf,  # the bar that is complete at the decision
                    "open": p,
                    "high": np.maximum(p, nxt) * 1.0005,
                    "low": np.minimum(p, nxt) * 0.9995,
                    "close": nxt,
                    "volume": np.full(len(grid), 10.0),
                    "quote_volume": np.full(len(grid), 1_000.0 * (j + 1)),
                    "available_ns": grid - tf + tf,  # available at the decision
                }
            )
        )
    minutes = pl.concat(frames)
    lat = base.config.latency_ns if latency_ns is None else latency_ns
    cfg = PanelConfig(tf, decision_delay_ns=0, latency_ns=lat)
    decisions = decision_grid(int(grid[0]), int(grid[-1]), cfg)
    frame = assemble(decisions, symbols, minutes, cfg).with_columns(
        # one synthetic bar per hour by construction: every execution interval is complete
        pl.lit(True).alias("complete"),
        pl.lit(tf // MINUTE_NS).alias("exec_minutes"),
    )
    # the synthetic spec is part of the data identity (a reused S1 result must come from the same market)
    files = [(f"synthetic:{spec.alpha}:{spec.strength}:{spec.block_len}:{spec.shuffle}", seed)]
    return Panel(frame, settlements(None, None), files, cfg, evaluate_from_ns=base.evaluate_from_ns)


@dataclass(frozen=True, slots=True)
class Splits:
    selection: Panel
    confirmation: Panel
    holdout: Panel
    # the holdout re-assembled at another decision -> order latency (lag stress); None: not rebuildable
    holdout_at_latency: Callable[[int], Panel] | None = None


def split_three(panel: Panel, *, warmup_days: int, sel_days: int, conf_days: int) -> Splits:
    """Three consecutive windows after a warm-up: selection, confirmation, holdout (the rest)."""
    grid = panel.frame["decision_ns"].unique().sort().to_numpy().astype(np.int64)
    t0 = int(grid[0]) + warmup_days * DAY_NS
    s1 = t0 + sel_days * DAY_NS
    c1 = s1 + conf_days * DAY_NS
    last = int(grid[-1]) - HOUR_NS
    if c1 + DAY_NS >= last:
        raise ValueError("panel too short for warm-up + selection + confirmation + holdout")
    warm = warmup_days * DAY_NS
    return Splits(
        slice_panel(panel, t0, s1 - HOUR_NS, warmup_ns=warm),
        slice_panel(panel, s1, c1 - HOUR_NS, warmup_ns=warm),
        slice_panel(panel, c1, last, warmup_ns=warm),
    )


def _pnl(ledger: TrialLedger, out: RunOutcome) -> F64:
    row = ledger.store.conn.execute("select pnl_daily from backtest_run where trial_id = ?", (out.trial_id,)).fetchone()
    return unblob(bytes(row[0])) if row is not None else np.zeros(0)


def synthetic_splits(
    base: Panel, spec: SyntheticSpec, *, seed: int, warmup_days: int, sel_days: int, conf_days: int
) -> Splits:
    """`split_three` of a synthetic panel, with the holdout rebuildable at any latency (same seed -> same market)."""
    days = {"warmup_days": warmup_days, "sel_days": sel_days, "conf_days": conf_days}
    sp = split_three(synthetic_panel(base, spec, seed=seed), **days)

    def holdout_at(latency_ns: int) -> Panel:
        syn = synthetic_panel(base, spec, seed=seed, latency_ns=latency_ns)
        return split_three(syn, **days).holdout

    return Splits(sp.selection, sp.confirmation, sp.holdout, holdout_at)


def holdout_evaluation(
    spec: StrategySpec,
    holdout: Panel,
    catalog: Catalog,
    ledger: TrialLedger,
    rule: DecisionRule,
    *,
    in_sample_sr: float,
    seed: int,
    panel_at_latency: Callable[[int], Panel] | None = None,
) -> Verdict:
    """The validator's judgement of one candidate on the synthetic holdout: base run + stress re-runs.
    The lag stresses need the holdout ASSEMBLED at the stressed latency (the execution prices are computed at
    assembly time; changing only the config would re-run the base fills, review finding 2)."""
    if rule.lag_stresses and panel_at_latency is None:
        raise ValueError("the lag stress needs the holdout rebuilt at the stressed latency (panel_at_latency)")
    base_cfg = RunConfig(engine="event", seed=seed)
    base = run_backtest(spec, holdout, catalog, ledger, base_cfg, stage="reference", fold="holdout")
    pnl = _pnl(ledger, base) if base.status == "done" else np.zeros(0)
    stressed: dict[str, float | None] = {}
    for m in rule.cost_multipliers:
        cfg = replace(base_cfg, cost=CostModel(stress=m))
        out = run_backtest(spec, holdout, catalog, ledger, cfg, stage="reference", fold=f"holdout-cost{m:g}")
        stressed[f"cost x{m:g}"] = sharpe(_pnl(ledger, out)) if out.status == "done" else None
    lag = holdout.config.latency_ns
    for name, lat in (
        ("lag+1", lag + holdout.config.timeframe_ns),
        ("lagx2", max(2 * lag, holdout.config.timeframe_ns)),
    ):
        if panel_at_latency is None:  # refused before the base run already; kept for the type checker
            raise ValueError("the lag stress needs the holdout rebuilt at the stressed latency (panel_at_latency)")
        panel_l = panel_at_latency(lat)
        out = run_backtest(spec, panel_l, catalog, ledger, base_cfg, stage="reference", fold=f"holdout-{name}")
        stressed[name] = sharpe(_pnl(ledger, out)) if out.status == "done" else None
    if pnl.shape[0] < 3:
        return incomplete_verdict(rule, status=base.status, reason=base.reason)
    return decide(pnl, in_sample_sr, rule, stressed_sr=stressed)


def incomplete_verdict(rule: DecisionRule, *, status: str, reason: str | None) -> Verdict:
    """A failed verdict for a holdout run that did not complete; it carries the rule hash so it can be booked."""
    return Verdict(
        False, ("holdout run did not complete",), {"status": status, "reason": reason, "rule_hash": rule.hash()}
    )


SearcherFactory = Callable[[str, int], Searcher]


def random_factory(family: str, seed: int) -> Searcher:
    return RandomSearcher(SPACES[family], seed)


def tpe_factory(family: str, seed: int) -> Searcher:
    return TpeSearcher(SPACES[family], seed=seed, n_startup_trials=5)


@dataclass(frozen=True, slots=True)
class Replication:
    seed: int
    family: str
    searcher: str
    s2_done: int
    s3_sr: float | None
    passed: bool
    reasons: tuple[str, ...]


def replicate(
    splits: Splits,
    catalog: Catalog,
    ledger: TrialLedger,
    *,
    family: str,
    factory: SearcherFactory,
    budget: SearchBudget,
    rule: DecisionRule,
    seed: int,
) -> Replication:
    """One replication: reduced search on selection/confirmation, verdict on the holdout."""
    searcher = factory(family, seed)
    res = run_family_search(
        ledger,
        family,
        searcher,
        selection=splits.selection,
        confirmation=splits.confirmation,
        catalog=catalog,
        config={"null_pipeline": True, "seed": seed, "family": family, "searcher": searcher.name},
        s1=S1Config(horizon_bars=4),
        budget=budget,
        seed=seed,
        fold=f"rep{seed}",
    )
    done3 = [o for o in res.s3 if o.status == "done"]
    if not done3:
        return Replication(seed, family, searcher.name, res.counts()["s2_done"], None, False, ("no S3 candidate",))
    best3 = max(done3, key=lambda o: float(o.metrics.get("sr_daily") or -math.inf))  # type: ignore[arg-type]
    sr3 = float(best3.metrics.get("sr_daily") or 0.0)  # type: ignore[arg-type]
    point_raw = best3.metrics.get("point")
    from quant_research.search.pipeline import point_from_json

    point = point_from_json(point_raw)  # type: ignore[arg-type]
    spec = build_point(SPACES[family], point)
    verdict = holdout_evaluation(
        spec,
        splits.holdout,
        catalog,
        ledger,
        rule,
        in_sample_sr=sr3,
        seed=seed,
        panel_at_latency=splits.holdout_at_latency,
    )
    return Replication(seed, family, searcher.name, res.counts()["s2_done"], sr3, verdict.passed, verdict.reasons)


@dataclass(frozen=True, slots=True)
class HarnessResult:
    label: str
    replications: tuple[Replication, ...]

    @property
    def n(self) -> int:
        return len(self.replications)

    @property
    def pass_rate(self) -> float:
        return sum(r.passed for r in self.replications) / self.n if self.n else math.nan

    def wilson_ci(self, z: float = 1.96) -> tuple[float, float]:
        n, p = self.n, self.pass_rate
        if n == 0:
            return math.nan, math.nan
        denom = 1 + z * z / n
        centre = (p + z * z / (2 * n)) / denom
        half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
        return max(0.0, centre - half), min(1.0, centre + half)

    def summary(self) -> dict[str, object]:
        lo, hi = self.wilson_ci()
        return {
            "label": self.label,
            "replications": self.n,
            "pass_rate": self.pass_rate,
            "wilson_95": [lo, hi],
            "by_searcher": {
                s: sum(r.passed for r in self.replications if r.searcher == s)
                / max(1, sum(1 for r in self.replications if r.searcher == s))
                for s in sorted({r.searcher for r in self.replications})
            },
        }


def register_and_judge(
    store: ValidatorStore,
    request_id: int,
    spec_hash: str,
    rule: DecisionRule,
    verdict: Verdict,
    *,
    epoch: int,
    k: int,
    in_sample_sr: float,
) -> bool:
    """Book-keeping of one harness verdict in a (temporary) validator.db: budget, verdict, ack."""
    from quant_research.validator.store import Submission

    store.ensure_budget(epoch, k, rule_hash=rule.hash())
    sid = store.register(
        Submission(request_id, spec_hash, rule.hash(), f"null:{request_id}", in_sample_sr, ("synthetic",), epoch)
    )
    store.judge(sid, verdict.passed, verdict.detail)
    return verdict.passed


def families_round_robin(families: Sequence[str], reps: int) -> list[str]:
    return [families[i % len(families)] for i in range(reps)]
