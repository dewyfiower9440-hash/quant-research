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
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Literal, cast

import numpy as np
import structlog
from numpy.typing import NDArray

from quant_research.backtest.panel import Panel, slice_panel
from quant_research.backtest.panel_store import Window
from quant_research.backtest.run import RunConfig, experiment_config, run_backtest, unblob
from quant_research.catalog.store import Catalog
from quant_research.research.ledger import TrialLedger, experiment_key
from quant_research.search.bandit import BanditPrior, FamilyBandit
from quant_research.search.campaign import (
    WINDOWS,
    Availability,
    BatchSummary,
    CampaignState,
    CauseCode,
    Cell,
    classify,
    next_round,
)
from quant_research.search.cost_model import spec_ops
from quant_research.search.model_cells import model_points
from quant_research.search.multiplicity import expected_max_sr, s3_power_table
from quant_research.search.optimizers import FixedSearcher, RandomSearcher, Searcher
from quant_research.search.pipeline import (
    DATA_COLUMNS,
    SearchBudget,
    SearchResult,
    existing_trial,
    missing_inputs,
    outcome_from_trial,
    point_json,
    run_family_search,
    split_for_reward,
)
from quant_research.search.s1 import S1Config
from quant_research.strategy.space import INFEASIBLE_V3, SPACE_V3_SUFFIX, Point, Space, build_point, space_of
from quant_research.strategy.templates import MODEL_FAMILIES

F64 = NDArray[np.float64]
log = structlog.get_logger(__name__)
S1_HORIZON_BARS: dict[str, int] = {"1h": 24, "4h": 6, "1d": 5}  # about one day, one day, one week ahead
MAX_WARMUP_BARS = 1_200  # longest v2 lookback (720) + smoothing + volatility windows, with room
Status = Literal["exhausted", "budget", "stopped", "gate", "waiting_data"]


@dataclass(frozen=True, slots=True)
class Rounds:
    """ADR-049 rounds: the pre-registered grid groups, the research coverage (read when a round is computed) and a
    hook run before a round starts (the caller drops its in-process panel cache: reopened cells need new panels)."""

    groups: Sequence[Sequence[Cell]]
    available: Callable[[], Availability]
    on_start: Callable[[], None] = lambda: None


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
    space_version: int = 2  # 3: the capped spaces of ADR-051 (`family@timeframe/v3`)

    def __post_init__(self) -> None:
        if self.space_version not in (2, 3):
            raise ValueError(f"space_version must be 2 or 3, got {self.space_version}")

    def identity(self) -> dict[str, object]:
        """What defines the batches' proposals and evaluations (a resumed campaign must keep it)."""
        out = asdict(self)
        out.pop("max_batches")
        if self.space_version == 2:  # campaigns saved before ADR-051 have no such key and must still resume
            out.pop("space_version")
        return out


def space_name(cell: Cell, version: int) -> str:
    """The registered space of a cell under a space version (model cells exist from version 3 on)."""
    if version != 3 and cell.family in MODEL_FAMILIES:
        raise ValueError(f"{cell.key}: model cells need space version 3 (ADR-053)")
    return cell.space + (SPACE_V3_SUFFIX if version == 3 else "")


MODEL_VALIDATION_SHARE = 0.25  # the last quarter of selection: where a model cell's points compete


def is_model(cell: Cell) -> bool:
    """ADR-053: a cell whose family combines other families' signals (fixed points, no search)."""
    return cell.family in MODEL_FAMILIES


def infeasible(cell: Cell, version: int) -> bool:
    """ADR-051: the cell's (family, timeframe) has no v3 space (an axis starts beyond the lookback cap)."""
    return version == 3 and cell.space in INFEASIBLE_V3


@dataclass(frozen=True, slots=True)
class CampaignOutcome:
    status: Status
    batches: int  # batches run in this call
    reason: str = ""


PanelProvider = Callable[[Cell], tuple[Panel, Panel]]


FUNDING_DATASET = DATA_COLUMNS["funding"][1]


def missing_data(cell: Cell, panel: Panel, *, require_funding: bool = False) -> tuple[str, ...]:
    """Datasets the cell's family needs that the panel does not cover (pipeline.missing_inputs). With
    `require_funding` (ADR-052, v3 campaigns) every cell also needs the funding rate column and at least one
    settlement: a perpetual backtest without funding cash flows is not judged."""
    if not require_funding:
        return missing_inputs(cell.family, panel)
    missing = missing_inputs(cell.family, panel, also=("funding",))
    if panel.funding.height == 0 and FUNDING_DATASET not in missing:
        missing = (*missing, FUNDING_DATASET)
    return missing


