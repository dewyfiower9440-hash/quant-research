"""v2.2 B7 (D8): the autonomous campaign's pure core: cells, failure causes, transition rules, state and resume."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from quant_research.search.bandit import BanditPrior, FamilyBandit
from quant_research.search.campaign import (
    BatchSummary,
    CampaignState,
    Cell,
    classify,
    negative_report,
    transitions,
)

MIN_EXPOSURE = 0.05


BASE = BatchSummary(
    s1_scored=30,
    s1_finite=20,
    s2_done=5,
    s2_exposed=3,
    best_sr_net=0.08,
    best_total_return=0.10,
    best_cost_fraction=0.02,
    best_gross_exposure=0.6,
    s3_sr=0.05,
    data_missing=(),
)


def _summary(**changes: object) -> BatchSummary:
    return replace(BASE, **changes)


def test_a_cell_key_round_trips_and_names_its_v2_space() -> None:
    cell = Cell("momentum", "4h", 100, "w1")
    assert cell.key == "momentum@4h|top100|w1" and cell.space == "momentum@4h"
    assert Cell.parse(cell.key) == cell
    with pytest.raises(ValueError, match="cell"):
        Cell.parse("momentum|top100")
    with pytest.raises(ValueError, match="timeframe"):
        Cell("momentum", "5m", 100, "w1")


@pytest.mark.parametrize(
    ("changes", "cause"),
    [
        ({}, "candidate"),
        ({"data_missing": ("binance.um.bulk.funding",)}, "data_missing"),
        ({"s1_finite": 0}, "no_signal"),
        ({"s2_done": 0, "best_sr_net": None, "s3_sr": None}, "no_signal"),
        ({"s2_exposed": 0, "best_sr_net": None}, "sparse"),
        ({"error": "ValueError: boom"}, "error"),
        ({"s3_threshold": 0.08}, "unstable"),  # S3 SR 0.05 below the best-of-N null level of its split
        ({"best_sr_net": -0.02, "best_total_return": -0.01, "best_cost_fraction": 0.05}, "cost_dominated"),
        ({"best_sr_net": -0.02, "best_total_return": -0.06, "best_cost_fraction": 0.05}, "no_signal"),
        ({"s3_sr": -0.01}, "unstable"),
        ({"s3_sr": None}, "unstable"),
    ],
)
def test_every_batch_is_given_exactly_one_cause(changes: dict[str, object], cause: str) -> None:
    assert classify(_summary(**changes), min_exposure=MIN_EXPOSURE) == cause


def test_transitions_turn_a_cause_into_the_next_experiment() -> None:
    cell = Cell("trend", "1h", 30, "w2")
    assert transitions(cell, "cost_dominated") == [Cell("trend", "4h", 30, "w2")]  # slower bars, fewer trades
    assert transitions(Cell("trend", "1d", 30, "w2"), "cost_dominated") == []  # nothing slower
    assert transitions(cell, "sparse") == [Cell("trend", "1h", 100, "w2")]  # a wider universe
    assert transitions(cell, "unstable") == [Cell("trend", "1h", 30, "w1")]  # the other research window
    assert transitions(cell, "no_signal") == [] and transitions(cell, "data_missing") == []
    assert transitions(cell, "candidate") == []


def test_the_state_runs_queued_transitions_first_then_the_bandit_and_retires_cells() -> None:
    cells = [Cell("trend", "1h", 30, "w2"), Cell("momentum", "1h", 30, "w2")]
    state = CampaignState.new("run", cells, max_pulls=2)
    bandit = FamilyBandit(tuple(c.key for c in state.all_cells()), BanditPrior(min_pulls=0.0))
    first = state.next_cell(bandit, seed=1, seq=0)
    assert first in cells
    state.record(first, "cost_dominated", _summary(best_sr_net=-0.1), reward=0.0)
    slower = Cell(first.family, "4h", 30, "w2")
    assert state.next_cell(bandit, seed=1, seq=1) == slower  # the transition target goes first
    state.record(slower, "no_signal", _summary(s1_finite=0), reward=0.0)
    state.record(slower, "no_signal", _summary(s1_finite=0), reward=0.0)
    assert slower.key in state.retired  # two pulls (max_pulls) -> retired
    state.record(first, "data_missing", _summary(data_missing=("binance.um.bulk.funding",)), reward=0.0)
    assert "binance.um.bulk.funding" in state.data_requests
    assert first.key in state.retired  # data missing: the same cell would fail the same way again


def test_a_candidate_cell_is_retired_after_its_submission_and_listed() -> None:
    cell = Cell("low_vol", "1d", 100, "w1")
    state = CampaignState.new("run", [cell], max_pulls=5)
    state.record(cell, "candidate", _summary(), reward=0.3)
    assert state.candidates == [cell.key] and cell.key in state.retired
    bandit = FamilyBandit(tuple(c.key for c in state.all_cells()), BanditPrior(min_pulls=0.0))
    assert state.next_cell(bandit, seed=1, seq=0) is None  # nothing left: the campaign ends, it does not loop


def test_the_state_survives_a_restart_and_makes_the_same_next_decision() -> None:
    cells = [Cell(f, "1h", 30, "w2") for f in ("trend", "momentum", "low_vol")]
    state = CampaignState.new("run", cells, max_pulls=3)
    bandit = FamilyBandit(tuple(c.key for c in state.all_cells()), BanditPrior(min_pulls=0.0))
    c0 = state.next_cell(bandit, seed=7, seq=0)
    assert c0 is not None
    state.record(c0, "sparse", _summary(s2_exposed=0, best_sr_net=None), reward=0.0)
    bandit.update(c0.key, 0.0)
    restored = CampaignState.from_json(json.loads(json.dumps(state.to_json())))
    rebandit = FamilyBandit.restore(
        tuple(c.key for c in restored.all_cells()),
        BanditPrior(min_pulls=0.0),
        json.loads(json.dumps(bandit.snapshot())),
    )
    assert restored.next_cell(rebandit, seed=7, seq=1) == state.next_cell(bandit, seed=7, seq=1)
    assert restored.history == state.history and restored.queue == state.queue


def test_an_exhausted_campaign_reports_a_negative_result_and_the_next_data_tiers() -> None:
    cells = [Cell("trend", "1h", 30, "w2"), Cell("funding_carry", "1h", 30, "w2")]
    state = CampaignState.new("run", cells, max_pulls=1)
    state.record(cells[0], "no_signal", _summary(s1_finite=0), reward=0.0)
    state.record(cells[1], "data_missing", _summary(data_missing=("binance.um.bulk.funding",)), reward=0.0)
    report = negative_report(state)
    assert report["candidates"] == [] and report["batches"] == 2
    assert report["causes"] == {"no_signal": 1, "data_missing": 1}
    tiers = report["next_data"]
    assert isinstance(tiers, list) and tiers[0] == "binance.um.bulk.funding"  # what the cells asked for first


def test_every_cell_a_record_can_add_is_a_bandit_arm_including_mixed_transition_paths() -> None:
    """B7 review 1: the closure was built per cause, so sparse -> unstable (4h top30 w2 -> 4h top100 w2 -> 4h top100
    w1) was not an arm and `bandit.update` raised KeyError mid-campaign."""
    state = CampaignState.new("run", [Cell("trend", "4h", 30, "w2")], max_pulls=9)
    arms = {c.key for c in state.all_cells()}
    for cell in list(state.all_cells()):
        for cause in ("cost_dominated", "sparse", "unstable"):
            assert {t.key for t in transitions(cell, cause)} <= arms
    assert "trend@4h|top100|w1" in arms and "trend@1d|top100|w1" in arms
    bandit = FamilyBandit(tuple(arms), BanditPrior(min_pulls=0.0))
    state.record(Cell("trend", "4h", 30, "w2"), "sparse", _summary(s2_exposed=0, best_sr_net=None), reward=0.0)
    wider = state.next_cell(bandit, seed=1, seq=1)
    assert wider == Cell("trend", "4h", 100, "w2") and wider is not None
    state.record(wider, "unstable", _summary(s3_sr=-0.1), reward=0.0)
    other = state.next_cell(bandit, seed=1, seq=2)
    assert other == Cell("trend", "4h", 100, "w1") and other is not None
    bandit.update(other.key, 0.0)  # no KeyError


def test_an_error_retires_the_cell_and_records_the_message() -> None:
    cell = Cell("trend", "1h", 30, "w2")
    state = CampaignState.new("run", [cell], max_pulls=3)
    state.record(cell, "error", _summary(error="ValueError: selection too short"), reward=0.0)
    assert cell.key in state.retired
    summary = state.history[-1]["summary"]
    assert isinstance(summary, dict) and summary["error"] == "ValueError: selection too short"
