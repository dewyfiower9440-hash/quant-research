"""Validator process (v2.2 B8; design §3.1, §9-2; ADR-040/041/042): research submits, the validator judges.

Research writes a `validation_request` (its S3 trial) into research.db's outbox (`submit_candidate`; one request per
spec per epoch). The validator process reads the outbox read-only and, per request, in isolation:

1. checks it can be judged at all: the experiment and spec exist, the request's rule is the epoch's rule, a query is
   left (k = rule.k). Otherwise it is recorded `refused` before ANY holdout read and never retried.
2. registers it (spec + rule) in validator.db, logs every holdout read (each dataset the panel provider reads) in
   the hash-chained access log, and evaluates the spec on the historical holdout H_hist [2023-01, 2024-12) with the
   pre-registered rule (base run + cost and lag stresses, `null_pipeline.holdout_evaluation`).
3. a run that fails a precondition (ADR-042) is refused without spending a query; otherwise the forward holdout
   H_fwd [2026-07, 2026-11) is run and, for an LLM-origin trial, must have a positive SR (ADR-041: the model may know
   2023-2024 markets; Random/TPE trials only report it). The verdict spends one query and leaves one ack bit.

A request whose processing raises after registration stays `registered` and is retried on the next run (same seed,
same holdout, the same verdict); the queue goes on with the next request. The holdout evaluations are trials of the
VALIDATOR's own ledger, never of research.db. Holdout panels come from an injected provider (validator role)."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

import numpy as np
import structlog

from quant_research.backtest.panel import Panel
from quant_research.backtest.panel_store import DEFAULT_DATASETS, Window
from quant_research.backtest.run import RunConfig, run_backtest, spec_datasets, unblob
from quant_research.catalog.epochs import EPOCH2_WINDOWS, HOLDOUT_UNTIL_NS, RESEARCH_UNTIL_NS
from quant_research.catalog.store import Catalog
from quant_research.research.ledger import TrialLedger
from quant_research.research.store import ResearchStore
from quant_research.search.campaign import candidate_trials
from quant_research.search.pipeline import panel_datasets
from quant_research.strategy.spec import StrategySpec
from quant_research.validator.null_pipeline import holdout_evaluation
from quant_research.validator.store import Submission, ValidatorError, ValidatorStore
from quant_research.validator.verdict import DecisionRule, sharpe

log = structlog.get_logger(__name__)
H_HIST = next(Window(w.from_ns, w.to_ns) for w in EPOCH2_WINDOWS if w.role == "holdout_hist")
H_FWD = Window(RESEARCH_UNTIL_NS, HOLDOUT_UNTIL_NS)
EPOCH = 2
HoldoutRole = Literal["hist", "fwd"]
HoldoutProvider = Callable[[StrategySpec, HoldoutRole, int], Panel]  # (spec, holdout, latency_ns) -> panel


@dataclass(frozen=True, slots=True)
class Request:
    request_id: int
    trial_id: int
    spec_hash: str
    experiment_key: str
    in_sample_sr: float
    rule_hash: str
    llm_origin: bool


@dataclass(frozen=True, slots=True)
class Outcome:
    request_id: int
    status: Literal["judged", "refused", "error"]  # error: left registered, retried on the next run
    passed: bool | None  # None unless judged


def submit_candidate(research: ResearchStore, trial_id: int, *, rule: DecisionRule, now_ns: int) -> int:
    """Research side: put one finished S3 trial into the outbox (its SR on the confirmation split is the in-sample SR
    the rule shrinks). Refuses anything else, a trial already submitted and a spec already submitted (the same
    strategy found in another cell would spend a second query on the same holdout run)."""
    row = research.conn.execute(
        "select stage, status, experiment_key, spec_hash, metrics_json from trial where trial_id = ?", (trial_id,)
    ).fetchone()
    if row is None or str(row[0]) != "S3" or str(row[1]) != "done":
        raise ValidatorError(f"trial {trial_id} is not a finished S3 trial")
    if research.conn.execute("select 1 from validation_request where trial_id = ?", (trial_id,)).fetchone():
        raise ValidatorError(f"trial {trial_id} is already submitted")
    if research.conn.execute("select 1 from validation_request where spec_hash = ?", (str(row[3]),)).fetchone():
        raise ValidatorError(f"spec {row[3]} of trial {trial_id} is already submitted in this epoch")
    metrics = json.loads(str(row[4]))
    sr = metrics.get("sr_daily")
    if not isinstance(sr, int | float) or not math.isfinite(float(sr)):
        raise ValidatorError(f"trial {trial_id} has no in-sample SR")
    cur = research.conn.execute(
        "insert into validation_request(trial_id, spec_hash, experiment_key, in_sample_sr, rule_hash, created_ns) "
        "values (?,?,?,?,?,?)",
        (trial_id, str(row[3]), str(row[2]), float(sr), rule.hash(), now_ns),
    )
    return int(cur.lastrowid or 0)


@dataclass(frozen=True, slots=True)
class CandidateSubmission:
    submitted: list[dict[str, int]]  # {"trial_id", "request_id"}
    skipped: dict[str, str]  # trial id -> why (already submitted, same spec, not a finished S3)


def submit_campaign_candidates(
    research: ResearchStore, history: Sequence[Mapping[str, object]], *, rule: DecisionRule, now_ns: int
) -> CandidateSubmission:
    """v2.3 step 9: every candidate of a campaign history into the outbox; refusals are reported, not raised (the
    campaign tick runs this each time, so the candidates of earlier ticks come back as 'already submitted')."""

    submitted: list[dict[str, int]] = []
    skipped: dict[str, str] = {}
    for trial in candidate_trials(history):
        try:
            submitted.append(
                {"trial_id": trial, "request_id": submit_candidate(research, trial, rule=rule, now_ns=now_ns)}
            )
        except ValidatorError as e:
            skipped[str(trial)] = str(e)
    return CandidateSubmission(submitted, skipped)


def pending_requests(research_ro: sqlite3.Connection, store: ValidatorStore) -> list[Request]:
    """Outbox requests not judged or refused yet (new ones and ones left `registered` by a crash), oldest first."""
    done = {
        int(r[0]) for r in store.conn.execute("select request_id from submission where status in ('judged', 'refused')")
    }
    rows = research_ro.execute(
        "select v.request_id, v.trial_id, v.spec_hash, v.experiment_key, v.in_sample_sr, v.rule_hash, t.model_id "
        "from validation_request v join trial t on t.trial_id = v.trial_id order by v.request_id"
    ).fetchall()
    return [
        Request(int(r[0]), int(r[1]), str(r[2]), str(r[3]), float(r[4]), str(r[5]), r[6] is not None)
        for r in rows
        if int(r[0]) not in done
    ]


def final_bit(hist_passed: bool, fwd_sr: float | None, *, llm_origin: bool) -> bool:
    """ADR-041: an LLM-origin candidate must also be positive on the forward holdout; others only report it."""
    if not hist_passed:
        return False
    return (fwd_sr is not None and fwd_sr > 0) if llm_origin else True


def finite_detail(value: object) -> object:
    """JSON-safe detail: NaN / inf become null (the verdict_detail column only accepts valid JSON)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(k): finite_detail(v) for k, v in cast(Mapping[object, object], value).items()}
    if isinstance(value, list | tuple):
        return [finite_detail(v) for v in cast(Sequence[object], value)]
    return value