def requires_funding(config: CampaignConfig) -> bool:
    """ADR-052 applies to the campaigns pre-registered after it (space version 3); c1 keeps its rule."""
    return config.space_version == 3


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
    funding = None if best is None else _number(best.metrics, "funding")
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
        best_funding_fraction=None if funding is None else funding / capital,
    )


def _judged(history: Sequence[Mapping[str, object]], window: str) -> int:
    """S3 runs judged on this window's confirmation split: a cell's own S3 in this window, plus the cross S3 of a
    cell of the other window (ADR-049 decision 3 runs it on this split)."""
    n = 0
    for h in history:
        summary = h.get("summary")
        if not isinstance(summary, Mapping):
            continue
        s = cast(Mapping[str, object], summary)
        mine = str(h.get("cell", "")).endswith(f"|{window}")
        n += (s.get("s3_sr") is not None) if mine else (s.get("cross_sr") is not None)
    return n


def earlier_histories(states: Sequence[CampaignState], own_run: str) -> list[list[dict[str, object]]]:
    """One history per other run id: the longest (histories only grow, so a stale copy such as campaign-c1-old.json
    neither counts a run twice nor replaces its current file and lowers the null N)."""
    best: dict[str, list[dict[str, object]]] = {}
    for s in states:
        if s.run_id != own_run and len(s.history) >= len(best.get(s.run_id, [])):
            best[s.run_id] = s.history
    return list(best.values())


def judged_s3_counts(histories: Sequence[Sequence[Mapping[str, object]]]) -> dict[str, int]:
    """S3 runs judged per research window in earlier campaigns (ADR-049 decision 2: the null N carries over)."""
    out: dict[str, int] = {}
    for window in WINDOWS:
        n = sum(_judged(h, window) for h in histories)
        if n:
            out[window] = n
    return out


def s3_null_threshold(
    history: Sequence[Mapping[str, object]], window: str, confirmation_days: int, *, prior: int = 0
) -> float:
    """Expected max daily SR under the null of the S3 runs judged on this window's confirmation split so far, this
    one included (N = earlier judged S3, `prior` ones of earlier campaigns among them, + 1; variance ~ 1 / days)."""
    return expected_max_sr(prior + _judged(history, window) + 1, 1.0 / max(confirmation_days, 1))


def pooled_null_threshold(history: Sequence[Mapping[str, object]], prior: Mapping[str, int], days: int) -> float:
    """ADR-049 decision 3: expected max daily SR under the null of every S3 judged on the two confirmation splits
    (earlier campaigns included) plus this batch's two (own + cross); variance ~ 1 / pooled days."""
    return expected_max_sr(pooled_n(history, prior), 1.0 / max(days, 1))


def pooled_n(history: Sequence[Mapping[str, object]], prior: Mapping[str, int]) -> int:
    """The N of the pooled bar for the next batch: every S3 judged on the two splits so far, plus its two."""
    return sum(prior.get(w, 0) + _judged(history, w) for w in WINDOWS) + 2


def pooled_power(
    history: Sequence[Mapping[str, object]], prior: Mapping[str, int], days: int
) -> tuple[int, float, list[tuple[float, float]]]:
    """(N, the pooled bar as an annual SR, power per true annual SR) for the next batch (design v2.3 section 3)."""
    n = pooled_n(history, prior)
    bar_ann, rows = s3_power_table(days, n)
    return n, bar_ann, rows


@dataclass(frozen=True, slots=True)
class CrossResult:
    trial_id: int
    cross_sr: float | None  # the cross S3's own daily SR
    pooled_sr: float | None  # SR of the own S3 and the cross S3 daily pnl joined
    days: int  # pooled days


def _daily_pnl(ledger: TrialLedger, trial_id: int) -> F64:
    row = ledger.store.conn.execute("select pnl_daily from backtest_run where trial_id = ?", (trial_id,)).fetchone()
    return unblob(bytes(row[0])) if row is not None and row[0] is not None else np.zeros(0)


