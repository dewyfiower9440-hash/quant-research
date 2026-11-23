"""v2.2 B7 (D8): the campaign driver on small synthetic panels (real pipeline, temporary databases)."""

from __future__ import annotations

import copy

from quant_research.backtest.panel import Panel
from quant_research.catalog.store import Catalog
from quant_research.research.ledger import TrialLedger
from quant_research.search.bandit import BanditPrior, FamilyBandit
from quant_research.search.campaign import BatchSummary, CampaignState, Cell
from quant_research.search.campaign_runner import (
    CampaignConfig,
    CampaignOutcome,
    Rounds,
    missing_data,
    run_campaign,
)
from quant_research.search.optimizers import RandomSearcher
from quant_research.strategy.space import build_point

CELLS = [Cell("momentum", "1h", 30, "w2"), Cell("funding_carry", "1h", 30, "w2")]
CONFIG = CampaignConfig(seed=4, s1_count=8, top_k=2, promote_rate=0.0, s3_top=1, s3_random_rate=0.0)


def _trials(ledger: TrialLedger) -> int:
    return int(ledger.store.conn.execute("select count(*) from trial").fetchone()[0])


def _bandit(state: CampaignState) -> FamilyBandit:
    return FamilyBandit(tuple(c.key for c in state.all_cells()), BanditPrior(min_pulls=0.0))


def test_the_campaign_runs_batches_persists_each_and_reports_missing_data(
    env: tuple[Catalog, TrialLedger],
    panels: tuple[Panel, Panel],
) -> None:
    catalog, ledger = env
    state = CampaignState.new("t", CELLS, max_pulls=1)
    saved: list[int] = []
    out = run_campaign(
        state,
        _bandit(state),
        ledger=ledger,
        catalog=catalog,
        panels=lambda _cell: panels,
        config=CONFIG,
        gate=lambda: (True, ""),
        stop=lambda: False,
        persist=lambda s, _b: saved.append(len(s.history)),
    )
    assert out.status == "exhausted" and out.batches == len(state.history) >= 2
    assert saved == list(range(1, out.batches + 1))  # persisted after every batch
    causes = {str(h["cell"]): str(h["cause"]) for h in state.history}
    if missing_data(CELLS[1], panels[0]):  # the synthetic panels carry no funding: the carry cell asks for it
        assert causes[CELLS[1].key] == "data_missing" and "binance.um.bulk.funding" in state.data_requests
    assert all(
        c in ("candidate", "no_signal", "cost_dominated", "sparse", "unstable", "below_bar", "data_missing")
        for c in causes.values()
    )


def test_a_batch_interrupted_before_its_state_was_saved_repeats_without_new_trials(
    env: tuple[Catalog, TrialLedger],
    panels: tuple[Panel, Panel],
) -> None:
    """D8 resume: the state file is written after the batch; a crash in between replays the same batch, which
    must reuse every finished trial (same fold, same seeds) instead of evaluating again."""
    catalog, ledger = env
    start = CampaignState.new("r", [CELLS[0]], max_pulls=3)

    def once(state: CampaignState) -> None:
        run_campaign(
            state,
            _bandit(state),
            ledger=ledger,
            catalog=catalog,
            panels=lambda _cell: panels,
            config=_one_batch(),
            gate=lambda: (True, ""),
            stop=lambda: False,
            persist=lambda _s, _b: None,  # the crash: nothing is saved
        )

    once(copy.deepcopy(start))
    first = _evaluations(ledger)
    assert first > 0  # precondition: the batch evaluated something
    once(copy.deepcopy(start))  # the restart from the last saved state replays the batch
    assert _evaluations(ledger) == first  # S1, S2, S3 and the reward run were all reused
    # the replay's proposals are recorded as S0 duplicates only (accounting rows, not tests: N_total counts S1+)
    reasons = ledger.store.conn.execute("select reason from trial where stage = 'S0' and status = 'rejected'")
    assert all(str(r[0]).startswith("duplicate:") for r in reasons)


def _evaluations(ledger: TrialLedger) -> int:
    return int(ledger.store.conn.execute("select count(*) from trial where stage <> 'S0'").fetchone()[0])


def _one_batch() -> CampaignConfig:
    return CampaignConfig(seed=4, s1_count=8, top_k=2, promote_rate=0.0, s3_top=1, s3_random_rate=0.0, max_batches=1)


