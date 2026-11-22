"""Search pipeline driver (Phase 4 step 10, first half): one family, one searcher, one budget ->
S0 -> S1 -> promotion -> S2 (vector, selection split) -> optional S3 (event, confirmation split).

Every stage goes through the trial ledger (one fitness evaluation = one trial, refusals included), the
searcher is told the outcome of every proposal exactly once, the memory model refuses the batch BEFORE any
trial is written, and the time budget shrinks the S1 count rather than skipping accounting. Resume within a
cycle is idempotent at S2/S3: a done trial of the same experiment key and fold is reused instead of re-run.
After the batch the family bandit receives one cost-normalised, deflated reward and the MAP-Elites archive
is offered every done S2/S3 trial."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

import numpy as np
import polars as pl

from quant_research.backtest.panel import Panel, slice_panel
from quant_research.backtest.panel_store import DEFAULT_DATASETS
from quant_research.backtest.run import RunConfig, RunOutcome, experiment_config, run_backtest
from quant_research.catalog.store import Catalog
from quant_research.research.ledger import TrialLedger, TrialOrigin, experiment_key
from quant_research.search.archive import Archive, Descriptor, Elite
from quant_research.search.bandit import FamilyBandit, cost_normalised_reward, deflated_performance
from quant_research.search.cost_model import (
    CostPredictor,
    LinearCost,
    LinearMemory,
    guard_memory,
    plan_rows,
    spec_ops,
)
from quant_research.search.optimizers import Searcher, propose
from quant_research.search.promotion import Selection, select_for_s2
from quant_research.search.s0_stage import Accepted, S0Outcome, run_s0
from quant_research.search.s1 import S1Config
from quant_research.search.s1_stage import S1Scored, reuse_s1, run_s1
from quant_research.search.s2_stage import S2Scored, run_s2, s2_engine_config
from quant_research.strategy.canonical import spec_hash
from quant_research.strategy.ops import METRICS, SPOT
from quant_research.strategy.space import Point, build_point, space_of
from quant_research.strategy.spec import TIMEFRAME_NS
from quant_research.strategy.templates import REQUIRED_DATA


@dataclass(frozen=True, slots=True)
class SearchBudget:
    s1_count: int = 200
    top_k: int = 10
    promote_rate: float = 0.05
    s3_top: int = 1  # S3 runs on the best S2 candidates (0: the caller runs S3, e.g. the Director)
    # A6: plus this random share of the other S2 survivors (S1-vs-S3 fidelity pairs). Off by default: it changes
    # the candidate sets of every caller (the null harness included); the batch tool turns it on
    s3_random_rate: float = 0.0
    # v2.2 B5 (D5): the top-S3 slots go only to S2 survivors with mean gross exposure >= this (ADR-042 needs 5 %
    # for a verdict, so a nearly-flat strategy would only spend an S3 and later a refused query); 0 = off
    min_s3_exposure: float = 0.0
    time_budget_s: float | None = None  # predicted S1 time budget; shrinks s1_count when the cost model is known
    memory_limit_mb: float = 2_048.0

    def __post_init__(self) -> None:
        if (
            self.s1_count < 0
            or self.top_k < 0
            or self.s3_top < 0
            or not 0.0 <= self.promote_rate <= 1.0
            or not 0.0 <= self.s3_random_rate <= 1.0
            or self.min_s3_exposure < 0.0
        ):
            raise ValueError(f"invalid search budget {self}")


@dataclass
class SearchResult:
    family: str
    searcher: str
    fold: str | None
    s0: S0Outcome
    s1: list[S1Scored]
    selection: Selection
    s2: list[S2Scored]
    s3: list[RunOutcome]
    cost_s: dict[str, float]  # measured wall time per stage
    proposed: int = 0
    reward: float | None = None
    validation: RunOutcome | None = None  # the bandit's reward run (last part of selection), when a bandit is given
    # A7: predicted seconds per stage from the cost model, on the candidates actually evaluated (rows x ops each)
    predicted: dict[str, float] = field(default_factory=lambda: dict[str, float]())
    # the cost stages that the bandit reward is divided by (the fidelity S3 samples are measurement, not search)
    reward_cost_stages: tuple[str, ...] = ("S0", "S1", "S2", "S3", "val")
    archive_offers: dict[str, int] = field(default_factory=lambda: dict[str, int]())

    def best_s2(self) -> S2Scored | None:
        done = [s for s in self.s2 if s.outcome.status == "done"]
        if not done:
            return None
        return max(done, key=lambda s: (_sr(s.outcome.metrics), -s.outcome.trial_id))

    def counts(self) -> dict[str, int]:
        return {
            "proposed": self.proposed,
            "s0_trials": self.s0.generated,
            "s0_accepted": self.s0.unique_new,
            "s0_duplicates": self.s0.duplicates,
            "s0_rejected": self.s0.rejected,
            "s1_trials": len(self.s1),
            "promoted_top": len(self.selection.top),
            "promoted_random": len(self.selection.promoted),
            "s2_trials": len(self.s2),
            "s2_done": sum(1 for s in self.s2 if s.outcome.status == "done"),
            "s3_trials": len(self.s3),
            "val_trials": 0 if self.validation is None else 1,  # the bandit's reward run (A4)
        }

    def summary(self) -> dict[str, object]:
        best = self.best_s2()
        return {
            "family": self.family,
            "searcher": self.searcher,
            "fold": self.fold,
            **self.counts(),
            "cost_s": self.cost_s,
            "reward": self.reward,
            "val_trial": None if self.validation is None else self.validation.trial_id,
            "val_sr": None if self.validation is None else self.validation.metrics.get("sr_daily"),
            "best_s2_trial": None if best is None else best.outcome.trial_id,
            "best_point": None if best is None else point_json(best.source.candidate.point),
            "archive_offers": self.archive_offers,
        }


def point_json(point: Point) -> dict[str, object]:
    return {"family": point.family, "params": point.params_dict(), "struct": point.struct_dict()}


def point_from_json(body: Mapping[str, object]) -> Point:
    params = cast(Mapping[str, float], body["params"])
    struct = cast(Mapping[str, str | int], body["struct"])
    return Point.of(str(body["family"]), params, struct)


# the panel column that carries each optional input a family needs (templates.REQUIRED_DATA names)
DATA_COLUMNS: dict[str, tuple[str, str]] = {
    "funding": ("funding_rate", "binance.um.bulk.funding"),
    "premium": ("premium_close", "binance.um.bulk.premium_klines_1m"),
    # v2.3 step 7: one need per metrics column (review: the taker / top-trader ratios are null in some files where
    # open interest is not, so a family is checked on the column it reads)
    "open_interest": ("oi_value", METRICS),
    "toptrader_ratio": ("toptrader_ls", METRICS),
    "taker_ratio": ("taker_ls", METRICS),
    "spot": ("spot_close", SPOT),
}


def panel_datasets(template_family: str) -> tuple[str, ...]:
    """The datasets a family's panel must read: the default four plus the optional inputs it needs (v2.3 step 7; a
    panel built without them has no such column, so the family would stay data_missing after the data arrived)."""
    extra = [DATA_COLUMNS[n][1] for n in REQUIRED_DATA.get(template_family, ()) if n in DATA_COLUMNS]
    return tuple(dict.fromkeys([*DEFAULT_DATASETS, *extra]))


def missing_inputs(template_family: str, panel: Panel) -> tuple[str, ...]:
    """Datasets the family needs whose column has no value among the panel's member rows."""
    out: list[str] = []
    members = panel.frame.filter(pl.col("member"))
    for need in REQUIRED_DATA.get(template_family, ()):
        if need not in DATA_COLUMNS:
            continue
        column, dataset = DATA_COLUMNS[need]
        if column not in members.columns or members[column].drop_nulls().is_empty():
            out.append(dataset)
    return tuple(out)