def cross_confirm(
    ledger: TrialLedger,
    catalog: Catalog,
    space: Space,
    point: Point,
    own_s3_trial: int,
    other_confirmation: Panel,
    *,
    fold: str,
    seed: int,
) -> CrossResult | None:
    """Run (or reuse, on a replayed batch) the spec's S3 on the other window's confirmation split as one trial in fold
    `<fold>:x`, and pool its daily pnl with the own S3's. Each split is a separate backtest: no run crosses the sealed
    holdout between the research windows (v2.2 §2.1). None when the cross run was refused."""
    spec = build_point(space, point)
    cfg = RunConfig(engine="event", seed=seed)  # the pipeline's S3 engine
    h, conf = experiment_config(spec, other_confirmation, catalog, cfg)
    fold_x = f"{fold}:x"
    prior = existing_trial(ledger, experiment_key(h, conf), "S3", fold_x)
    if prior is not None:
        out = outcome_from_trial(ledger, prior)
    else:
        tags: dict[str, object] = {
            "point": point_json(point),
            "cross_of": own_s3_trial,
            "rows": other_confirmation.frame.height,
            "ops": spec_ops(spec),
        }
        out = run_backtest(spec, other_confirmation, catalog, ledger, cfg, stage="S3", fold=fold_x, tags=tags)
    if out.status != "done":
        return None
    joined = np.concatenate([_daily_pnl(ledger, own_s3_trial), _daily_pnl(ledger, out.trial_id)])
    sd = float(np.std(joined, ddof=1)) if len(joined) > 1 else 0.0
    pooled = float(np.mean(joined)) / sd if sd > 0 else None
    return CrossResult(out.trial_id, _number(out.metrics, "sr_daily"), pooled, len(joined))


def split_window(
    panel: Panel,
    window: Window,
    *,
    selection_share: float = 0.7,
    max_warmup_bars: int = MAX_WARMUP_BARS,
    rule: SplitRule | None = None,
) -> tuple[Panel, Panel]:
    """(selection, confirmation) of a window panel: the first month is warm-up only (its ranking month may lie in a
    sealed holdout, B3); the remaining months split by `selection_share` at a month boundary. Each split keeps up to
    `max_warmup_bars` earlier rows as feature warm-up, marked not tradable and out of the metrics."""
    sel_from, conf_from, _end = split_bounds(window, selection_share=selection_share, rule=rule)
    first = int(cast(int, panel.frame["decision_ns"].min()))
    tf = panel.config.timeframe_ns
    cap = max_warmup_bars * tf
    selection = slice_panel(panel, sel_from, conf_from - tf, warmup_ns=min(sel_from - first, cap))
    confirmation = slice_panel(panel, conf_from, window.end_ns - tf, warmup_ns=min(conf_from - first, cap))
    return selection, confirmation


@dataclass(frozen=True, slots=True)
class SplitRule:
    """How a research window is cut. v2: one warm-up month. v3 (ADR-051): the first 90 days (three months) are
    warm-up, and the split must leave 9 selection and 4 confirmation months."""

    warmup_months: int = 1
    min_selection_months: int = 1
    min_confirmation_months: int = 1

    @staticmethod
    def of(space_version: int) -> SplitRule:
        return SplitRule(3, 9, 4) if space_version == 3 else SplitRule()


