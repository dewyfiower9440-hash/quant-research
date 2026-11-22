"""Phase 4 step 10 (driver): S0 -> S1 -> promotion -> S2 -> S3 with one trial per evaluation, every proposal
told back to the searcher once, resume without duplicates, memory refusal before any trial."""

from __future__ import annotations

import pytest

from quant_research.backtest.panel import Panel, slice_panel
from quant_research.backtest.run import RunOutcome
from quant_research.catalog.store import Catalog
from quant_research.research.ledger import TrialLedger
from quant_research.search.archive import Archive
from quant_research.search.bandit import BanditPrior, FamilyBandit
from quant_research.search.cost_model import LinearMemory, MemoryBudgetExceeded
from quant_research.search.optimizers import RandomSearcher, TpeSearcher
from quant_research.search.pipeline import SearchBudget, SearchResult, point_from_json, point_json, run_family_search
from quant_research.search.s1 import S1Config
from quant_research.strategy.space import SPACES

M = 60 * 10**9
H = 60 * M
D = 24 * H
T0 = 1_767_225_600 * 10**9
DAYS = 30
CONFIG: dict[str, object] = {"data_version": "d1", "lag_versions": {"klines": 1}, "seed": 0}


def _count(ledger: TrialLedger, stage: str, fold: str | None = None) -> int:
    if fold is None:
        return int(ledger.store.conn.execute("select count(*) from trial where stage = ?", (stage,)).fetchone()[0])
    return int(
        ledger.store.conn.execute("select count(*) from trial where stage = ? and fold = ?", (stage, fold)).fetchone()[
            0
        ]
    )


def test_every_stage_is_accounted_and_the_searcher_is_told_once_per_proposal(
    env: tuple[Catalog, TrialLedger], panels: tuple[Panel, Panel]
) -> None:
    catalog, ledger = env
    sel, conf = panels
    searcher = TpeSearcher(SPACES["mean_reversion"], seed=3, n_startup_trials=5)
    bandit = FamilyBandit(tuple(SPACES), BanditPrior(min_pulls=0.0))
    archive = Archive()
    res = run_family_search(
        ledger,
        "mean_reversion",
        searcher,
        selection=sel,
        confirmation=conf,
        catalog=catalog,
        config=CONFIG,
        s1=S1Config(horizon_bars=4),
        budget=SearchBudget(s1_count=12, top_k=2, promote_rate=0.2, s3_top=1),
        seed=1,
        fold="cycle1",
        bandit=bandit,
        archive=archive,
    )
    c = res.counts()
    assert c["proposed"] == c["s0_trials"] == 12 == _count(ledger, "S0")
    assert c["s1_trials"] == c["s0_accepted"] == _count(ledger, "S1", "cycle1")
    assert c["s2_trials"] == c["promoted_top"] + c["promoted_random"] == _count(ledger, "S2", "cycle1")
    assert c["s3_trials"] == min(1, c["s2_done"]) == _count(ledger, "S3", "cycle1")  # sampling off
    assert searcher.pending() == 0 and searcher.internal_evaluations() == 12  # Optuna evaluations = proposals
    assert res.reward is not None and res.reward >= 0.0 and bandit.arms["mean_reversion"].pulls == 1.0
    assert res.archive_offers["offered"] == c["s2_done"] + sum(1 for o in res.s3 if o.status == "done")
    s = res.summary()
    assert s["best_s2_trial"] is not None and point_from_json(s["best_point"]) == res.best_s2().source.candidate.point  # type: ignore[arg-type,union-attr]
    assert set(res.cost_s) == {"S0", "S1", "S2", "S3", "val"}  # val: the bandit's reward run (A4)
    assert _count(ledger, "S2", "cycle1:val") == (1 if c["s2_done"] else 0)
    assert ledger.store.conn.execute("select count(*) from trial where status = 'running'").fetchone()[0] == 0