def test_the_stop_check_and_the_gate_end_the_campaign_before_any_batch(
    env: tuple[Catalog, TrialLedger],
    panels: tuple[Panel, Panel],
) -> None:
    catalog, ledger = env
    state = CampaignState.new("s", CELLS)

    def run(gate: tuple[bool, str], stop: bool) -> CampaignOutcome:
        return run_campaign(
            state,
            _bandit(state),
            ledger=ledger,
            catalog=catalog,
            panels=lambda _cell: panels,
            config=CONFIG,
            gate=lambda: gate,
            stop=lambda: stop,
            persist=lambda _s, _b: None,
        )

    stopped, gated = run((True, ""), True), run((False, "hot"), False)
    assert (stopped.status, gated.status, gated.reason) == ("stopped", "gate", "hot")
    assert state.history == [] and _trials(ledger) == 0


def test_a_window_splits_into_warm_up_selection_and_confirmation_without_overlap() -> None:
    """The first month of a window is warm-up only (B3); the remaining months split 70 / 30 into selection and
    confirmation; each split keeps every earlier row as feature warm-up (no orders, not in the metrics)."""
    import numpy as np
    import polars as pl

    from quant_research.backtest.panel import PanelConfig, settlements
    from quant_research.backtest.panel_store import Window
    from quant_research.search.campaign_runner import split_window

    day = 86_400 * 10**9
    jan = 1_767_225_600 * 10**9  # 2026-01-01
    window = Window(jan, jan + (31 + 28 + 31 + 30 + 31) * day)  # Jan..May 2026
    decisions = jan + np.arange(0, (31 + 28 + 31 + 30 + 31) * 24) * 3_600 * 10**9 + 60 * 10**9
    frame = pl.DataFrame(
        {"decision_ns": decisions, "symbol": ["A"] * len(decisions), "complete": [True] * len(decisions)}
    )
    panel = Panel(frame, settlements(None, None), [], PanelConfig(3_600 * 10**9), evaluate_from_ns=jan + 31 * day)
    sel, conf = split_window(panel, window)
    feb, may = jan + 31 * day, jan + (31 + 28 + 31 + 30) * day
    assert sel.evaluate_from_ns == feb and _ns(sel.frame["decision_ns"].min()) == int(decisions[0])
    assert conf.evaluate_from_ns is not None and conf.evaluate_from_ns >= feb
    sel_last = _ns(sel.frame["decision_ns"].max())
    assert sel_last < conf.evaluate_from_ns <= may  # selection ends before confirmation is evaluated
    traded = sel.frame.filter(pl.col("complete"))["decision_ns"]
    assert _ns(traded.min()) >= feb  # no orders in the warm-up month


def _ns(value: object) -> int:
    if not isinstance(value, int):
        raise TypeError(f"expected an integer, got {value!r}")
    return value


def test_a_batch_that_raises_is_recorded_as_error_and_the_campaign_goes_on(
    env: tuple[Catalog, TrialLedger],
    panels: tuple[Panel, Panel],
) -> None:
    """B7 review 6: an exception in one cell would recur on every restart (the next cell is deterministic)."""
    catalog, ledger = env
    state = CampaignState.new("e", CELLS, max_pulls=1)

    def flaky(cell: Cell) -> tuple[Panel, Panel]:
        if cell == CELLS[0]:
            raise ValueError("selection too short to split")
        return panels

    out = run_campaign(
        state,
        _bandit(state),
        ledger=ledger,
        catalog=catalog,
        panels=flaky,
        config=CONFIG,
        gate=lambda: (True, ""),
        stop=lambda: False,
        persist=lambda _s, _b: None,
    )
    causes = {str(h["cell"]): str(h["cause"]) for h in state.history}
    assert causes[CELLS[0].key] == "error" and CELLS[0].key in state.retired
    assert out.status == "exhausted" and CELLS[1].key in causes  # the other cell still ran


def test_the_s3_threshold_grows_with_the_s3_runs_judged_on_the_same_split() -> None:
    """B7 review 3: S3 SR > 0 passes half of all null cells; the bar is the best-of-N null level of the split."""
    from quant_research.search.campaign_runner import s3_null_threshold

    def judged(n: int, window: str) -> list[dict[str, object]]:
        return [{"cell": f"trend@4h|top100|{window}", "summary": {"s3_sr": 0.01}} for _ in range(n)]

    assert s3_null_threshold([], "w2", 150) == 0.0  # the first S3 on a split: no selection among S3 runs yet
    t5, t50 = s3_null_threshold(judged(4, "w2"), "w2", 150), s3_null_threshold(judged(49, "w2"), "w2", 150)
    assert 0.0 < t5 < t50
    assert s3_null_threshold(judged(49, "w1"), "w2", 150) == 0.0  # another window's split does not count
    assert s3_null_threshold(judged(49, "w2"), "w2", 600) < t50  # a longer split: a tighter null