def _spec(research_ro: sqlite3.Connection, experiment_key: str) -> StrategySpec:
    row = research_ro.execute("select spec_json from experiment where experiment_key = ?", (experiment_key,)).fetchone()
    if row is None:
        raise ValidatorError(f"experiment {experiment_key} is not in research.db")
    return StrategySpec.model_validate_json(str(row[0]))


def _log_reads(
    store: ValidatorStore, submission_id: int, datasets: Sequence[str], window: Window, purpose: str
) -> None:
    for dataset in datasets:
        store.log_access(submission_id, dataset, window.first_ns, window.end_ns, purpose)


def _fwd_sr(spec: StrategySpec, panel: Panel, catalog: Catalog, ledger: TrialLedger, seed: int) -> float | None:
    fwd = run_backtest(
        spec, panel, catalog, ledger, RunConfig(engine="event", seed=seed), stage="reference", fold="holdout-fwd"
    )
    if fwd.status != "done":
        return None
    row = ledger.store.conn.execute("select pnl_daily from backtest_run where trial_id = ?", (fwd.trial_id,)).fetchone()
    pnl = unblob(bytes(row[0])) if row is not None else np.zeros(0)
    sr = sharpe(pnl) if pnl.shape[0] >= 3 else math.nan
    return sr if math.isfinite(sr) else None


