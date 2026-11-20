"""Campaign driver (v2.2 B7, D8): runs cells chosen by `search/campaign.py` through the search pipeline.

I/O stays at the edges: the caller injects the panel provider (one (selection, confirmation) pair per cell),
the resource gate, the time budget, the stop check and the state persister; the driver only orchestrates. Every
batch's fold is `<run>:<cell>:<pull>` and its seeds are derived from (campaign seed, cell, pull), so a batch
interrupted before its state was saved repeats with the same proposals and the pipeline reuses its finished trials.

Candidate rule (B7 review): the best S3-eligible S2 survivor's own S3 must beat the expected maximum SR under the
null of all S3 runs this campaign has judged on the same confirmation split (Bailey-LdP, variance 1 / days), so a
campaign that tries many cells on one split does not turn noise into validator queries. The bandit's reward is that
batch's validation-split SR (A4) floored at 0, not per second (a replayed batch costs almost nothing)."""

from __future__ import annotations

import math
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal, cast

import structlog

from quant_research.backtest.panel import Panel, slice_panel
from quant_research.backtest.panel_store import Window
from quant_research.catalog.store import Catalog
from quant_research.research.ledger import TrialLedger
from quant_research.search.bandit import BanditPrior, FamilyBandit
from quant_research.search.campaign import BatchSummary, CampaignState, CauseCode, Cell, classify
from quant_research.search.multiplicity import expected_max_sr
from quant_research.search.optimizers import RandomSearcher
from quant_research.search.pipeline import SearchBudget, SearchResult, missing_inputs, run_family_search
from quant_research.search.s1 import S1Config
from quant_research.strategy.space import space_of

log = structlog.get_logger(__name__)
S1_HORIZON_BARS: dict[str, int] = {"1h": 24, "4h": 6, "1d": 5}  # about one day, one day, one week ahead
MAX_WARMUP_BARS = 1_200  # longest v2 lookback (720) + smoothing + volatility windows, with room
Status = Literal["exhausted", "budget", "stopped", "gate"]


@dataclass(frozen=True, slots=True)
class CampaignConfig:
    seed: int = 0
    s1_count: int = 60
    top_k: int = 5
    promote_rate: float = 0.05
    s3_top: int = 1
    s3_random_rate: float = 0.05
    min_exposure: float = 0.05  # ADR-042's verdict precondition, also the S3 eligibility (B5)
    max_batches: int | None = None

    def identity(self) -> dict[str, object]:
        """What defines the batches' proposals and evaluations (a resumed campaign must keep it)."""
        out = asdict(self)
        out.pop("max_batches")
        return out


@dataclass(frozen=True, slots=True)
class CampaignOutcome:
    status: Status
    batches: int  # batches run in this call
    reason: str = ""


PanelProvider = Callable[[Cell], tuple[Panel, Panel]]


def missing_data(cell: Cell, selection: Panel) -> tuple[str, ...]:
    """Datasets the cell's family needs that have no rows in its selection panel (pipeline.missing_inputs)."""
    return missing_inputs(cell.family, selection)


def _number(metrics: Mapping[str, object], key: str) -> float | None:
    value = metrics.get(key)
    return float(value) if isinstance(value, int | float) and math.isfinite(float(value)) else None


def _sr_key(metrics: Mapping[str, object]) -> float:
    value = _number(metrics, "sr_daily")
    return -math.inf if value is None else value


def summarize(
    result: SearchResult, *, min_exposure: float, s3_threshold: float = 0.0, capital: float = 10_000.0
) -> BatchSummary:
    """The batch reduced to the cause rules' numbers. The best S2 is the S3-eligible one (exposure >= min, as the
    pipeline picks its top S3); its S3 is matched by `s2_trial_id`, so a fidelity sample never decides."""
    finite = [s for s in result.s1 if math.isfinite(s.result.score())]
    done = [s.outcome for s in result.s2 if s.outcome.status == "done"]
    exposed = [o for o in done if (_number(o.metrics, "gross_exposure") or 0.0) >= min_exposure]
    best = max(exposed, key=lambda o: (_sr_key(o.metrics), -o.trial_id), default=None)
    s3 = None
    if best is not None:
        s3 = next((o for o in result.s3 if o.status == "done" and o.metrics.get("s2_trial_id") == best.trial_id), None)
    costs = None if best is None else _number(best.metrics, "costs")
    return BatchSummary(
        s1_scored=len(result.s1),
        s1_finite=len(finite),
        s2_done=len(done),
        s2_exposed=len(exposed),
        best_sr_net=None if best is None else _number(best.metrics, "sr_daily"),
        best_total_return=None if best is None else _number(best.metrics, "total_return"),
        best_cost_fraction=None if costs is None else costs / capital,
        best_gross_exposure=None if best is None else _number(best.metrics, "gross_exposure"),
        s3_sr=None if s3 is None else _number(s3.metrics, "sr_daily"),
        data_missing=(),
        s3_threshold=s3_threshold,
        s3_trial_id=None if s3 is None else s3.trial_id,
    )


def s3_null_threshold(history: Sequence[Mapping[str, object]], window: str, confirmation_days: int) -> float:
    """Expected max daily SR under the null of the S3 runs judged on this window's confirmation split so far, this
    one included (N = earlier judged S3 + 1; variance of a daily SR estimate ~ 1 / days)."""
    judged = 0
    for h in history:
        summary = h.get("summary")
        if str(h.get("cell", "")).endswith(f"|{window}") and isinstance(summary, Mapping):
            judged += cast(Mapping[str, object], summary).get("s3_sr") is not None
    return expected_max_sr(judged + 1, 1.0 / max(confirmation_days, 1))


