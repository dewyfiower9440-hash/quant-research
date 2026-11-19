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

from quant_research.backtest.panel import Panel
from quant_research.backtest.run import RunConfig, RunOutcome, experiment_config, run_backtest
from quant_research.catalog.store import Catalog
from quant_research.research.ledger import TrialLedger, TrialOrigin, experiment_key
from quant_research.search.archive import Archive, Descriptor, Elite
from quant_research.search.bandit import FamilyBandit, cost_normalised_reward, deflated_performance
from quant_research.search.cost_model import LinearCost, LinearMemory, guard_memory, plan_rows
from quant_research.search.multiplicity import expected_max_sr, sr_variance
from quant_research.search.optimizers import Searcher, propose
from quant_research.search.promotion import Selection, select_for_s2
from quant_research.search.s0_stage import S0Outcome, run_s0
from quant_research.search.s1 import S1Config
from quant_research.search.s1_stage import S1Scored, run_s1
from quant_research.search.s2_stage import S2Scored, run_s2
from quant_research.strategy.space import SPACES, Point, build_point


@dataclass(frozen=True, slots=True)
class SearchBudget:
    s1_count: int = 200
    top_k: int = 10
    promote_rate: float = 0.05
    s3_top: int = 1  # S3 runs on the best S2 candidates (0: the caller runs S3, e.g. the Director)
    time_budget_s: float | None = None  # predicted S1 time budget; shrinks s1_count when the cost model is known
    memory_limit_mb: float = 2_048.0

    def __post_init__(self) -> None:
        if self.s1_count < 0 or self.top_k < 0 or self.s3_top < 0 or not 0.0 <= self.promote_rate <= 1.0:
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
    cost_model: Mapping[str, LinearCost] | None = None,
    memory_model: LinearMemory | None = None,
    bandit: FamilyBandit | None = None,
    archive: Archive | None = None,
    s2_config: RunConfig | None = None,
    s3_config: RunConfig | None = None,
) -> SearchResult:
    """Run one family's search batch. Raises MemoryBudgetExceeded before any trial when the predicted peak
    memory exceeds the budget; raises RuntimeError when the resource gate refuses."""
    if family not in SPACES:
        raise ValueError(f"unknown family {family!r}")
    space = SPACES[family]
    rows = selection.frame.height
    if memory_model is not None:
        guard_memory(memory_model, rows, limit_mb=budget.memory_limit_mb)  # refuses BEFORE any trial
    if gate is not None:
        ok, why = gate()
        if not ok:
            raise RuntimeError(f"resource gate refused the batch: {why}")
    s1_count = budget.s1_count
    if cost_model is not None and budget.time_budget_s is not None and "S1" in cost_model:
        # shrink the sample so the predicted S1 batch fits the time budget (cost is per row x ops)
        fit_rows = plan_rows(cost_model["S1"], rows=rows, ops=[20] * s1_count, budget_s=budget.time_budget_s)
        if fit_rows < rows:
            s1_count = max(0, int(s1_count * fit_rows / max(rows, 1)))
    cost: dict[str, float] = {}

    t0 = time.perf_counter()
    cands = propose(space, searcher, s1_count)
    s0 = run_s0(ledger, cands, config, origin=origin)
    cost["S0"] = time.perf_counter() - t0

    t1 = time.perf_counter()
    scored = run_s1(ledger, s0.accepted, selection, config, s1, origin=origin, fold=fold)
    # tell the searcher once per proposal, in proposal order: S0 refusals / duplicates -> None, else S1 score
    scores = iter(scored)
    for cand, status in zip(cands, s0.statuses, strict=True):
        if status != "done":
            searcher.tell(cand.point, None)
            continue
        s = next(scores)
        sc = s.result.score()
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
        for s in done[: budget.s3_top]:
            spec = build_point(space, s.source.candidate.point)
            h, conf = experiment_config(spec, confirmation, catalog, s3cfg)
            prior = existing_trial(ledger, experiment_key(h, conf), "S3", fold)
            if prior is not None:
                s3.append(outcome_from_trial(ledger, prior))
                continue
            tags = {"point": point_json(s.source.candidate.point), "s2_trial_id": s.outcome.trial_id}
            s3.append(
                run_backtest(
                    spec, confirmation, catalog, ledger, s3cfg, stage="S3", fold=fold, origin=origin, tags=tags
                )
            )
        cost["S3"] = time.perf_counter() - t3

    result = SearchResult(family, searcher.name, fold, s0, scored, sel, s2, s3, cost, proposed=len(cands))
    if bandit is not None:
        result.reward = batch_reward(result, sum(cost.values()))
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
        spec = build_point(SPACES[s.candidate.point.family], s.candidate.point)
        h, conf = experiment_config(spec, panel, catalog, config)
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


def batch_reward(result: SearchResult, cost_s: float) -> float:
    """Validation-split (S3) SR of the best candidate, net of the null's expected max over the S2 trials of
    this batch, per second of total batch cost. No S3 / undefined -> 0 reward (the cost was still paid)."""
    done3 = [o for o in result.s3 if o.status == "done" and math.isfinite(_sr(o.metrics))]
    if not done3:
        return 0.0
    best = max(_sr(o.metrics) for o in done3)
    days_v = done3[0].metrics.get("days")
    t = int(days_v) if isinstance(days_v, int | float) and days_v >= 2 else 2
    n2 = max(1, sum(1 for s in result.s2 if s.outcome.status == "done"))
    sr0 = expected_max_sr(n2, sr_variance(0.0, 0.0, 3.0, t))
    return cost_normalised_reward(deflated_performance(best, sr0), max(cost_s, 1e-6))


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