def test_the_s3_threshold_counts_the_s3_runs_of_earlier_campaigns_on_the_same_split() -> None:
    """ADR-049 decision 2: a new run id must not restart the best-of-N bar from zero."""
    from quant_research.search.campaign_runner import judged_s3_counts, s3_null_threshold

    earlier: list[dict[str, object]] = [{"cell": "trend@4h|top100|w2", "summary": {"s3_sr": 0.01}} for _ in range(49)]
    earlier.append({"cell": "trend@4h|top100|w1", "summary": {"s3_sr": None}})  # not judged
    prior = judged_s3_counts([earlier])
    assert prior == {"w2": 49}
    fresh = s3_null_threshold([], "w2", 150, prior=prior.get("w2", 0))
    assert fresh == s3_null_threshold(earlier, "w2", 150) > 0.0


def test_the_pooled_bar_counts_every_s3_on_both_splits_and_tightens_with_more_days() -> None:
    """ADR-049 decision 3: N = all S3 judged on the two confirmation splits (cross runs included) + this batch's two."""
    from quant_research.search.campaign_runner import pooled_null_threshold

    def entry(window: str, cross: float | None) -> dict[str, object]:
        return {"cell": f"trend@4h|top100|{window}", "summary": {"s3_sr": 0.01, "cross_sr": cross}}

    few = pooled_null_threshold([entry("w2", None)], {}, 480)
    more = pooled_null_threshold([entry("w2", 0.02), entry("w1", None)], {}, 480)
    prior = pooled_null_threshold([entry("w2", None)], {"w1": 40, "w2": 40}, 480)
    assert 0.0 < few < more < prior
    assert pooled_null_threshold([entry("w2", None)], {}, 960) < few
    from quant_research.search.campaign_runner import judged_s3_counts

    assert judged_s3_counts([[entry("w2", 0.02)]]) == {"w1": 1, "w2": 1}  # a cross run is judged on the other split


def test_a_cross_window_s3_is_one_trial_reused_on_replay_and_pooled_by_concatenation(
    env: tuple[Catalog, TrialLedger],
    panels: tuple[Panel, Panel],
) -> None:
    import numpy as np

    from quant_research.backtest.run import RunConfig, run_backtest, unblob
    from quant_research.search.campaign_runner import cross_confirm
    from quant_research.search.optimizers import propose
    from quant_research.strategy.space import space_of

    catalog, ledger = env
    space = space_of("momentum@1h")
    traded = None
    for cand in propose(space, RandomSearcher(space, 3), 12):  # a point that trades in the fixture (flat pnl: no SR)
        run = run_backtest(
            build_point(space, cand.point), panels[1], catalog, ledger, RunConfig(engine="event", seed=1), stage="S3"
        )
        sr = run.metrics.get("sr_daily")
        if isinstance(sr, float) and np.isfinite(sr):
            traded = (cand.point, run)
            break
    assert traded is not None  # precondition: the fixture has a trading point among the first 12
    point, own = traded
    before = _trials(ledger)
    first = cross_confirm(ledger, catalog, space, point, own.trial_id, panels[1], fold="f", seed=1)
    assert _trials(ledger) == before + 1 and first is not None
    again = cross_confirm(ledger, catalog, space, point, own.trial_id, panels[1], fold="f", seed=1)
    assert _trials(ledger) == before + 1 and again == first  # replay: the finished trial is reused
    pnl = [
        unblob(
            bytes(
                ledger.store.conn.execute("select pnl_daily from backtest_run where trial_id = ?", (t,)).fetchone()[0]
            )
        )
        for t in (own.trial_id, first.trial_id)
    ]
    joined = np.concatenate(pnl)
    expected = float(np.mean(joined) / np.std(joined, ddof=1))
    assert first.pooled_sr == expected and first.days == len(joined)