def test_resume_in_the_same_fold_reuses_finished_s2_and_s3_trials(
    env: tuple[Catalog, TrialLedger], panels: tuple[Panel, Panel]
) -> None:
    catalog, ledger = env
    sel, conf = panels

    def run() -> SearchResult:
        return run_family_search(
            ledger,
            "momentum",
            RandomSearcher(SPACES["momentum"], seed=5),  # same seed -> same proposals (a restart replays)
            selection=sel,
            confirmation=conf,
            catalog=catalog,
            config=CONFIG,
            s1=S1Config(horizon_bars=4),
            budget=SearchBudget(s1_count=8, top_k=2, promote_rate=0.0, s3_top=1),
            seed=2,
            fold="cycle7",
        )

    first = run()
    s2_after_first, s3_after_first = _count(ledger, "S2", "cycle7"), _count(ledger, "S3", "cycle7")
    s1_after_first = _count(ledger, "S1", "cycle7")
    assert len(first.s2) == s2_after_first and first.best_s2() is not None
    second = run()  # the restart: S0 refusals are new trials (duplicates), S1/S2/S3 results are reused
    assert _count(ledger, "S2", "cycle7") == s2_after_first and _count(ledger, "S3", "cycle7") == s3_after_first
    assert _count(ledger, "S1", "cycle7") == s1_after_first  # same fold + data version: no second evaluation
    assert _count(ledger, "S0") == 16
    # review finding 3: the restart used to end with 0 candidates (every proposal an S0 duplicate)
    b1, b2 = first.best_s2(), second.best_s2()
    assert b2 is not None and b1 is not None and b2.outcome.trial_id == b1.outcome.trial_id
    assert len(second.s1) == len(first.s1) and len(second.s2) == len(first.s2)
    dup = ledger.store.conn.execute(
        "select experiment_key, count(*) from trial where stage in ('S2','S3') and fold = 'cycle7' "
        "group by experiment_key, stage having count(*) > 1"
    ).fetchall()
    assert dup == []


def test_memory_refusal_happens_before_any_trial(env: tuple[Catalog, TrialLedger], panels: tuple[Panel, Panel]) -> None:
    catalog, ledger = env
    sel, conf = panels
    searcher = TpeSearcher(SPACES["trend"], seed=1)
    with pytest.raises(MemoryBudgetExceeded):
        run_family_search(
            ledger,
            "trend",
            searcher,
            selection=sel,
            confirmation=conf,
            catalog=catalog,
            config=CONFIG,
            s1=S1Config(),
            budget=SearchBudget(s1_count=5, memory_limit_mb=10.0),
            seed=0,
            memory_model=LinearMemory(500.0, 0.001),  # predicts well above 10 MB
        )
    assert ledger.store.conn.execute("select count(*) from trial").fetchone()[0] == 0
    assert searcher.asked() == 0
    with pytest.raises(RuntimeError):
        run_family_search(
            ledger,
            "trend",
            searcher,
            selection=sel,
            confirmation=conf,
            catalog=catalog,
            config=CONFIG,
            s1=S1Config(),
            budget=SearchBudget(s1_count=5),
            seed=0,
            gate=lambda: (False, "hot"),
        )
    assert ledger.store.conn.execute("select count(*) from trial").fetchone()[0] == 0
    with pytest.raises(ValueError):
        SearchBudget(promote_rate=1.5)
    assert point_from_json(point_json(searcher.ask())).family == "trend"