def split_bounds(
    window: Window, *, selection_share: float = 0.7, rule: SplitRule | None = None
) -> tuple[int, int, int]:
    """(selection start, confirmation start, end) of a research window, all month boundaries: the first
    `warmup_months` are warm-up, the rest splits by `selection_share`. Needs no panel (reports use it)."""
    r = rule or SplitRule()
    if not 0.0 < selection_share < 1.0:
        raise ValueError(f"selection_share must be in (0, 1), got {selection_share}")
    months = window.months()
    if len(months) < r.warmup_months + 2:
        raise ValueError(
            f"window {window} needs at least {r.warmup_months + 2} months (warm-up, selection, confirmation)"
        )
    starts = [_month_start(m) for m in months[r.warmup_months :]] + [window.end_ns]
    cut = max(1, min(len(starts) - 2, round((len(starts) - 1) * selection_share)))
    if cut < r.min_selection_months or len(starts) - 1 - cut < r.min_confirmation_months:
        raise ValueError(
            f"window {window}: {cut} selection and {len(starts) - 1 - cut} confirmation months, the rule needs "
            f"{r.min_selection_months} and {r.min_confirmation_months}"
        )
    return starts[0], starts[cut], window.end_ns


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
    prior_s3: Mapping[str, int] | None = None,
) -> tuple[CauseCode, BatchSummary, float]:
    if infeasible(cell, config.space_version):  # before any panel is loaded: nothing is evaluated, no N
        return "infeasible", BatchSummary(0, 0, 0, 0, None, None, None, None, None, (), infeasible=True), 0.0
    selection, confirmation = panels(cell)
    funded = requires_funding(config)
    missing = tuple(
        dict.fromkeys(
            [
                *missing_data(cell, selection, require_funding=funded),
                *(missing_data(cell, confirmation, require_funding=True) if funded else ()),
            ]
        )
    )
    if missing:
        return "data_missing", BatchSummary(0, 0, 0, 0, None, None, None, None, None, missing), 0.0
    pull = state.pulls.get(cell.key, 0)
    seed = batch_seed(config.seed, cell, pull)
    name = space_name(cell, config.space_version)
    horizon = S1_HORIZON_BARS[cell.timeframe]
    batch_config: dict[str, object] = {"campaign": state.run_id, "cell": cell.key, "pull": pull, "seed": seed}
    if is_model(cell):
        # ADR-053: the weights are estimated on the fit part of selection and the points compete on the validation
        # part, which the weights never saw; nothing is searched, so every point goes to S2
        fit, selection = split_for_reward(selection, MODEL_VALIDATION_SHARE)
        points, fitted = model_points(space_of(name), fit)
        del fit
        batch_config["model"] = fitted
        if not points:  # nothing could be estimated (no positive IC and no rows): nothing to evaluate
            empty = BatchSummary(0, 0, 0, 0, None, None, None, None, None, ())
            return classify(empty, min_exposure=config.min_exposure), empty, 0.0
        searcher: Searcher = FixedSearcher(space_of(name), points)
        budget = SearchBudget(
            s1_count=len(points),
            top_k=len(points),
            promote_rate=1.0,  # whatever the S1 score: a point without enough S1 events is still backtested
            s3_top=1,
            s3_random_rate=0.0,
            min_s3_exposure=config.min_exposure,
        )
        bandit = None  # the validation part is already the selection of this batch
    else:
        searcher = RandomSearcher(space_of(name), seed)  # one study per batch: TPE would only see its random start
        budget = SearchBudget(
            s1_count=config.s1_count,
            top_k=config.top_k,
            promote_rate=config.promote_rate,
            s3_top=config.s3_top,
            s3_random_rate=config.s3_random_rate,
            min_s3_exposure=config.min_exposure,
        )
        bandit = FamilyBandit((name,), BanditPrior(min_pulls=0.0))  # only for the A4 validation run
    result = run_family_search(
        ledger,
        name,
        searcher,
        selection=selection,
        confirmation=confirmation,
        catalog=catalog,
        config=batch_config,
        s1=S1Config(horizon_bars=horizon),
        budget=budget,
        seed=seed,
        fold=f"{state.run_id}:{cell.key}:{pull}",
        bandit=bandit,
    )
    prior = (prior_s3 or {}).get(cell.window, 0)
    threshold = s3_null_threshold(state.history, cell.window, _days(confirmation), prior=prior)
    summary = summarize(result, min_exposure=config.min_exposure, s3_threshold=threshold)
    del selection, confirmation  # review (step 4): one window's panels in memory at a time, also for the cross run
    if (summary.best_sr_net or 0.0) > 0 and (summary.s3_sr or 0.0) > 0 and summary.s3_trial_id is not None:
        own = summary
        summary = guarded_cross(
            own,
            lambda: with_cross(
                own,
                result,
                state,
                cell,
                ledger=ledger,
                catalog=catalog,
                panels=panels,
                seed=seed,
                fold=f"{state.run_id}:{cell.key}:{pull}",
                prior_s3=prior_s3 or {},
                require_funding=funded,
            ),
            cell=cell,
        )
    validation = None if result.validation is None else _number(result.validation.metrics, "sr_daily")
    if is_model(cell):  # its S2 already ran on the validation part of selection
        validation = summary.best_sr_net
    reward = max(0.0, validation or 0.0)
    return classify(summary, min_exposure=config.min_exposure), summary, reward


def guarded_cross(summary: BatchSummary, cross: Callable[[], BatchSummary], *, cell: Cell) -> BatchSummary:
    """Run the cross step; MemoryError / PermissionError propagate (transient: the campaign stops, the replay reuses
    every trial), any other failure keeps the own S3 in the summary (the null N still counts it) as `error`."""
    try:
        return cross()
    except (MemoryError, PermissionError):
        raise
    except Exception as e:  # job-step boundary: logged with the traceback, recorded, never swallowed silently
        log.exception("campaign.cross_failed", cell=cell.key)
        return replace(summary, error=f"cross: {type(e).__name__}: {e}")