def test_the_cross_step_runs_the_best_s3_spec_on_the_other_windows_split_and_skips_when_it_cannot(
    env: tuple[Catalog, TrialLedger],
    panels: tuple[Panel, Panel],
) -> None:
    """Review (step 4) 3: the cross run must use the other window's panel, match the own S3's spec, and leave the
    summary alone when the spec cannot be matched or the other window lacks the family's data."""
    from dataclasses import replace

    from quant_research.search.campaign_runner import summarize, with_cross
    from quant_research.search.pipeline import SearchBudget, run_family_search
    from quant_research.search.s1 import S1Config
    from quant_research.strategy.space import space_of

    catalog, ledger = env
    selection, confirmation = panels
    result = run_family_search(
        ledger,
        "momentum@1h",
        RandomSearcher(space_of("momentum@1h"), 5),
        selection=selection,
        confirmation=confirmation,
        catalog=catalog,
        config={"t": 1},
        s1=S1Config(horizon_bars=24),
        budget=SearchBudget(s1_count=12, top_k=3, promote_rate=0.0, s3_top=1),
        seed=5,
        fold="t:c:0",
    )
    summary = summarize(result, min_exposure=0.0)
    assert summary.s3_trial_id is not None  # precondition: an S3 ran
    cell = Cell("momentum", "1h", 30, "w2")
    state = CampaignState.new("t", [cell])
    asked: list[str] = []

    def other_window(c: Cell) -> tuple[Panel, Panel]:
        asked.append(c.window)
        return confirmation, selection  # the 'other window' confirmation is a different period than the own S3's

    out = with_cross(
        summary,
        result,
        state,
        cell,
        ledger=ledger,
        catalog=catalog,
        panels=other_window,
        seed=5,
        fold="t:c:0",
        prior_s3={},
    )
    assert asked == ["w1"] and out.cross_trial_id is not None and out.pooled_threshold > 0
    fold, window = ledger.store.conn.execute(
        "select t.fold, b.window_from_ns from trial t join backtest_run b using (trial_id) where t.trial_id = ?",
        (out.cross_trial_id,),
    ).fetchone()
    own = ledger.store.conn.execute(
        "select window_from_ns from backtest_run where trial_id = ?", (summary.s3_trial_id,)
    ).fetchone()[0]
    assert fold == "t:c:0:x" and window != own  # its own trial, on the other split
    unmatched = replace(summary, s3_trial_id=-1)
    assert (
        with_cross(
            unmatched,
            result,
            state,
            cell,
            ledger=ledger,
            catalog=catalog,
            panels=other_window,
            seed=5,
            fold="t:c:0",
            prior_s3={},
        )
        == unmatched
    )
    carry = Cell("funding_carry", "1h", 30, "w2")  # the fixture has no funding: the other window lacks the data
    assert (
        with_cross(
            summary,
            result,
            state,
            carry,
            ledger=ledger,
            catalog=catalog,
            panels=other_window,
            seed=5,
            fold="t:c:0",
            prior_s3={},
        )
        == summary
    )


def test_a_failed_cross_step_keeps_the_own_s3_as_error_and_transient_failures_propagate() -> None:
    """Re-review (step 4) 6: the guard around the cross step had no test."""
    import pytest

    from quant_research.search.campaign import classify
    from quant_research.search.campaign_runner import guarded_cross, judged_s3_counts

    cell = Cell("trend", "4h", 100, "w2")
    own = BatchSummary(10, 10, 3, 3, 0.1, 0.1, 0.0, 0.5, 0.02, (), s3_trial_id=7)

    def broken() -> BatchSummary:
        raise ValueError("empty universe")

    out = guarded_cross(own, broken, cell=cell)
    assert out.error == "cross: ValueError: empty universe" and out.s3_sr == 0.02
    assert classify(out, min_exposure=0.05) == "error"
    state = CampaignState.new("g", [cell])
    state.record(cell, "error", out, reward=0.0)
    assert judged_s3_counts([state.history]) == {"w2": 1}  # the own S3 still counts toward the null N

    def short() -> BatchSummary:
        raise MemoryError("cannot allocate")

    with pytest.raises(MemoryError):
        guarded_cross(own, short, cell=cell)