def split_window(
    panel: Panel, window: Window, *, selection_share: float = 0.7, max_warmup_bars: int = MAX_WARMUP_BARS
) -> tuple[Panel, Panel]:
    """(selection, confirmation) of a window panel: the first month is warm-up only (its ranking month may lie in a
    sealed holdout, B3); the remaining months split by `selection_share` at a month boundary. Each split keeps up to
    `max_warmup_bars` earlier rows as feature warm-up, marked not tradable and out of the metrics."""
    if not 0.0 < selection_share < 1.0:
        raise ValueError(f"selection_share must be in (0, 1), got {selection_share}")
    months = window.months()
    if len(months) < 3:
        raise ValueError(f"window {window} needs at least 3 months (warm-up, selection, confirmation)")
    starts = [_month_start(m) for m in months[1:]] + [window.end_ns]
    cut = max(1, min(len(starts) - 2, round((len(starts) - 1) * selection_share)))
    first = int(cast(int, panel.frame["decision_ns"].min()))
    tf = panel.config.timeframe_ns
    cap = max_warmup_bars * tf
    sel_from, conf_from = starts[0], starts[cut]
    selection = slice_panel(panel, sel_from, conf_from - tf, warmup_ns=min(sel_from - first, cap))
    confirmation = slice_panel(panel, conf_from, window.end_ns - tf, warmup_ns=min(conf_from - first, cap))
    return selection, confirmation


def _month_start(month: str) -> int:
    return int(datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC).timestamp()) * 10**9


def batch_seed(seed: int, cell: Cell, pull: int) -> int:
    return (seed * 1_000_003 + zlib.crc32(cell.key.encode("utf-8")) * 31 + pull) % (2**31)


def _days(panel: Panel) -> int:
    lo = panel.evaluate_from_ns or int(cast(int, panel.frame["decision_ns"].min()))
    hi = int(cast(int, panel.frame["decision_ns"].max()))
    return max(1, (hi - lo) // (86_400 * 10**9))


def run_batch(
    state: CampaignState,
    cell: Cell,
    *,
    ledger: TrialLedger,
    catalog: Catalog,
    panels: PanelProvider,
    config: CampaignConfig,
) -> tuple[CauseCode, BatchSummary, float]:
    selection, confirmation = panels(cell)
    missing = missing_data(cell, selection)
    if missing:
        return "data_missing", BatchSummary(0, 0, 0, 0, None, None, None, None, None, missing), 0.0
    pull = state.pulls.get(cell.key, 0)
    seed = batch_seed(config.seed, cell, pull)
    one_arm = FamilyBandit((cell.space,), BanditPrior(min_pulls=0.0))  # only for the A4 validation run
    result = run_family_search(
        ledger,
        cell.space,
        RandomSearcher(space_of(cell.space), seed),  # one study per batch: TPE would only see its random start
        selection=selection,
        confirmation=confirmation,
        catalog=catalog,
        config={"campaign": state.run_id, "cell": cell.key, "pull": pull, "seed": seed},
        s1=S1Config(horizon_bars=S1_HORIZON_BARS[cell.timeframe]),
        budget=SearchBudget(
            s1_count=config.s1_count,
            top_k=config.top_k,
            promote_rate=config.promote_rate,
            s3_top=config.s3_top,
            s3_random_rate=config.s3_random_rate,
            min_s3_exposure=config.min_exposure,
        ),
        seed=seed,
        fold=f"{state.run_id}:{cell.key}:{pull}",
        bandit=one_arm,
    )
    threshold = s3_null_threshold(state.history, cell.window, _days(confirmation))
    summary = summarize(result, min_exposure=config.min_exposure, s3_threshold=threshold)
    validation = None if result.validation is None else _number(result.validation.metrics, "sr_daily")
    reward = max(0.0, validation or 0.0)
    return classify(summary, min_exposure=config.min_exposure), summary, reward


def run_campaign(
    state: CampaignState,
    bandit: FamilyBandit,
    *,
    ledger: TrialLedger,
    catalog: Catalog,
    panels: PanelProvider,
    config: CampaignConfig,
    gate: Callable[[], tuple[bool, str]],
    stop: Callable[[], bool],
    persist: Callable[[CampaignState, FamilyBandit], None],
    out_of_time: Callable[[], bool] = lambda: False,
) -> CampaignOutcome:
    """Run batches until nothing is eligible, the batch or time budget is used, the stop check fires or the gate
    refuses. The state and the bandit are persisted after every batch. A batch that raises is recorded with cause
    `error` (its cell retires, the trials it wrote stay in the ledger) so one bad cell cannot stall the campaign."""
    ran = 0
    while True:
        if config.max_batches is not None and ran >= config.max_batches:
            return CampaignOutcome("budget", ran, "batch budget used")
        if out_of_time():
            return CampaignOutcome("budget", ran, "time budget used")
        if stop():
            return CampaignOutcome("stopped", ran, "stop requested")
        ok, why = gate()
        if not ok:
            return CampaignOutcome("gate", ran, why)
        cell = state.next_cell(bandit, seed=config.seed, seq=len(state.history))
        if cell is None:
            return CampaignOutcome("exhausted", ran, "no eligible cell")
        try:
            cause, summary, reward = run_batch(
                state, cell, ledger=ledger, catalog=catalog, panels=panels, config=config
            )
        except Exception as e:  # job-runner boundary: record, retire the cell, continue (never swallowed silently)
            log.exception("campaign.batch_failed", cell=cell.key)
            cause, reward = "error", 0.0
            summary = BatchSummary(0, 0, 0, 0, None, None, None, None, None, (), error=f"{type(e).__name__}: {e}")
        state.record(cell, cause, summary, reward=reward)
        bandit.update(cell.key, reward)
        persist(state, bandit)
        ran += 1