def process_request(
    req: Request,
    *,
    research_ro: sqlite3.Connection,
    store: ValidatorStore,
    ledger: TrialLedger,
    catalog: Catalog,
    holdout: HoldoutProvider,
    rule: DecisionRule,
    datasets_read: Sequence[str] = DEFAULT_DATASETS,
    seed: int = 0,
) -> Outcome:
    """Check, register, read (logged), judge or refuse one request."""
    known = store.submission_of(req.request_id)
    placeholder = Submission(
        req.request_id, req.spec_hash, req.rule_hash, req.experiment_key, req.in_sample_sr, (), EPOCH
    )

    def close(why: str) -> Outcome:  # refused before any (new) holdout read, never retried
        if known is None:
            store.record_refused(placeholder, why)
        else:
            store.refuse(known[0], why)
        return Outcome(req.request_id, "refused", None)

    try:
        spec = _spec(research_ro, req.experiment_key)
    except (ValidatorError, ValueError) as e:  # pydantic's ValidationError is a ValueError
        return close(f"unreadable request: {e}")
    # every dataset the holdout provider reads for this spec is logged: its ops, the default panel, and the panel
    # datasets of its family (tools/run_validator.py builds holdout panels with panel_datasets; review step 9)
    datasets = sorted(set(spec_datasets(spec)) | set(datasets_read) | set(panel_datasets(spec.family)))
    k, used = store.budget(EPOCH)
    if req.rule_hash != rule.hash():
        return close(f"request rule {req.rule_hash} is not the epoch's rule {rule.hash()}")
    if used >= k:  # also for a retried request: it would read and evaluate, then fail in judge() forever
        return close(f"epoch {EPOCH} query budget k={k} is used up")
    # a request left `registered` by a crash is evaluated again (same seed, same holdout)
    sid = store.register(replace_datasets(placeholder, datasets)) if known is None else known[0]
    _log_reads(store, sid, datasets, H_HIST, "holdout_hist")
    verdict = holdout_evaluation(
        spec,
        holdout(spec, "hist", 0),
        catalog,
        ledger,
        rule,
        in_sample_sr=req.in_sample_sr,
        seed=seed,
        panel_at_latency=lambda latency: holdout(spec, "hist", latency),
    )
    if verdict.insufficient:  # ADR-042: no verdict, no query spent; research is told nothing either
        store.refuse(sid, "; ".join(verdict.reasons) or "insufficient")
        return Outcome(req.request_id, "refused", None)
    fwd_sr = None
    if verdict.passed:  # a failed H_hist verdict needs no forward read (it cannot change the bit)
        _log_reads(store, sid, datasets, H_FWD, "holdout_fwd")
        fwd_sr = _fwd_sr(spec, holdout(spec, "fwd", 0), catalog, ledger, seed)
    passed = final_bit(verdict.passed, fwd_sr, llm_origin=req.llm_origin)
    detail = dict(verdict.detail) | {
        "fwd_sr": fwd_sr,
        "llm_origin": req.llm_origin,
        "hist_passed": verdict.passed,
        "rule_hash": rule.hash(),
    }
    store.judge(sid, passed, cast(dict[str, object], finite_detail(detail)))
    return Outcome(req.request_id, "judged", passed)


def replace_datasets(s: Submission, datasets: Sequence[str]) -> Submission:
    return Submission(
        s.request_id, s.spec_hash, s.rule_hash, s.experiment_key, s.in_sample_sr, tuple(datasets), s.epoch
    )


def process_pending(
    research_ro: sqlite3.Connection,
    *,
    store: ValidatorStore,
    ledger: TrialLedger,
    catalog: Catalog,
    holdout: HoldoutProvider,
    rule: DecisionRule,
    datasets_read: Sequence[str] = DEFAULT_DATASETS,
) -> list[Outcome]:
    """Every pending request, in order, under the epoch-2 budget k = rule.k (bound to the rule once). One request's
    failure is logged and leaves it registered for the next run; the queue goes on."""
    store.ensure_budget(EPOCH, rule.k, rule_hash=rule.hash())
    out: list[Outcome] = []
    for r in pending_requests(research_ro, store):
        try:
            out.append(
                process_request(
                    r,
                    research_ro=research_ro,
                    store=store,
                    ledger=ledger,
                    catalog=catalog,
                    holdout=holdout,
                    rule=rule,
                    datasets_read=datasets_read,
                )
            )
        except Exception:  # job boundary: one request must not stall the queue; it is retried next run
            log.exception("validator.request_failed", request_id=r.request_id)
            out.append(Outcome(r.request_id, "error", None))
    return out