def _exposure(metrics: dict[str, object]) -> float:
    value = metrics.get("gross_exposure")
    return float(value) if isinstance(value, int | float) and value == value else 0.0


def _sr(metrics: Mapping[str, object]) -> float:
    v = metrics.get("sr_daily")
    return float(v) if isinstance(v, int | float) and math.isfinite(float(v)) else -math.inf


def existing_trial(ledger: TrialLedger, key: str, stage: str, fold: str | None) -> int | None:
    """A finished trial of this experiment in this fold (resume: not evaluated twice within a cycle)."""
    row = ledger.store.conn.execute(
        "select trial_id from trial where experiment_key = ? and stage = ? and status in ('done', 'rejected') "
        "and ((? is null and fold is null) or fold = ?) order by trial_id limit 1",
        (key, stage, fold, fold),
    ).fetchone()
    return None if row is None else int(row[0])


def run_family_search(
    ledger: TrialLedger,
    family: str,
    searcher: Searcher,
    *,
    selection: Panel,
    confirmation: Panel | None,
    catalog: Catalog,
    config: Mapping[str, object],
    s1: S1Config,
    budget: SearchBudget,
    seed: int,
    fold: str | None = None,
    origin: TrialOrigin | None = None,
    gate: Callable[[], tuple[bool, str]] | None = None,
    cost_model: Mapping[str, CostPredictor] | None = None,
    memory_model: LinearMemory | None = None,
    bandit: FamilyBandit | None = None,
    archive: Archive | None = None,
    s2_config: RunConfig | None = None,
    s3_config: RunConfig | None = None,
    reward_split: float = 0.25,
) -> SearchResult:
    """Run one family's search batch. Raises MemoryBudgetExceeded before any trial when the predicted peak
    memory exceeds the budget; raises RuntimeError when the resource gate refuses."""
    try:
        space = space_of(family)  # v1 family or v2 "family@timeframe"
    except KeyError as e:
        raise ValueError(f"unknown family {family!r}") from e
    if missing := missing_inputs(space.template_family, selection):  # B6 review: refuse before spending S1
        raise ValueError(f"data: {family} needs {', '.join(missing)}, which has no rows in the selection panel")
    if TIMEFRAME_NS[space.timeframe] != selection.config.timeframe_ns:  # S1 has no timeframe check of its own
        raise ValueError(
            f"timeframe: space {family} is {space.timeframe} but the panel bars are "
            f"{selection.config.timeframe_ns // 10**9} s"
        )
    reward_panel = selection  # replaced by the validation part when a bandit is given
    if bandit is not None:  # A4: S1/S2 on the fit part; the last `reward_split` of selection is the bandit's
        selection, reward_panel = split_for_reward(selection, reward_split)
    rows = selection.frame.height
    if memory_model is not None:  # refuses BEFORE any trial; the reward run's panel is the larger one
        guard_memory(memory_model, max(rows, reward_panel.frame.height), limit_mb=budget.memory_limit_mb)
    if gate is not None:
        ok, why = gate()
        if not ok:
            raise RuntimeError(f"resource gate refused the batch: {why}")
    s1_count = budget.s1_count
    if cost_model is not None and budget.time_budget_s is not None and "S1" in cost_model:
        s1_count = s1_budget(cost_model["S1"], rows=rows, count=s1_count, budget_s=budget.time_budget_s)
    cost: dict[str, float] = {}

    first_new = int(ledger.store.conn.execute("select coalesce(max(trial_id), 0) from trial").fetchone()[0])
    t0 = time.perf_counter()
    cands = propose(space, searcher, s1_count)
    s0 = run_s0(ledger, cands, config, origin=origin)
    cost["S0"] = time.perf_counter() - t0

    t1 = time.perf_counter()
    new_hashes = {a.spec_hash for a in s0.accepted}
    seen: set[str] = set()
    dups: list[Accepted] = []
    for d in s0.duplicate_of:  # a spec repeated within this batch is evaluated once, as the new one
        if d.spec_hash not in new_hashes and d.spec_hash not in seen:
            seen.add(d.spec_hash)
            dups.append(d)
    reused, again = reuse_s1(ledger, dups, selection, config, s1, fold=fold)
    scored = [*run_s1(ledger, [*s0.accepted, *again], selection, config, s1, origin=origin, fold=fold), *reused]
    by_hash = {s.candidate.spec_hash: s for s in scored}
    # tell the searcher once per proposal, in proposal order: refusals -> None, else the (new or reused) S1 score
    for cand, status in zip(cands, s0.statuses, strict=True):
        hit = by_hash.get(spec_hash(cand.spec)) if cand.spec is not None and status != "rejected" else None
        if hit is None:
            searcher.tell(cand.point, None)
            continue
        sc = hit.result.score()
        searcher.tell(cand.point, sc if math.isfinite(sc) else None)
    cost["S1"] = time.perf_counter() - t1

    sel = select_for_s2(scored, top_k=budget.top_k, promote_rate=budget.promote_rate, seed=seed)
    t2 = time.perf_counter()
    s2cfg = s2_config or RunConfig(engine="vector", seed=seed)
    s2 = _run_s2_resumable(ledger, sel, selection, catalog, s2cfg, fold=fold, origin=origin)
    cost["S2"] = time.perf_counter() - t2

    s3: list[RunOutcome] = []
    if confirmation is not None and budget.s3_top > 0:
        t3 = time.perf_counter()
        done = sorted(
            (s for s in s2 if s.outcome.status == "done"), key=lambda s: (-_sr(s.outcome.metrics), s.outcome.trial_id)
        )
        s3cfg = s3_config or RunConfig(engine="event", seed=seed)
        if budget.min_s3_exposure > 0:
            exposed = [s for s in done if _exposure(s.outcome.metrics) >= budget.min_s3_exposure]
            done = exposed + [s for s in done if s not in exposed]  # the flat ones may still be fidelity samples
            top = exposed[: budget.s3_top]
        else:
            top = done[: budget.s3_top]
        rest = [s for s in done if s not in top]
        draws = np.random.default_rng([seed, 3]).random(len(rest))
        sampled = [s for s, u in zip(rest, draws, strict=True) if float(u) < budget.s3_random_rate]
        chosen = top + sampled
        n_top = len(top)
        for i, s in enumerate(chosen):
            if i == n_top:  # from here on: the fidelity samples, timed apart from the search's own S3
                cost["S3"] = time.perf_counter() - t3
                t3 = time.perf_counter()
            spec = build_point(space, s.source.candidate.point)
            h, conf = experiment_config(spec, confirmation, catalog, s3cfg)
            prior = existing_trial(ledger, experiment_key(h, conf), "S3", fold)
            if prior is not None:
                s3.append(outcome_from_trial(ledger, prior))
                continue
            tags: dict[str, object] = {
                "point": point_json(s.source.candidate.point),
                "s2_trial_id": s.outcome.trial_id,
                "rows": confirmation.frame.height,
                "ops": spec_ops(spec),
            }
            s3.append(
                run_backtest(
                    spec, confirmation, catalog, ledger, s3cfg, stage="S3", fold=fold, origin=origin, tags=tags
                )
            )
        cost["S3_sample" if len(chosen) > n_top else "S3"] = time.perf_counter() - t3
        cost.setdefault("S3", 0.0)

    result = SearchResult(family, searcher.name, fold, s0, scored, sel, s2, s3, cost, proposed=len(cands))
    if cost_model is not None:
        result.predicted = predicted_costs(ledger, cost_model, fold=fold, s1=scored, s2=s2, after_trial_id=first_new)
    if bandit is not None:
        tv = time.perf_counter()
        result.validation = _reward_run(ledger, s2, reward_panel, catalog, s2cfg, fold=fold, origin=origin)
        cost["val"] = time.perf_counter() - tv
        result.reward = batch_reward(result, sum(cost.get(k, 0.0) for k in result.reward_cost_stages))
        bandit.update(family, result.reward)
    if archive is not None:
        result.archive_offers = offer_to_archive(archive, family, [s.outcome for s in s2] + s3)
    return result