def with_cross(
    summary: BatchSummary,
    result: SearchResult,
    state: CampaignState,
    cell: Cell,
    *,
    ledger: TrialLedger,
    catalog: Catalog,
    panels: PanelProvider,
    seed: int,
    fold: str,
    prior_s3: Mapping[str, int],
    require_funding: bool = False,
) -> BatchSummary:
    """ADR-049 decision 3 for a batch whose best S2 and its own S3 are both positive: the same spec on the other
    window's confirmation split, pooled with the own S3 and judged against the two splits' best-of-N null bar."""
    s3 = next((o for o in result.s3 if o.trial_id == summary.s3_trial_id), None)
    s2_id = None if s3 is None else s3.metrics.get("s2_trial_id")
    source = next((s.source for s in result.s2 if s.outcome.trial_id == s2_id), None)
    if source is None:
        return summary
    other = Cell(cell.family, cell.timeframe, cell.top_m, next(w for w in WINDOWS if w != cell.window))
    _selection, other_confirmation = panels(other)
    if missing_data(other, other_confirmation, require_funding=require_funding):
        return summary  # the other window lacks the family's data: no cross run, the cell is below_bar (no_cross)
    space = space_of(source.candidate.point.family)  # the space the point was drawn from (v2 or v3)
    x = cross_confirm(
        ledger,
        catalog,
        space,
        source.candidate.point,
        summary.s3_trial_id or 0,
        other_confirmation,
        fold=fold,
        seed=seed,
    )
    if x is None:
        return summary
    bar = pooled_null_threshold(state.history, prior_s3, x.days)
    return replace(summary, cross_sr=x.cross_sr, pooled_sr=x.pooled_sr, pooled_threshold=bar, cross_trial_id=x.trial_id)


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
    rounds: Rounds | None = None,
    prior_s3: Mapping[str, int] | None = None,
) -> CampaignOutcome:
    """Run batches until the batch or time budget is used, the stop check fires or the gate refuses. When nothing is
    eligible: without `rounds` the campaign is `exhausted`; with them the next ADR-049 round starts (persisted), and
    only a round with nothing to open ends the call as `waiting_data` (the next scheduled tick asks again). The
    state and the bandit are persisted after every batch. A batch that raises is recorded with cause `error` (its
    cell retires, the trials it wrote stay in the ledger) so one bad cell cannot stall the campaign."""
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
            if rounds is None:
                return CampaignOutcome("exhausted", ran, "no eligible cell")
            try:
                available = rounds.available()
            except PermissionError as e:  # the coverage could not be read now (a state file mid-replace): pause
                log.warning("campaign.round_transient", error=str(e))
                return CampaignOutcome("gate", ran, f"transient {type(e).__name__}: {e}")
            plan = next_round(state, available, rounds.groups)
            if plan.empty:
                if state.data_requests != list(plan.waiting):
                    state.data_requests = list(plan.waiting)
                    persist(state, bandit)  # the state file shows what the campaign now waits for
                waiting = ", ".join(plan.waiting) or "no data requested"
                return CampaignOutcome("waiting_data", ran, f"round {state.round} has nothing to open ({waiting})")
            rounds.on_start()
            state.start_round(plan, available)
            bandit.add_arms(c.key for c in state.all_cells())  # cells no transition reaches (1h, top30)
            log.info("campaign.round", round=state.round, reopened=list(plan.reopened), added=len(plan.added))
            persist(state, bandit)
            continue
        try:
            cause, summary, reward = run_batch(
                state, cell, ledger=ledger, catalog=catalog, panels=panels, config=config, prior_s3=prior_s3
            )
        except (MemoryError, PermissionError) as e:  # not a property of the cell: stop, nothing recorded, resumable
            log.warning("campaign.batch_transient", cell=cell.key, error=f"{type(e).__name__}: {e}")
            return CampaignOutcome("gate", ran, f"transient {type(e).__name__}: {e}")
        except Exception as e:  # job-runner boundary: record, retire the cell, continue (never swallowed silently)
            log.exception("campaign.batch_failed", cell=cell.key)
            cause, reward = "error", 0.0
            summary = BatchSummary(0, 0, 0, 0, None, None, None, None, None, (), error=f"{type(e).__name__}: {e}")
        state.record(cell, cause, summary, reward=reward)
        bandit.update(cell.key, reward)
        persist(state, bandit)
        ran += 1