def test_earlier_campaigns_count_once_each_with_their_latest_history() -> None:
    """Re-review (step 3): a stale copy (campaign-c1-old.json sorts first) replaced c1's file and lowered the N."""
    from quant_research.search.campaign_runner import earlier_histories

    cell = Cell("trend", "4h", 100, "w2")
    old, new, mine = CampaignState.new("c1", [cell]), CampaignState.new("c1", [cell]), CampaignState.new("c2", [cell])
    judged = BatchSummary(1, 1, 1, 1, 0.1, 0.1, 0.0, 0.5, 0.01, ())
    for s, n in ((old, 1), (new, 3), (mine, 5)):
        for _ in range(n):
            s.record(cell, "unstable", judged, reward=0.0)
    histories = earlier_histories([old, new, mine], "c2")
    assert [len(h) for h in histories] == [3]  # c1 once, its longest history; c2 (this run) not at all


def test_an_exhausted_round_opens_the_next_group_then_waits_for_data(
    env: tuple[Catalog, TrialLedger],
    panels: tuple[Panel, Panel],
) -> None:
    """ADR-049: with rounds the campaign does not end 'exhausted'; it adds the next group, runs it, and stops only as
    'waiting_data' when the data its data_missing cells asked for never arrives."""
    catalog, ledger = env
    first, second = Cell("momentum", "1h", 30, "w2"), Cell("funding_carry", "1h", 30, "w2")
    state = CampaignState.new("g", [first], max_pulls=1)
    bandit = _bandit(state)
    started: list[int] = []
    out = run_campaign(
        state,
        bandit,
        ledger=ledger,
        catalog=catalog,
        panels=lambda _cell: panels,
        config=CONFIG,
        gate=lambda: (True, ""),
        stop=lambda: False,
        persist=lambda _s, _b: None,
        rounds=Rounds(groups=[[first], [second]], available=lambda: {}, on_start=lambda: started.append(1)),
    )
    ran = [str(h["cell"]) for h in state.history]
    assert second.key in ran and state.round >= 2 and started  # the second group ran after the first retired
    assert out.status == "waiting_data"
    if missing_data(second, panels[0]):
        assert "binance.um.bulk.funding" in out.reason and state.data_requests == ["binance.um.bulk.funding"]


def test_rounds_never_restart_a_round_with_an_empty_plan(
    env: tuple[Catalog, TrialLedger],
    panels: tuple[Panel, Panel],
) -> None:
    catalog, ledger = env
    cell = Cell("momentum", "1h", 30, "w2")
    state = CampaignState.new("e", [cell], max_pulls=1)
    state.record(cell, "no_signal", BatchSummary(0, 0, 0, 0, None, None, None, None, None, ()), reward=0.0)
    out = run_campaign(
        state,
        _bandit(state),
        ledger=ledger,
        catalog=catalog,
        panels=lambda _cell: panels,
        config=CONFIG,
        gate=lambda: (True, ""),
        stop=lambda: False,
        persist=lambda _s, _b: None,
        rounds=Rounds(groups=[[cell]], available=lambda: {}),
    )
    assert out.status == "waiting_data" and out.batches == 0 and state.round == 1


def test_an_unreadable_coverage_while_a_round_is_computed_pauses_the_campaign(
    env: tuple[Catalog, TrialLedger],
    panels: tuple[Panel, Panel],
) -> None:
    """Final re-review 1: the coverage read (tier state file mid-replace) raised out of run_campaign: the tick
    crashed without its report. It is transient like a batch's: the campaign pauses, the round is not started."""
    catalog, ledger = env
    cell = Cell("momentum", "1h", 30, "w2")
    state = CampaignState.new("u", [cell], max_pulls=1)
    state.record(cell, "no_signal", BatchSummary(0, 0, 0, 0, None, None, None, None, None, ()), reward=0.0)

    def locked() -> dict[str, dict[str, str]]:
        raise PermissionError("unreadable data-queue-state.json")

    out = run_campaign(
        state,
        _bandit(state),
        ledger=ledger,
        catalog=catalog,
        panels=lambda _cell: panels,
        config=CONFIG,
        gate=lambda: (True, ""),
        stop=lambda: False,
        persist=lambda _s, _b: None,
        rounds=Rounds(groups=[[cell]], available=locked),
    )
    assert out.status == "gate" and "PermissionError" in out.reason and state.round == 1


def test_the_time_budget_ends_the_campaign_as_budget_not_as_the_gate(
    env: tuple[Catalog, TrialLedger],
    panels: tuple[Panel, Panel],
) -> None:
    """B7 review 8: --max-hours went through the gate and exited 3 like a resource refusal."""
    catalog, ledger = env
    state = CampaignState.new("b", CELLS)
    out = run_campaign(
        state,
        _bandit(state),
        ledger=ledger,
        catalog=catalog,
        panels=lambda _cell: panels,
        config=CONFIG,
        gate=lambda: (True, ""),
        stop=lambda: False,
        persist=lambda _s, _b: None,
        out_of_time=lambda: True,
    )
    assert (out.status, out.reason) == ("budget", "time budget used")