def outcome_from_trial(ledger: TrialLedger, trial_id: int) -> RunOutcome:
    row = ledger.store.conn.execute(
        "select status, reason, metrics_json from trial where trial_id = ?", (trial_id,)
    ).fetchone()
    metrics = cast(dict[str, object], json.loads(row[2])) if row[2] else {}
    status = "done" if str(row[0]) == "done" else "rejected"
    return RunOutcome(trial_id, status, None if row[1] is None else str(row[1]), metrics, None)


def _run_s2_resumable(
    ledger: TrialLedger,
    sel: Selection,
    panel: Panel,
    catalog: Catalog,
    config: RunConfig,
    *,
    fold: str | None,
    origin: TrialOrigin | None,
) -> list[S2Scored]:
    """run_s2 for the promoted candidates without a finished S2 trial in this fold; the rest are reused."""
    fresh_top: list[S1Scored] = []
    fresh_rand: list[S1Scored] = []
    reused: list[S2Scored] = []
    for s in [*sel.top, *sel.promoted]:
        spec = build_point(space_of(s.candidate.point.family), s.candidate.point)
        h, conf = experiment_config(spec, panel, catalog, s2_engine_config(spec, config))
        prior = existing_trial(ledger, experiment_key(h, conf), "S2", fold)
        if prior is None:
            (fresh_top if s in sel.top else fresh_rand).append(s)
        else:
            reused.append(S2Scored(s, sel.kind(s.trial_id), outcome_from_trial(ledger, prior)))
    if not fresh_top and not fresh_rand:
        return reused
    fresh = Selection(fresh_top, fresh_rand, [], sel.seed, sel.promote_rate)
    out = run_s2(ledger, fresh, panel, catalog, config, origin=origin, fold=fold)
    return reused + out


