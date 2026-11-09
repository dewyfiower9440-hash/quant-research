"""Phase 4 step 2: the S0 stage of the search pipeline. Every generated candidate is exactly one S0 trial:
accepted (done), rejected by a constraint or an S0 rule, or rejected as a canonical duplicate."""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path

import pytest

from quant_research.research.ledger import TrialLedger, TrialOrigin
from quant_research.research.store import ResearchStore
from quant_research.search.s0_stage import run_s0
from quant_research.strategy.space import SPACES, Candidate, Point, build_point, generate

CONFIG = {"data_version": "d1", "seed": 0}
TREND_POINT = Point.of(
    "trend",
    {"fast": 24, "slow": 96},
    {"side": "long_short", "smooth": 0, "universe_n": 5, "sizing": "equal", "time_stop": 0},
)


@pytest.fixture
def ledger(tmp_path: Path) -> Generator[TrialLedger]:
    store = ResearchStore.open(tmp_path / "research.db")
    yield TrialLedger(store, env_id="ENV-T", code_sha="abc")
    store.close()


def _s0_rows(ledger: TrialLedger) -> list[tuple[int, str, str | None, dict[str, object]]]:
    rows = ledger.store.conn.execute(
        "select trial_id, status, reason, metrics_json from trial where stage = 'S0' order by trial_id"
    )
    return [(int(r[0]), str(r[1]), r[2], json.loads(r[3]) if r[3] else {}) for r in rows]


def _accepted(point: Point) -> Candidate:
    return Candidate(point, build_point(SPACES[point.family], point), ())


def test_every_generated_candidate_is_exactly_one_s0_trial(ledger: TrialLedger) -> None:
    cands = generate(SPACES["trend"], seed=5, count=200)
    out = run_s0(ledger, cands, CONFIG)
    rows = _s0_rows(ledger)
    assert len(rows) == out.generated == 200
    done = [r for r in rows if r[1] == "done"]
    rejected = [r for r in rows if r[1] == "rejected"]
    assert len(done) == len(out.accepted) == out.unique_new
    assert len(rejected) == out.rejected + out.duplicates
    assert out.rejected == sum(1 for c in cands if c.spec is None)
    assert {a.trial_id for a in out.accepted} == {r[0] for r in done}


def test_a_duplicate_in_the_same_batch_is_rejected_and_points_at_the_first_trial(ledger: TrialLedger) -> None:
    out = run_s0(ledger, [_accepted(TREND_POINT), _accepted(TREND_POINT)], CONFIG)
    rows = _s0_rows(ledger)
    assert [r[1] for r in rows] == ["done", "rejected"]
    assert rows[1][2] == f"duplicate: canonical spec already passed S0 in trial {rows[0][0]}"
    assert (out.generated, out.unique_new, out.duplicates, out.rejected) == (2, 1, 1, 0)


def test_a_candidate_seen_in_an_earlier_batch_is_a_duplicate_trial_not_a_free_skip(ledger: TrialLedger) -> None:
    first = run_s0(ledger, [_accepted(TREND_POINT)], CONFIG)
    second = run_s0(ledger, [_accepted(TREND_POINT)], CONFIG)
    assert (second.generated, second.unique_new, second.duplicates) == (1, 0, 1)
    assert len(_s0_rows(ledger)) == 2
    assert (
        first.accepted[0].experiment_key
        == ledger.store.conn.execute(
            "select experiment_key from trial where trial_id = ?", (_s0_rows(ledger)[1][0],)
        ).fetchone()[0]
    )
    # another configuration (data version) is another experiment, not a duplicate
    third = run_s0(ledger, [_accepted(TREND_POINT)], {**CONFIG, "data_version": "d2"})
    assert third.unique_new == 1


def test_rule_and_constraint_rejections_keep_every_reason(ledger: TrialLedger) -> None:
    bad_point = Point.of("trend", {"fast": 96, "slow": 48}, TREND_POINT.struct_dict())
    cands = [
        Candidate(bad_point, None, ("constraint: fast 96 >= slow 48",)),
        Candidate(TREND_POINT, None, ("execution: maker is paper only", "exit: holding below 1 hour")),
    ]
    out = run_s0(ledger, cands, CONFIG)
    rows = _s0_rows(ledger)
    assert [r[1] for r in rows] == ["rejected", "rejected"]
    assert rows[0][2] == "constraint: fast 96 >= slow 48"
    assert rows[1][2] == "execution: maker is paper only; exit: holding below 1 hour"
    assert out.rejected == 2 and out.accepted == []


def test_each_s0_trial_records_its_point_and_origin(ledger: TrialLedger) -> None:
    run_s0(ledger, [_accepted(TREND_POINT)], CONFIG, origin=TrialOrigin(cycle_id=None, model_id="search"))
    _, _, _, metrics = _s0_rows(ledger)[0]
    assert metrics["point"] == {
        "family": "trend",
        "params": {"fast": 24.0, "slow": 96.0},
        "struct": TREND_POINT.struct_dict(),
    }
    model = ledger.store.conn.execute("select model_id from trial").fetchone()[0]
    assert model == "search"


def test_an_empty_batch_records_nothing(ledger: TrialLedger) -> None:
    out = run_s0(ledger, [], CONFIG)
    assert (out.generated, out.unique_new, out.duplicates, out.rejected) == (0, 0, 0, 0)
    assert _s0_rows(ledger) == []
