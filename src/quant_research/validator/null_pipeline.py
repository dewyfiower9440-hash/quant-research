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
from typing import Literal, cast, get_args

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
from quant_research.strategy.space import build_point, space_of
from quant_research.strategy.spec import StrategySpec
from quant_research.validator.store import ValidatorStore
from quant_research.validator.verdict import DecisionRule, Verdict, decide, sharpe, stationary_bootstrap_indices

F64 = NDArray[np.float64]
HOUR_NS = 3_600 * 10**9
DAY_NS = 24 * HOUR_NS
Alpha = Literal["none", "random", "mechanism"]
NullKind = Literal["bootstrap", "permute", "signflip"]


@dataclass(frozen=True, slots=True)
class SyntheticSpec:
    """A synthetic market (ADR-043). Nulls: `bootstrap` = stationary block bootstrap of the base returns;
    `permute` = whole blocks of the base in a random order (structure inside a block kept, its time position
    lost); `signflip` = bootstrap path with each block's returns multiplied by a random sign (all symbols together:
    volatility and co-movement kept, any direction removed). Every null starts from per-symbol demeaned returns.
    `hours` = length of the output market (None: the base length), so holdouts longer than the base are possible.
    Alphas: `random` adds `strength` to ALL symbols in a random fifth of the hours; `mechanism` makes every symbol
    lean against its previous hour."""

    alpha: Alpha = "none"
    strength: float = 0.0  # per-hour drift (random) or mean-reversion coefficient (mechanism)
    block_len: int = 24  # hours per block
    null: NullKind = "bootstrap"
    hours: int | None = None

    def __post_init__(self) -> None:
        if self.null not in get_args(NullKind):
            raise ValueError(f"unknown null {self.null!r}; one of {get_args(NullKind)}")
        if self.alpha not in get_args(Alpha):
            raise ValueError(f"unknown alpha {self.alpha!r}; one of {get_args(Alpha)}")
        if not math.isfinite(self.strength) or (self.alpha == "mechanism" and abs(self.strength) >= 1.0):
            raise ValueError(f"invalid strength {self.strength} for alpha {self.alpha!r}")
        if self.block_len < 1:
            raise ValueError(f"block_len must be >= 1, got {self.block_len}")
        if self.hours is not None and self.hours < 2:
            raise ValueError(f"hours must be >= 2, got {self.hours}")


def _bootstrap_path(source: int, length: int, block_len: int, rng: np.random.Generator) -> NDArray[np.int64]:
    """Stationary bootstrap indices of `length` into a source of `source` rows (geometric blocks, circular)."""
    starts = rng.integers(0, source, size=length)
    new_block = rng.random(length) < 1.0 / block_len
    new_block[0] = True
    idx = np.empty(length, dtype=np.int64)
    for t in range(length):
        idx[t] = starts[t] if new_block[t] else (idx[t - 1] + 1) % source
    return idx


def _permutation_path(source: int, length: int, block_len: int, rng: np.random.Generator) -> NDArray[np.int64]:
    """Whole blocks of the source in random order, passes repeated until `length` rows."""
    blocks = [np.arange(s, min(s + block_len, source), dtype=np.int64) for s in range(0, source, block_len)]
    out: list[NDArray[np.int64]] = []
    n = 0
    while n < length:
        for b in rng.permutation(len(blocks)):
            out.append(blocks[int(b)])
            n += blocks[int(b)].shape[0]
    return np.concatenate(out)[:length]


def parse_null(name: str) -> NullKind:
    """A null kind from text (CLI); an unknown name is refused, never mapped to another null."""
    for kind in get_args(NullKind):
        if name == kind:
            return cast(NullKind, kind)
    raise ValueError(f"unknown null {name!r}; one of {get_args(NullKind)}")