MIN_SPLIT_DECISIONS = 24  # each part of the reward split keeps at least a day of hourly decisions


OPS_PRIOR = 20  # typical expanded-tree node count, used only before the candidates are known


def s1_budget(model: CostPredictor, *, rows: int, count: int, budget_s: float) -> int:
    """How many S1 proposals fit the time budget: the v1 line shrinks through `plan_rows` (cost per row x ops),
    any other model (v2 `RowCost`: a per-trial cost on this panel) by its predicted seconds per trial."""
    if isinstance(model, LinearCost):
        fit_rows = plan_rows(model, rows=rows, ops=[OPS_PRIOR] * count, budget_s=budget_s)
        return count if fit_rows >= rows else max(0, int(count * fit_rows / max(rows, 1)))
    per_trial = model.predict(rows, OPS_PRIOR)
    if per_trial <= 0 or per_trial * count <= budget_s:
        return count
    return max(0, int(budget_s / per_trial))


def predicted_costs(
    ledger: TrialLedger,
    cost_model: Mapping[str, CostPredictor],
    *,
    fold: str | None,
    s1: Sequence[S1Scored],
    s2: Sequence[S2Scored],
    after_trial_id: int = 0,
) -> dict[str, float]:
    """Model seconds for the S1 / S2 trials EVALUATED in this batch (trial id > `after_trial_id`), each from its
    recorded rows and ops (A7: a constant ops count and the expected S2 count made the band ratio swing 0.6-2.5;
    review: reused trials cost a lookup, not an evaluation, and are left out)."""
    out: dict[str, float] = {}
    for stage, trials in (
        ("S1", [s.trial_id for s in s1 if s.trial_id > after_trial_id]),
        ("S2", [s.outcome.trial_id for s in s2 if s.outcome.trial_id > after_trial_id]),
    ):
        if stage not in cost_model or not trials:
            continue
        total = 0.0
        for (raw,) in ledger.store.conn.execute(
            "select metrics_json from trial where trial_id in (select value from json_each(?))", (json.dumps(trials),)
        ):
            m = cast(dict[str, object], json.loads(str(raw))) if raw else {}
            r, o = m.get("rows"), m.get("ops")
            if isinstance(r, int | float) and isinstance(o, int | float):
                total += cost_model[stage].predict(int(r), int(o))
        out[stage] = total
    return out


