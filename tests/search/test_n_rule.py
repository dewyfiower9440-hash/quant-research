"""v2.5 step 8a (ADR-058 re-examination): the cell-level N of the pooled bar. A repeated judgment of the same cell
does not raise N, only the best judgment of a cell group in a campaign may be a candidate, and the raw-N bar is
kept beside the new one."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import cast

import pytest

from quant_research.backtest.panel import Panel
from quant_research.catalog.store import Catalog
from quant_research.research.ledger import TrialLedger
from quant_research.search.bandit import BanditPrior, FamilyBandit
from quant_research.search.campaign import (
    BatchSummary,
    CampaignState,
    Cell,
    below_bar_reason,
    classify,
    summary_from_json,
)
from quant_research.search.campaign_runner import (
    CampaignConfig,
    NRule,
    PanelProvider,
    earlier_pairs,
    group_record,
    judged_pairs,
    pair_key,
    pooled_n,
    pooled_n_cell,
    pooled_null_threshold,
    run_campaign,
    saved_space_version,
    summarize,
    with_cross,
)
from quant_research.search.multiplicity import expected_max_sr
from quant_research.search.optimizers import RandomSearcher
from quant_research.search.pipeline import SearchBudget, run_family_search
from quant_research.search.s1 import S1Config
from quant_research.strategy.space import space_of


def _entry(cell: str, s3: float | None, cross: float | None, pooled: float | None = None) -> dict[str, object]:
    return {"cell": cell, "summary": {"s3_sr": s3, "cross_sr": cross, "pooled_sr": pooled}}


W2 = "mean_reversion@1d|top100|w2"
W1 = "mean_reversion@1d|top100|w1"
OTHER = "momentum@1d|top100|w2"


def test_a_pair_is_counted_once_however_often_its_cell_is_judged() -> None:
    cell = Cell.parse(W2)
    assert pair_key(4, cell, "w2") == "v4|mean_reversion@1d|top100|w2"
    one = [_entry(W2, 0.01, 0.02)]
    assert judged_pairs(one, 4) == {"v4|mean_reversion@1d|top100|w2", "v4|mean_reversion@1d|top100|w1"}
    assert judged_pairs([_entry(W2, 0.01, None)], 4) == {"v4|mean_reversion@1d|top100|w2"}  # no cross run
    assert judged_pairs([_entry(W2, None, None), {"cell": W2}], 4) == frozenset()  # nothing was judged
    again = [*one, _entry(W2, 0.03, 0.01), _entry(W1, 0.02, 0.02)]  # the same two pairs, three times
    assert judged_pairs(again, 4) == judged_pairs(one, 4)
    assert pooled_n_cell(again, 4, frozenset(), cell) == 2  # this batch's two pairs are already there
    assert pooled_n(again, {}) == 8  # the raw rule: six judgments + this batch's two
    assert pooled_n_cell(again, 4, frozenset(), Cell.parse(OTHER)) == 4  # a new cell adds its two
    assert pooled_n_cell([], 4, frozenset(), cell) == 2
    # another space version, a regime-gated or a model family is another cell
    assert judged_pairs(one, 3).isdisjoint(judged_pairs(one, 4))
    gated = [_entry("mean_reversion~regime@1d|top100|w2", 0.01, 0.02)]
    assert judged_pairs(gated, 4).isdisjoint(judged_pairs(one, 4))


def test_earlier_campaigns_count_under_the_space_version_they_were_saved_with() -> None:
    cell = Cell.parse(W2)
    judged = BatchSummary(1, 1, 1, 1, 0.1, 0.1, 0.0, 0.5, 0.01, (), cross_sr=0.02, pooled_sr=0.03)
    c1, c2, stale, mine = (CampaignState.new(run, [cell]) for run in ("c1", "c2", "c2", "c3"))
    c2.settings = {"config": CampaignConfig(space_version=3).identity()}
    stale.settings = dict(c2.settings)
    mine.settings = {"config": CampaignConfig(space_version=4, n_rule="cell").identity()}
    for state, n in ((c1, 2), (c2, 3), (stale, 1), (mine, 4)):
        for _ in range(n):
            state.record(cell, "below_bar", judged, reward=0.0)
    assert (saved_space_version(c1), saved_space_version(c2), saved_space_version(mine)) == (2, 3, 4)
    prior = earlier_pairs([stale, c1, c2, mine], "c3")
    assert prior == {
        "v2|mean_reversion@1d|top100|w2",
        "v2|mean_reversion@1d|top100|w1",
        "v3|mean_reversion@1d|top100|w2",
        "v3|mean_reversion@1d|top100|w1",
    }  # each run once (c2's stale copy adds nothing), this run not at all
    assert pooled_n_cell(mine.history, 4, prior, cell) == 6
    assert pooled_n_cell(mine.history, 4, prior, Cell.parse(OTHER)) == 8
    c1.settings = {"config": {"space_version": "3"}}
    with pytest.raises(ValueError, match="space_version must be an integer"):
        earlier_pairs([c1], "c3")


def test_the_group_record_reads_both_windows_and_only_recorded_pooled_srs() -> None:
    history = [
        _entry(W2, 0.01, 0.02, 0.05),
        _entry(W1, 0.02, None, None),  # judged on its own split, no cross run: counted, not compared
        _entry(W1, 0.02, 0.01, 0.09),
        _entry(OTHER, 0.02, 0.01, 0.50),  # another group
        _entry(W2, None, None, None),  # nothing judged
    ]
    assert group_record(history, Cell.parse(W2)) == (3, 0.09)
    assert group_record(history, Cell.parse(OTHER)) == (1, 0.50)
    assert group_record(history, Cell("low_vol", "1d", 100, "w2")) == (0, None)
    assert group_record(history, Cell("mean_reversion", "1d", 30, "w2")) == (0, None)  # another top M


def test_a_batch_that_is_not_the_best_of_its_cell_group_is_below_the_bar() -> None:
    passing = BatchSummary(
        1, 1, 1, 1, 0.1, 0.1, 0.0, 0.5, 0.02, (), cross_sr=0.02, pooled_sr=0.2, pooled_threshold=0.1, cell_judged=2
    )
    assert classify(passing, min_exposure=0.05) == "candidate"
    beaten = replace(passing, not_cell_best=True)
    assert classify(beaten, min_exposure=0.05) == "below_bar" and below_bar_reason(beaten) == "not_cell_best"
    under = replace(beaten, pooled_sr=0.05)
    assert below_bar_reason(under) == "pooled_below"  # the bar is read first


def test_the_cell_rule_fields_round_trip_and_older_state_files_still_load() -> None:
    cell = Cell.parse(W2)
    plain = BatchSummary(1, 1, 1, 1, 0.1, 0.1, 0.0, 0.5, 0.02, (), cross_sr=0.02, pooled_sr=0.2, pooled_threshold=0.1)
    ruled = replace(plain, pooled_threshold_raw=0.13, cell_judged=3, not_cell_best=True)
    state = CampaignState.new("c3", [cell])
    state.record(cell, "candidate", plain, reward=0.0)
    state.record(cell, "below_bar", ruled, reward=0.0)
    raw = state.to_json()
    stored = [cast(Mapping[str, object], h["summary"]) for h in CampaignState.from_json(raw).history]
    assert "cell_judged" not in stored[0] and "pooled_threshold_raw" not in stored[0]  # the raw rule's file shape
    assert summary_from_json(stored[0]) == plain and summary_from_json(stored[1]) == ruled
    assert (plain.pooled_threshold_raw, plain.cell_judged, plain.not_cell_best) == (None, None, False)


def test_the_n_rule_is_part_of_a_campaigns_identity_only_when_it_is_not_the_raw_one() -> None:
    assert "n_rule" not in CampaignConfig().identity()  # c1 and c2 were saved without it and must still resume
    assert "n_rule" not in CampaignConfig(space_version=3).identity()
    assert CampaignConfig(space_version=4, n_rule="cell").identity()["n_rule"] == "cell"
    from_a_file = json.loads('{"n_rule": "effective"}')  # what a hand-edited state or task file could carry
    with pytest.raises(ValueError, match="n_rule"):
        CampaignConfig(n_rule=from_a_file["n_rule"])


def test_the_cross_step_under_the_cell_rule_sets_the_cell_bar_and_keeps_the_raw_one(
    env: tuple[Catalog, TrialLedger],
    panels: tuple[Panel, Panel],
) -> None:
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
    prior_s3 = {"w1": 40, "w2": 40}
    prior_pairs = frozenset({"v3|trend@4h|top100|w2", "v3|trend@4h|top100|w1", "v3|momentum@4h|top100|w2"})

    def cross(history_state: CampaignState, rule: NRule) -> BatchSummary:
        return with_cross(
            summary,
            result,
            history_state,
            cell,
            ledger=ledger,
            catalog=catalog,
            panels=lambda _c: (confirmation, selection),
            seed=5,
            fold="t:c:0",
            prior_s3=prior_s3,
            n_rule=rule,
            space_version=2,
            prior_pairs=prior_pairs,
        )

    raw = cross(state, "raw")
    assert raw.pooled_sr is not None and raw.cross_trial_id is not None  # precondition: the cross run was judged
    assert (raw.pooled_threshold_raw, raw.cell_judged, raw.not_cell_best) == (None, None, False)
    first = cross(state, "cell")
    days = round(1.0 / (raw.pooled_threshold / expected_max_sr(82, 1.0)) ** 2)  # the raw bar: N = 80 + 2
    assert raw.pooled_threshold == pytest.approx(pooled_null_threshold([], prior_s3, days))
    assert first.pooled_threshold_raw == raw.pooled_threshold
    assert first.pooled_threshold == pytest.approx(expected_max_sr(5, 1.0 / days))  # three earlier pairs + its two
    assert first.pooled_threshold < raw.pooled_threshold
    assert (first.cell_judged, first.not_cell_best) == (1, False)
    assert (first.cross_sr, first.pooled_sr, first.cross_trial_id) == (raw.cross_sr, raw.pooled_sr, raw.cross_trial_id)
    # a second judgment of the group: N stays (the pairs are the same), and a tie with the earlier one is not better
    # (the runner reaches the cross step only with a positive own S3; this fixture's S3 has no SR of its own)
    judged = replace(first, s3_sr=0.01)
    state.record(cell, "below_bar", judged, reward=0.0)
    second = cross(state, "cell")
    assert second.pooled_threshold == first.pooled_threshold
    assert (second.cell_judged, second.not_cell_best) == (2, True)
    pooled = first.pooled_sr
    assert pooled is not None
    worse = CampaignState.new("t", [cell])
    worse.record(cell, "below_bar", replace(judged, pooled_sr=pooled - 1.0), reward=0.0)
    assert cross(worse, "cell").not_cell_best is False  # better than every earlier judgment of the group


def _campaign(
    env: tuple[Catalog, TrialLedger], cells: list[Cell], config: CampaignConfig, provider: PanelProvider, run_id: str
) -> list[BatchSummary]:
    catalog, ledger = env
    state = CampaignState.new(run_id, cells, max_pulls=1)
    run_campaign(
        state,
        FamilyBandit(tuple(c.key for c in cells), BanditPrior(min_pulls=0.0)),
        ledger=ledger,
        catalog=catalog,
        panels=provider,
        config=config,
        gate=lambda: (True, ""),
        stop=lambda: False,
        persist=lambda _s, _b: None,
    )
    return [summary_from_json(cast(Mapping[str, object], h["summary"])) for h in state.history]


def test_every_judged_batch_under_the_cell_rule_says_how_often_its_group_was_judged(
    env: tuple[Catalog, TrialLedger],
    panels: tuple[Panel, Panel],
) -> None:
    """Review of step 8a: a batch whose own S3 was judged but that never got through the cross step had no count."""
    w2, w1 = Cell("momentum", "1h", 30, "w2"), Cell("momentum", "1h", 30, "w1")
    config = CampaignConfig(
        seed=4, s1_count=12, top_k=3, promote_rate=0.0, s3_top=1, s3_random_rate=0.0, min_exposure=0.0, n_rule="cell"
    )
    both = _campaign(env, [w2, w1], config, lambda _cell: panels, "t")
    assert len(both) == 2 and all(s.s3_sr is not None for s in both)  # precondition: both batches were judged
    assert sorted(s.cell_judged or 0 for s in both) == [1, 2]  # the two windows' cells are one group

    def no_other_window(cell: Cell) -> tuple[Panel, Panel]:
        if cell.window == "w1":
            raise RuntimeError("the other window cannot be read")
        return panels

    (alone,) = _campaign(env, [w2], config, no_other_window, "u")
    assert alone.s3_sr is not None and alone.error.startswith("cross:")  # precondition: the cross step failed
    assert alone.cross_sr is None and alone.pooled_threshold_raw is None
    assert alone.cell_judged == 1  # still counted: its own S3 was judged
    (raw,) = _campaign(env, [w2], replace(config, n_rule="raw"), lambda _cell: panels, "r")
    assert raw.s3_sr is not None and raw.cell_judged is None  # the raw rule records nothing new