def test_a_transient_failure_stops_the_campaign_without_retiring_the_cell(
    env: tuple[Catalog, TrialLedger],
    panels: tuple[Panel, Panel],
) -> None:
    """B8 review (part 2): MemoryError / OSError (disk, file in use) are not a property of the cell; recording them
    as `error` retired the cell for good. The campaign stops (resumable) and nothing is recorded."""
    catalog, ledger = env
    state = CampaignState.new("m", CELLS, max_pulls=1)

    def short_of_memory(_cell: Cell) -> tuple[Panel, Panel]:
        raise MemoryError("unable to allocate")

    out = run_campaign(
        state,
        _bandit(state),
        ledger=ledger,
        catalog=catalog,
        panels=short_of_memory,
        config=CONFIG,
        gate=lambda: (True, ""),
        stop=lambda: False,
        persist=lambda _s, _b: None,
    )
    assert out.status == "gate" and "MemoryError" in out.reason
    assert state.history == [] and state.retired == []


def test_a_cell_without_data_is_recorded_as_error_not_a_campaign_stop(
    env: tuple[Catalog, TrialLedger],
    panels: tuple[Panel, Panel],
) -> None:
    """Re-review 1: FileNotFoundError (no shard with member rows) is an OSError; treating it as transient stopped
    the campaign on the same cell at every restart. It is a property of the cell: recorded as error, cell retired."""
    catalog, ledger = env
    state = CampaignState.new("f", CELLS, max_pulls=1)

    def no_shard(cell: Cell) -> tuple[Panel, Panel]:
        if cell == CELLS[0]:
            raise FileNotFoundError("no shard with member rows for the window")
        return panels

    out = run_campaign(
        state,
        _bandit(state),
        ledger=ledger,
        catalog=catalog,
        panels=no_shard,
        config=CONFIG,
        gate=lambda: (True, ""),
        stop=lambda: False,
        persist=lambda _s, _b: None,
    )
    causes = {str(h["cell"]): str(h["cause"]) for h in state.history}
    assert causes[CELLS[0].key] == "error" and out.status == "exhausted"


def test_the_round_power_table_uses_the_same_n_as_the_pooled_bar() -> None:
    """v2.4 step 3: the reported power must describe the bar that judges, not a smaller N."""
    import pytest

    from quant_research.search.campaign_runner import pooled_null_threshold, pooled_power
    from quant_research.search.multiplicity import annualised

    def entry(window: str, cross: float | None) -> dict[str, object]:
        summary = BatchSummary(1, 1, 1, 1, 0.1, 0.1, 0.0, 0.5, 0.01, (), cross_sr=cross)
        state = CampaignState.new("p", [Cell("trend", "4h", 100, window)])
        state.record(Cell("trend", "4h", 100, window), "unstable", summary, reward=0.0)
        return state.history[0]

    history = [entry("w2", 0.02), entry("w1", None)]
    prior = {"w1": 36, "w2": 42}
    n, bar_ann, rows = pooled_power(history, prior, 480)
    assert n == 36 + 42 + 3 + 2  # earlier campaigns + this one's own and cross S3 + the next batch's two
    assert bar_ann == pytest.approx(annualised(pooled_null_threshold(history, prior, 480), 365))
    assert all(0.0 < p < 1.0 for _sr, p in rows)


def test_split_bounds_are_month_boundaries_computed_without_a_panel() -> None:
    from datetime import UTC, datetime

    import pytest

    from quant_research.backtest.panel_store import Window
    from quant_research.search.campaign_runner import split_bounds

    def ns(y: int, m: int) -> int:
        return int(datetime(y, m, 1, tzinfo=UTC).timestamp()) * 10**9

    sel, conf, end = split_bounds(Window(ns(2024, 12), ns(2026, 7)))
    assert (sel, conf, end) == (ns(2025, 1), ns(2026, 2), ns(2026, 7))  # 1 warm-up month, 13 + 5 months
    with pytest.raises(ValueError, match="at least 3 months"):
        split_bounds(Window(ns(2025, 1), ns(2025, 3)))