def realizable_z(alpha: Alpha, rets: F64) -> float:
    """z (mean / sd * sqrt(T)) on `rets` of the best strategy that can actually be TRADED against the planted alpha
    (A3 review: the old formulas assumed independent symbols and an oracle that knows which hours drift).
    random -> long all symbols equally (the drifting hours are not observable); mechanism -> per symbol, fade the
    last hour (position -sign(r_{t-1})), equal weight; none -> the long-all z (a reference)."""
    simple = np.expm1(rets)  # the engine's returns (re-review: z on log returns differs)
    pnl = (-np.sign(simple[:-1]) * simple[1:]).mean(axis=1) if alpha == "mechanism" else simple.mean(axis=1)
    sd = float(np.std(pnl, ddof=1))
    return float(np.mean(pnl)) / sd * math.sqrt(pnl.shape[0]) if sd > 0 else 0.0


def calibrate_strength(
    alpha: Alpha,
    z: float,
    closes: F64,
    *,
    holdout_hours: int,
    draws: int = 8,
    seed: int = 0,
    block_len: int = 24,
    iters: int = 40,
    null: NullKind = "bootstrap",
) -> float:
    """The strength at which the tradable strategy of `realizable_z` reaches `z` on average over `draws` synthetic
    holdouts of `holdout_hours` built with `null` (bisection; ADR-043). The z is the GROSS (before cost) hourly
    z of that strategy (long all / fade the last hour), not a daily net SR. An unreachable target is refused."""
    if alpha == "none" or z <= 0:
        return 0.0

    def mean_z(strength: float) -> float:
        zs = [
            realizable_z(
                alpha,
                synthetic_returns(
                    closes,
                    SyntheticSpec(
                        alpha=alpha, strength=strength, block_len=block_len, hours=holdout_hours + 1, null=null
                    ),
                    np.random.default_rng(seed + i),
                ),
            )
            for i in range(draws)
        ]
        return float(np.mean(zs))

    lo, hi = 0.0, (0.9 if alpha == "mechanism" else 0.01)
    while alpha == "random" and mean_z(hi) < z and hi < 1.0:  # widen until the target is bracketed
        hi *= 2
    at_lo, at_hi = mean_z(lo), mean_z(hi)
    if not at_lo < z <= at_hi:
        raise ValueError(f"calibration cannot reach z={z} for {alpha} (z at {lo}: {at_lo:.2f}, at {hi}: {at_hi:.2f})")
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        if mean_z(mid) < z:
            lo = mid
        else:
            hi = mid
    return hi


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
    # every null starts from CENTRED SIMPLE returns (the engine trades simple returns): resampling raw returns
    # planted each symbol's drift (A3 review 1), and centring log returns still left a sigma^2/2 arithmetic drift
    # that a high-vs-low volatility long/short harvests (re-review); resampling and the sign flip act on simple
    # returns, converted back to log returns for the price path
    rets = np.expm1(rets)
    rets = rets - rets.mean(axis=0)
    source = rets.shape[0]
    t = source if spec.hours is None else spec.hours - 1
    if spec.null == "permute":
        idx = _permutation_path(source, t, spec.block_len, rng)
    elif spec.hours is None and spec.null == "bootstrap":
        idx = stationary_bootstrap_indices(source, spec.block_len, 1, rng)[0]  # the v2.1 path, unchanged
    else:
        idx = _bootstrap_path(source, t, spec.block_len, rng)
    out = rets[idx].copy()
    if spec.null == "signflip":
        signs = np.where(rng.random(t // spec.block_len + 1) < 0.5, -1.0, 1.0)
        out *= np.repeat(signs, spec.block_len)[:t, None]
    out = np.log1p(np.maximum(out, -0.99))  # back to log returns (a -100 % hour cannot exist)
    if spec.alpha == "random" and spec.strength:
        picks = rng.integers(0, 5, size=t)  # a random fifth of the hours carries a drift
        hours = np.equal(picks, 0)
        out[hours] += spec.strength
    elif spec.alpha == "mechanism" and spec.strength:
        for k in range(1, t):  # tomorrow leans against today: a mean-reversion mechanism
            out[k] -= spec.strength * out[k - 1]
    return out


FUNDING_INTERVAL_HOURS = 8


def null_inputs(
    symbols: Sequence[str], grid: NDArray[np.int64], timeframe_ns: int, rng: np.random.Generator
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(premium bars, funding settlements) drawn independently of the returns: a slowly moving premium level per
    symbol (AR(1) noise) and an 8-hourly funding rate (its own AR(1) noise). They let a v3 campaign (ADR-052:
    funding required; ADR-053: families that read premium and funding) run on a null market; by construction
    they predict nothing."""
    n = len(grid)
    premium: list[pl.DataFrame] = []
    funding: list[pl.DataFrame] = []
    every = FUNDING_INTERVAL_HOURS * HOUR_NS // timeframe_ns
    for s in symbols:
        shocks = rng.standard_normal(n)
        level = np.zeros(n)
        for k in range(1, n):  # AR(1), half-life about a day of hourly bars
            level[k] = 0.97 * level[k - 1] + 0.00005 * shocks[k]
        premium.append(
            pl.DataFrame(
                {
                    "symbol": [s] * n,
                    "event_ns": grid - timeframe_ns,
                    "open": level,
                    "high": level,
                    "low": level,
                    "close": level,
                    "available_ns": grid,
                }
            )
        )
        at = grid[:: max(1, int(every))]
        rate = np.zeros(len(at))
        noise = rng.standard_normal(len(at))
        for k in range(1, len(at)):
            rate[k] = 0.8 * rate[k - 1] + 0.00005 * noise[k]
        funding.append(
            pl.DataFrame(
                {
                    "symbol": [s] * len(at),
                    "event_ns": at,
                    "last_funding_rate": rate,
                    "funding_interval_hours": [FUNDING_INTERVAL_HOURS] * len(at),
                    "available_ns": at,
                }
            )
        )
    return pl.concat(premium), pl.concat(funding)


def synthetic_panel(
    base: Panel,
    spec: SyntheticSpec,
    *,
    seed: int,
    start_price: float = 100.0,
    latency_ns: int | None = None,
    inputs: bool = False,
) -> Panel:
    """A panel on the SAME decision grid as `base`, rebuilt from synthetic hourly bars (one bar = one hour).
    The same seed gives the same synthetic market at any `latency_ns` (the lag stress re-assembles it).
    `inputs`: also premium bars and funding settlements that are independent of the returns (`null_inputs`), from
    a random stream of their own, so the price market of a seed is the same with and without them."""
    symbols, grid, closes = hourly_closes(base)
    if spec.hours is not None:  # a longer (or shorter) market on the base's hourly grid
        grid = int(grid[0]) + np.arange(spec.hours, dtype=np.int64) * base.config.timeframe_ns
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
    premium, funding = null_inputs(symbols, grid, tf, np.random.default_rng([seed, 1])) if inputs else (None, None)
    frame = assemble(decisions, symbols, minutes, cfg, premium=premium, funding=funding).with_columns(
        # one synthetic bar per hour by construction: every execution interval is complete
        pl.lit(True).alias("complete"),
        pl.lit(tf // MINUTE_NS).alias("exec_minutes"),
    )
    # the synthetic spec is part of the data identity (a reused S1 result must come from the same market)
    tag = f"synthetic:{spec.alpha}:{spec.strength}:{spec.block_len}:{spec.null}:{spec.hours}"
    files = [(tag + (":inputs" if inputs else ""), seed)]
    return Panel(frame, settlements(funding, None), files, cfg, evaluate_from_ns=base.evaluate_from_ns)


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
    for name, lat in stress_latencies(holdout.config.latency_ns, holdout.config.timeframe_ns).items():
        if panel_at_latency is None:  # refused before the base run already; kept for the type checker
            raise ValueError("the lag stress needs the holdout rebuilt at the stressed latency (panel_at_latency)")
        panel_l = panel_at_latency(lat)
        out = run_backtest(spec, panel_l, catalog, ledger, base_cfg, stage="reference", fold=f"holdout-{name}")
        stressed[name] = sharpe(_pnl(ledger, out)) if out.status == "done" else None
    if pnl.shape[0] < 3:
        return incomplete_verdict(rule, status=base.status, reason=base.reason)
    entries = base.metrics.get("entries")
    exposure = base.metrics.get("gross_exposure")
    return decide(
        pnl,
        in_sample_sr,
        rule,
        stressed_sr=stressed,
        entries=float(entries) if isinstance(entries, int | float) else None,
        mean_exposure=float(exposure) if isinstance(exposure, int | float) else None,
    )


def stress_latencies(latency_ns: int, timeframe_ns: int) -> dict[str, int]:
    """The two lag stresses of the rule: one bar later, and twice the latency but at least two bars (with latency 0
    the old `max(2 * lag, bar)` equalled lag+1, so the second stress was the first one again; B8 review)."""
    return {"lag+1": latency_ns + timeframe_ns, "lagx2": max(2 * latency_ns, 2 * timeframe_ns)}


def incomplete_verdict(rule: DecisionRule, *, status: str, reason: str | None) -> Verdict:
    """A holdout run that did not complete cannot be judged: insufficient (refused, no query spent; ADR-042). It
    carries the rule hash so it can be booked."""
    return Verdict(
        False,
        ("holdout run did not complete",),
        {"status": status, "reason": reason, "rule_hash": rule.hash(), "insufficient": True},
        insufficient=True,
    )


SearcherFactory = Callable[[str, int], Searcher]


def random_factory(family: str, seed: int) -> Searcher:
    return RandomSearcher(space_of(family), seed)


def tpe_factory(family: str, seed: int) -> Searcher:
    return TpeSearcher(space_of(family), seed=seed, n_startup_trials=5)


@dataclass(frozen=True, slots=True)
class Replication:
    seed: int
    family: str
    searcher: str
    s2_done: int
    s3_sr: float | None
    passed: bool
    reasons: tuple[str, ...]
    insufficient: bool = False  # the holdout run could not be judged (ADR-042): neither a pass nor a fail


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
    spec = build_point(space_of(family), point)
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
    return Replication(
        seed,
        family,
        searcher.name,
        res.counts()["s2_done"],
        sr3,
        verdict.passed,
        verdict.reasons,
        insufficient=verdict.insufficient,
    )


@dataclass(frozen=True, slots=True)
class HarnessResult:
    label: str
    replications: tuple[Replication, ...]

    @property
    def judged(self) -> tuple[Replication, ...]:
        """Replications that produced a verdict (insufficient ones are neither passes nor fails)."""
        return tuple(r for r in self.replications if not r.insufficient)

    @property
    def n(self) -> int:
        return len(self.judged)

    @property
    def pass_rate(self) -> float:
        return sum(r.passed for r in self.judged) / self.n if self.n else math.nan

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
            "replications": len(self.replications),
            "judged": self.n,
            "insufficient": len(self.replications) - self.n,
            "measurable": self.n > 0,  # all insufficient: the harness measured NOTHING (not an FWER of 0)
            "pass_rate": self.pass_rate,
            "wilson_95": [lo, hi],
            "by_searcher": {
                s: sum(r.passed for r in self.judged if r.searcher == s)
                / max(1, sum(1 for r in self.judged if r.searcher == s))
                for s in sorted({r.searcher for r in self.judged})
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
    if verdict.insufficient:  # no verdict: the submission is refused and the query is not spent (ADR-042)
        store.refuse(sid, "; ".join(verdict.reasons))
        return False
    store.judge(sid, verdict.passed, verdict.detail)
    return verdict.passed


def families_round_robin(families: Sequence[str], reps: int) -> list[str]:
    return [families[i % len(families)] for i in range(reps)]


@dataclass(frozen=True, slots=True)
class EpochReplication:
    """One synthetic epoch: search over the families, then up to k holdout queries (ADR-043 end-to-end FWER)."""

    seed: int
    searcher: str
    candidates: int  # S3 survivors offered to the validator
    queries: int  # judged queries (insufficient ones are refused and spend nothing)
    insufficient: int
    passes: int
    records: tuple[dict[str, object], ...]  # per submitted candidate; kept so passes can be inspected

    @property
    def passed(self) -> bool:
        return self.passes > 0


def replicate_epoch(
    splits: Splits,
    catalog: Catalog,
    ledger: TrialLedger,
    vstore: ValidatorStore,
    *,
    families: Sequence[str],
    factory: SearcherFactory,
    budget: SearchBudget,
    rule: DecisionRule,
    seed: int,
    k: int,
    epoch: int = 1,
) -> EpochReplication:
    """Search every family on selection/confirmation, rank the S3 survivors with a positive confirmation SR, and submit
    them to the validator in that order until k queries are spent (review 1 finding 10: one query per replication
    measured a per-query rate, not the epoch's FWER)."""
    from quant_research.search.pipeline import point_from_json

    if k != rule.k:
        raise ValueError(f"the epoch budget k={k} must be the rule's k={rule.k} (alpha/k is the per-query level)")
    offered: list[tuple[float, str, RunOutcome]] = []
    name = ""
    for family in families:
        searcher = factory(family, seed)
        name = searcher.name
        res = run_family_search(
            ledger,
            family,
            searcher,
            selection=splits.selection,
            confirmation=splits.confirmation,
            catalog=catalog,
            config={"null_pipeline": "epoch", "seed": seed, "family": family, "searcher": searcher.name},
            s1=S1Config(horizon_bars=4),
            budget=budget,
            seed=seed,
            fold=f"epoch{seed}",
        )
        for o in res.s3:
            sr = o.metrics.get("sr_daily")
            # the submission policy research would follow: only survivors with a positive confirmation SR
            if o.status == "done" and isinstance(sr, int | float) and math.isfinite(float(sr)) and float(sr) > 0:
                offered.append((float(sr), family, o))
    offered.sort(key=lambda x: (-x[0], x[2].trial_id))
    vstore.ensure_budget(epoch, k, rule_hash=rule.hash())
    queries = insufficient = passes = 0
    records: list[dict[str, object]] = []
    for sr3, family, o in offered:
        if queries >= k:
            break
        spec = build_point(space_of(family), point_from_json(o.metrics.get("point")))  # type: ignore[arg-type]
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
        request = seed * 1_000 + len(records) + 1
        ok = register_and_judge(
            vstore, request, f"{family}:{o.trial_id}", rule, verdict, epoch=epoch, k=k, in_sample_sr=sr3
        )
        if verdict.insufficient:
            insufficient += 1
        else:
            queries += 1
            passes += int(ok)
        records.append(
            {
                "family": family,
                "trial_id": o.trial_id,
                "sr3": sr3,
                "passed": ok,
                "insufficient": verdict.insufficient,
                "detail": {key: verdict.detail.get(key) for key in ("sr_holdout", "p_one_sided", "psr", "days")},
            }
        )
    return EpochReplication(seed, name, len(offered), queries, insufficient, passes, tuple(records))


@dataclass(frozen=True, slots=True)
class EpochResult:
    label: str
    replications: tuple[EpochReplication, ...]

    def summary(self) -> dict[str, object]:
        # an epoch with no positive survivor is a MEASURED non-pass; only an epoch whose every submission was
        # insufficient measured nothing (A3 review finding 6)
        judged = [r for r in self.replications if r.queries > 0 or r.candidates == 0]
        n = len(judged)
        rate = sum(r.passed for r in judged) / n if n else math.nan
        lo, hi = wilson(rate, n)
        return {
            "label": self.label,
            "replications": len(self.replications),
            "measurable": n > 0,  # no replication made a query: nothing was measured (not an FWER of 0)
            "judged_replications": n,
            "unmeasured": sum(1 for r in self.replications if r.candidates > 0 and r.queries == 0),
            "no_candidate": sum(1 for r in self.replications if r.candidates == 0),
            "fwer": rate,  # P(at least one of the epoch's queries passed)
            "wilson_95": [lo, hi],
            "mean_queries": sum(r.queries for r in self.replications) / max(1, len(self.replications)),
            "mean_candidates": sum(r.candidates for r in self.replications) / max(1, len(self.replications)),
            "insufficient_share": sum(r.insufficient for r in self.replications)
            / max(1, sum(r.insufficient + r.queries for r in self.replications)),
        }


def wilson(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0 or not math.isfinite(p):
        return math.nan, math.nan
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)