def split_for_reward(selection: Panel, share: float) -> tuple[Panel, Panel]:
    """(fit, validation): the selection block's evaluated decisions cut at 1 - `share`; the validation part keeps
    every earlier row as warm-up only (features), so it is evaluated on data the fit part never scored."""
    if not 0.0 < share < 1.0:
        raise ValueError(f"reward_split must be in (0, 1), got {share}")
    col = selection.frame["decision_ns"]
    lo, hi = col.min(), col.max()
    if not isinstance(lo, int) or not isinstance(hi, int):
        raise ValueError("an empty selection panel cannot be split")
    start = selection.evaluate_from_ns if selection.evaluate_from_ns is not None else lo
    grid = sorted(int(d) for d in col.unique().to_list() if int(d) >= start)
    cut_at = int(len(grid) * (1.0 - share))
    if cut_at < MIN_SPLIT_DECISIONS or len(grid) - cut_at < MIN_SPLIT_DECISIONS:
        raise ValueError(f"selection too short for the reward split ({len(grid)} decisions, share {share})")
    cut = grid[cut_at]
    tf = selection.config.timeframe_ns
    fit = slice_panel(selection, start, cut - tf, warmup_ns=start - lo)
    val = slice_panel(selection, cut, hi, warmup_ns=cut - lo)
    return fit, val