def test_a_duplicate_on_another_window_is_evaluated_again_not_reused(
    env: tuple[Catalog, TrialLedger], panels: tuple[Panel, Panel]
) -> None:
    """Review 2 finding 8: the reuse key ignored the slice window (slice_panel keeps the file list), so the same
    fold label on another selection window reused S1 results computed on other data."""
    catalog, ledger = env
    sel, conf = panels
    grid = sel.frame["decision_ns"].unique().sort().to_list()
    shorter = slice_panel(sel, int(grid[len(grid) // 3]), int(grid[-1]))

    def run(panel: Panel) -> SearchResult:
        return run_family_search(
            ledger,
            "momentum",
            RandomSearcher(SPACES["momentum"], seed=5),
            selection=panel,
            confirmation=conf,
            catalog=catalog,
            config=CONFIG,
            s1=S1Config(horizon_bars=4),
            budget=SearchBudget(s1_count=6, top_k=1, promote_rate=0.0, s3_top=0),
            seed=2,
            fold="same-label",
        )

    first = run(sel)
    before = _count(ledger, "S1", "same-label")
    second = run(shorter)
    assert len(second.s1) == len(first.s1) and len(first.s1) > 0
    assert _count(ledger, "S1", "same-label") == before + len(second.s1)  # every one evaluated again


def test_reuse_finds_the_trial_of_the_same_window_among_several(
    env: tuple[Catalog, TrialLedger], panels: tuple[Panel, Panel]
) -> None:
    """Review 3: the reuse lookup took the OLDEST trial and compared its window, so after W1 and W2 a restart on
    W2 evaluated everything again."""
    catalog, ledger = env
    sel, conf = panels
    grid = sel.frame["decision_ns"].unique().sort().to_list()
    w2 = slice_panel(sel, int(grid[len(grid) // 3]), int(grid[-1]))

    def run(panel: Panel) -> SearchResult:
        return run_family_search(
            ledger,
            "momentum",
            RandomSearcher(SPACES["momentum"], seed=5),
            selection=panel,
            confirmation=conf,
            catalog=catalog,
            config=CONFIG,
            s1=S1Config(horizon_bars=4),
            budget=SearchBudget(s1_count=6, top_k=1, promote_rate=0.0, s3_top=0),
            seed=2,
            fold="two-windows",
        )

    run(sel)
    run(w2)
    before = _count(ledger, "S1", "two-windows")
    again = run(w2)
    assert len(again.s1) > 0 and _count(ledger, "S1", "two-windows") == before  # W2's own trials reused


def test_the_bandit_reward_comes_from_a_validation_split_inside_selection_never_from_s3(
    env: tuple[Catalog, TrialLedger], panels: tuple[Panel, Panel]
) -> None:
    """v2.2 A4 (analysis A-4, ADR-046; design v2.1 §8): with a bandit, S1/S2 run on the first 75 % of the selection
    block, the best S2 candidate is re-run on the last 25 % (its own trial, fold '<fold>:val') and only that run
    rewards the bandit; the confirmation split (S3) does not enter it."""
    catalog, ledger = env
    sel, conf = panels

    def run(fold: str, confirmation: Panel | None) -> tuple[SearchResult, FamilyBandit]:
        bandit = FamilyBandit(tuple(SPACES), BanditPrior(min_pulls=0.0))
        res = run_family_search(
            ledger,
            "mean_reversion",
            RandomSearcher(SPACES["mean_reversion"], seed=3),
            selection=sel,
            confirmation=confirmation,
            catalog=catalog,
            config=CONFIG,
            s1=S1Config(horizon_bars=4),
            budget=SearchBudget(s1_count=10, top_k=2, promote_rate=0.0, s3_top=1),
            seed=1,
            fold=fold,
            bandit=bandit,
        )
        return res, bandit

    with_s3, b1 = run("a4-with-s3", conf)
    without_s3, b2 = run("a4-no-s3", None)
    assert with_s3.validation is not None and with_s3.validation.status == "done"
    assert _count(ledger, "S2", "a4-with-s3:val") == 1  # the validation run is a trial of its own
    # S1 / S2 saw only the fit part: fewer rows than the whole selection block
    s1_rows = {int(str(s.result.metrics().get("ic_samples", 0))) for s in with_s3.s1}
    assert with_s3.s1 and max(s1_rows) > 0
    assert int(str(with_s3.validation.metrics.get("days"))) < int(str(with_s3.s2[0].outcome.metrics.get("days")))
    assert with_s3.s3 and not without_s3.s3
    assert with_s3.counts()["val_trials"] == 1 and with_s3.summary()["val_trial"] == with_s3.validation.trial_id
    # the reward is a function of the validation run and the cost only (A4 review: compare at a FIXED cost; the
    # batches' wall times differ, and with a 0-floored reward an end-to-end comparison could not fail)
    assert b1.arms["mean_reversion"].pulls == b2.arms["mean_reversion"].pulls == 1.0


def test_batch_reward_ignores_s3_and_does_not_deflate_a_single_held_out_run() -> None:
    """A4 review findings 2-3: the reward must not move when the S3 results change, and a single run on unseen
    data is not a maximum over trials (deflating it by E[max of the S2 trials] floored almost every reward)."""
    from dataclasses import replace

    from quant_research.search.pipeline import batch_reward

    def outcome(tid: int, sr: float) -> RunOutcome:
        return RunOutcome(tid, "done", None, {"sr_daily": sr, "days": 30}, None)

    base = _result_with(validation=outcome(9, 0.2), s3=[outcome(10, -1.0)])
    other_s3 = replace(base, s3=[outcome(11, 3.0)])
    assert batch_reward(base, 10.0) == batch_reward(other_s3, 10.0) > 0.0  # S3 changed: same reward
    worse = replace(base, validation=outcome(9, -0.1))
    assert batch_reward(worse, 10.0) == 0.0  # a negative held-out SR earns nothing
    assert batch_reward(replace(base, validation=None), 10.0) == 0.0


def _result_with(*, validation: RunOutcome | None, s3: list[RunOutcome]) -> SearchResult:
    from quant_research.search.promotion import Selection
    from quant_research.search.s0_stage import S0Outcome

    return SearchResult(
        "trend",
        "random",
        "f",
        S0Outcome(0, 0, 0, 0),
        [],
        Selection([], [], [], 0, 0.0),
        [],
        s3,
        {},
        validation=validation,
    )


def test_the_reward_split_refuses_a_selection_too_short_to_split(panels: tuple[Panel, Panel]) -> None:
    from quant_research.search.pipeline import split_for_reward

    sel, _ = panels
    grid = sel.frame["decision_ns"].unique().sort().to_list()
    tiny = slice_panel(sel, int(grid[-3]), int(grid[-1]))
    with pytest.raises(ValueError, match="too short"):
        split_for_reward(tiny, 0.25)
    fit, val = split_for_reward(sel, 0.25)
    assert fit.frame.height > 0 and val.frame.height > 0


def test_s3_takes_the_top_s2_survivors_plus_a_random_share_of_the_rest(
    env: tuple[Catalog, TrialLedger], panels: tuple[Panel, Panel]
) -> None:
    """v2.2 A6 (analysis A-6): one S3 per batch left the S1-vs-S3 rank correlation with 2-4 pairs per family."""
    catalog, ledger = env
    sel, conf = panels

    def run(rate: float, fold: str) -> SearchResult:
        return run_family_search(
            ledger,
            "momentum",
            RandomSearcher(SPACES["momentum"], seed=8),
            selection=sel,
            confirmation=conf,
            catalog=catalog,
            config=CONFIG,
            s1=S1Config(horizon_bars=4),
            budget=SearchBudget(s1_count=10, top_k=4, promote_rate=0.0, s3_top=1, s3_random_rate=rate),
            seed=3,
            fold=fold,
        )

    every = run(1.0, "a6-all")
    only_top = run(0.0, "a6-top")
    assert every.counts()["s2_done"] >= 2  # precondition: something to sample from
    assert len(every.s3) == every.counts()["s2_done"] and len(only_top.s3) == 1
    with pytest.raises(ValueError):
        SearchBudget(s3_random_rate=1.5)


def test_predicted_costs_use_the_evaluated_candidates_rows_and_ops(
    env: tuple[Catalog, TrialLedger], panels: tuple[Panel, Panel]
) -> None:
    """v2.2 A7 (analysis A-7): the batch prediction used a constant 20 ops and the EXPECTED S2 count."""
    import json

    from quant_research.search.cost_model import LinearCost

    catalog, ledger = env
    sel, conf = panels
    model = {"S1": LinearCost(0.01, 1e-6), "S2": LinearCost(0.02, 2e-6)}
    res = run_family_search(
        ledger,
        "trend",
        RandomSearcher(SPACES["trend"], seed=2),
        selection=sel,
        confirmation=conf,
        catalog=catalog,
        config=CONFIG,
        s1=S1Config(horizon_bars=4),
        budget=SearchBudget(s1_count=8, top_k=2, promote_rate=0.0, s3_top=0),
        seed=1,
        fold="a7",
        cost_model=model,
    )
    rows_ops = [
        (int(m["rows"]), int(m["ops"]))
        for (raw,) in ledger.store.conn.execute("select metrics_json from trial where stage = 'S1' and fold = 'a7'")
        for m in [json.loads(str(raw))]
    ]
    assert rows_ops and res.predicted["S1"] == pytest.approx(sum(model["S1"].predict(r, o) for r, o in rows_ops))
    s2_rows_ops = [
        (int(m["rows"]), int(m["ops"]))
        for (raw,) in ledger.store.conn.execute("select metrics_json from trial where stage = 'S2' and fold = 'a7'")
        for m in [json.loads(str(raw))]
    ]
    assert res.predicted["S2"] == pytest.approx(sum(model["S2"].predict(r, o) for r, o in s2_rows_ops))


def test_s3_sampling_is_off_by_default_and_its_time_is_not_charged_to_the_bandit_reward(
    env: tuple[Catalog, TrialLedger], panels: tuple[Panel, Panel]
) -> None:
    """A6-A12 review finding 5: a 5 % default silently changed the null harness's candidate sets and charged the
    fidelity runs' wall time to the bandit reward."""
    assert SearchBudget().s3_random_rate == 0.0
    catalog, ledger = env
    sel, conf = panels
    bandit = FamilyBandit(tuple(SPACES), BanditPrior(min_pulls=0.0))
    res = run_family_search(
        ledger,
        "momentum",
        RandomSearcher(SPACES["momentum"], seed=8),
        selection=sel,
        confirmation=conf,
        catalog=catalog,
        config=CONFIG,
        s1=S1Config(horizon_bars=4),
        budget=SearchBudget(s1_count=10, top_k=4, promote_rate=0.0, s3_top=1, s3_random_rate=1.0),
        seed=3,
        fold="sample-cost",
        bandit=bandit,
    )
    assert "S3_sample" in res.cost_s and res.cost_s["S3_sample"] > 0.0
    assert "S3_sample" not in res.reward_cost_stages


def test_predicted_costs_count_only_trials_created_in_this_batch(
    env: tuple[Catalog, TrialLedger], panels: tuple[Panel, Panel]
) -> None:
    """A6-A12 review finding 6: reused trials were predicted at full cost but measured at lookup cost."""
    from quant_research.search.cost_model import LinearCost

    catalog, ledger = env
    sel, conf = panels
    model = {"S1": LinearCost(0.01, 1e-6), "S2": LinearCost(0.02, 2e-6)}

    def run() -> SearchResult:
        return run_family_search(
            ledger,
            "trend",
            RandomSearcher(SPACES["trend"], seed=2),
            selection=sel,
            confirmation=conf,
            catalog=catalog,
            config=CONFIG,
            s1=S1Config(horizon_bars=4),
            budget=SearchBudget(s1_count=8, top_k=2, promote_rate=0.0, s3_top=0),
            seed=1,
            fold="a7-resume",
            cost_model=model,
        )

    first, again = run(), run()
    assert first.predicted["S1"] > 0 and again.predicted.get("S1", 0.0) == 0.0  # everything reused the 2nd time


def test_an_s2_survivor_below_the_minimum_exposure_is_not_an_s3_candidate(
    env: tuple[Catalog, TrialLedger], panels: tuple[Panel, Panel]
) -> None:
    """v2.2 B5 (D5): a strategy that is nearly always flat cannot pass the verdict's exposure precondition
    (ADR-042), so S3 (and later a holdout query) is not spent on it; the top-S3 slot goes to the best survivor
    with mean gross exposure >= `min_s3_exposure`. Off by default (0.0)."""
    catalog, ledger = env
    sel, conf = panels

    def run(minimum: float, fold: str) -> SearchResult:
        return run_family_search(
            ledger,
            "momentum",
            RandomSearcher(SPACES["momentum"], seed=8),
            selection=sel,
            confirmation=conf,
            catalog=catalog,
            config=CONFIG,
            s1=S1Config(horizon_bars=4),
            budget=SearchBudget(s1_count=10, top_k=4, promote_rate=0.0, s3_top=1, min_s3_exposure=minimum),
            seed=3,
            fold=fold,
        )

    base = run(0.0, "b5-any")
    assert base.counts()["s2_done"] >= 1 and len(base.s3) == 1  # precondition: an S3 ran without the minimum
    assert run(1e9, "b5-none").s3 == []  # nobody reaches an absurd minimum: no S3
    with pytest.raises(ValueError):
        SearchBudget(min_s3_exposure=-0.1)


def test_a_v2_space_runs_through_the_pipeline_and_a_timeframe_mismatch_is_refused_before_any_trial(
    env: tuple[Catalog, TrialLedger], panels: tuple[Panel, Panel]
) -> None:
    """B5 review 1 and 4: `run_family_search` still checked `family in SPACES` (v1 only), and S1 had no
    timeframe check, so 4h / 1d points on a 1h panel would have been scored by S1 on the wrong bars."""
    from quant_research.strategy.space import SPACES_V2

    catalog, ledger = env
    sel, conf = panels

    def run(name: str) -> SearchResult:
        return run_family_search(
            ledger,
            name,
            RandomSearcher(SPACES_V2[name], seed=8),
            selection=sel,
            confirmation=conf,
            catalog=catalog,
            config=CONFIG,
            s1=S1Config(horizon_bars=4),
            budget=SearchBudget(s1_count=8, top_k=2, promote_rate=0.0, s3_top=1),
            seed=3,
            fold="b5-v2",
        )

    res = run("momentum@1h")
    assert res.counts()["s1_trials"] >= 1 and res.family == "momentum@1h"
    before = _count(ledger, "S0")
    with pytest.raises(ValueError, match="timeframe"):
        run("momentum@4h")
    assert _count(ledger, "S0") == before  # refused before any trial


def test_a_family_whose_data_is_not_in_the_panel_is_refused_before_any_trial(
    env: tuple[Catalog, TrialLedger], panels: tuple[Panel, Panel]
) -> None:
    """B6 review 5: premium_basis on a panel without the premium index spent its whole S1 budget on -inf scores."""
    catalog, ledger = env
    sel, conf = panels
    with pytest.raises(ValueError, match="premium"):
        run_family_search(
            ledger,
            "premium_basis",
            RandomSearcher(SPACES["premium_basis"], seed=1),
            selection=sel,
            confirmation=conf,
            catalog=catalog,
            config=CONFIG,
            s1=S1Config(horizon_bars=4),
            budget=SearchBudget(s1_count=4, top_k=1, promote_rate=0.0, s3_top=0),
            seed=1,
        )
    assert _count(ledger, "S0") == 0


def test_the_v23_families_ask_for_their_datasets_and_are_data_missing_without_their_columns(
    panels: tuple[Panel, Panel],
) -> None:
    """v2.3 steps 7-8: a family's panel reads the datasets of its inputs; a panel without them is data_missing."""
    from quant_research.backtest.panel_store import DEFAULT_DATASETS
    from quant_research.search.pipeline import missing_inputs, panel_datasets
    from quant_research.strategy.ops import METRICS, SPOT

    assert panel_datasets("trend") == DEFAULT_DATASETS
    assert panel_datasets("oi_positioning") == (*DEFAULT_DATASETS, METRICS)
    assert panel_datasets("taker_flow") == (*DEFAULT_DATASETS, METRICS)
    assert panel_datasets("spot_basis") == (*DEFAULT_DATASETS, SPOT)
    selection, _confirmation = panels  # klines only: no metrics, no spot columns
    assert missing_inputs("oi_positioning", selection) == (METRICS,)
    assert missing_inputs("spot_basis", selection) == (SPOT,)
    assert missing_inputs("trend", selection) == ()


def test_an_input_present_for_too_few_member_rows_counts_as_missing(panels: tuple[Panel, Panel]) -> None:
    """Final review 1 (ADR-049 addendum 2): one symbol with funding passed `missing_inputs`, so a family was judged
    on a fraction of its universe. An input must cover at least 80 % of the member rows."""
    import polars as pl

    from quant_research.search.pipeline import MIN_INPUT_COVERAGE, missing_inputs

    selection, _confirmation = panels
    symbols = selection.frame["symbol"].unique().sort().to_list()
    assert len(symbols) == 4 and MIN_INPUT_COVERAGE == 0.8

    def with_funding(have: list[str]) -> Panel:
        frame = selection.frame.with_columns(
            pl.when(pl.col("symbol").is_in(have)).then(0.0001).otherwise(None).alias("funding_rate")
        )
        return Panel(frame, selection.funding, selection.files, selection.config, selection.evaluate_from_ns)

    assert missing_inputs("funding_carry", with_funding(symbols)) == ()
    assert missing_inputs("funding_carry", with_funding(symbols[:3])) == ("binance.um.bulk.funding",)  # 75 %