def _reward_run(
    ledger: TrialLedger,
    s2: Sequence[S2Scored],
    panel: Panel,
    catalog: Catalog,
    config: RunConfig,
    *,
    fold: str | None,
    origin: TrialOrigin | None,
) -> RunOutcome | None:
    """The best S2 candidate on the reward (validation) part of selection: a trial of its own (stage S2,
    fold '<fold>:val'), reused on resume."""
    done = sorted(
        (s for s in s2 if s.outcome.status == "done"), key=lambda s: (-_sr(s.outcome.metrics), s.outcome.trial_id)
    )
    if not done:
        return None
    best = done[0]
    spec = build_point(space_of(best.source.candidate.point.family), best.source.candidate.point)
    cfg = s2_engine_config(spec, config)
    vfold = f"{fold}:val" if fold is not None else "val"
    h, conf = experiment_config(spec, panel, catalog, cfg)
    prior = existing_trial(ledger, experiment_key(h, conf), "S2", vfold)
    if prior is not None:
        return outcome_from_trial(ledger, prior)
    tags: dict[str, object] = {
        "point": point_json(best.source.candidate.point),
        "s2_trial_id": best.outcome.trial_id,
        "purpose": "bandit_reward",
        "rows": panel.frame.height,
        "ops": spec_ops(spec),
    }
    return run_backtest(spec, panel, catalog, ledger, cfg, stage="S2", fold=vfold, origin=origin, tags=tags)


def batch_reward(result: SearchResult, cost_s: float) -> float:
    """SR of the best S2 candidate on the reward (validation) part of selection, per second of total batch cost
    (A4: the confirmation split never enters the bandit). The validation run is ONE evaluation on data the
    selection of that candidate never scored, so its null is SR 0, not the expected maximum of the batch's S2
    trials (A4 review: that double deflation floored almost every reward; ADR-046 addendum). No validation run /
    undefined -> 0 reward (the cost was still paid)."""
    v = result.validation
    if v is None or v.status != "done" or not math.isfinite(_sr(v.metrics)):
        return 0.0
    return cost_normalised_reward(deflated_performance(_sr(v.metrics), 0.0), max(cost_s, 1e-6))


def offer_to_archive(archive: Archive, family: str, outcomes: Sequence[RunOutcome]) -> dict[str, int]:
    counts = {"offered": 0, "accepted": 0}
    for o in outcomes:
        if o.status != "done":
            continue
        probe_raw = o.metrics.get("probe")
        probe = (
            tuple(float(cast(float, v)) for v in cast(list[object], probe_raw)) if isinstance(probe_raw, list) else ()
        )
        days = o.metrics.get("days")
        elite = Elite(
            o.trial_id,
            f"trial:{o.trial_id}",
            family,
            _sr(o.metrics),
            int(days) if isinstance(days, int | float) else 0,
            Descriptor.of(o.metrics),
            probe,
        )
        counts["offered"] += 1
        if archive.offer(elite).accepted:
            counts["accepted"] += 1
    return counts
