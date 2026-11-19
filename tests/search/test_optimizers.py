"""Phase 4 step 6: Random baseline and TPE (Optuna 5.1.0, pinned) behind one ask/tell interface. Every ask
is one candidate for the recorded stages, so Optuna's internal evaluation count equals the trial count."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from quant_research.research.ledger import TrialLedger
from quant_research.research.store import ResearchStore
from quant_research.search.compare import compare, planted_landscape, run_search
from quant_research.search.cost_model import LinearCost
from quant_research.search.optimizers import (
    RandomSearcher,
    TpeSearcher,
    candidate_of,
    distributions,
    point_of,
    propose,
)
from quant_research.search.s0_stage import run_s0
from quant_research.strategy.space import SPACES, Point

CONFIG: dict[str, object] = {"data_version": "d1", "seed": 0}


@pytest.fixture
def ledger(tmp_path: Path) -> Generator[TrialLedger]:
    store = ResearchStore.open(tmp_path / "research.db")
    yield TrialLedger(store, env_id="ENV-T", code_sha="abc")
    store.close()


def test_distributions_cover_every_registered_axis_and_points_land_on_the_grid() -> None:
    space = SPACES["volatility"]
    d = distributions(space)
    assert set(d) == {
        "short",
        "long",
        "ratio",
        "struct:smooth",
        "struct:universe_n",
        "struct:sizing",
        "struct:time_stop",
    }
    s = TpeSearcher(space, seed=1)
    for _ in range(50):
        p = s.ask()
        assert candidate_of(space, p).point == p  # on the grid: build/violation never raises
        s.tell(p, None)
    point = point_of(
        space,
        {
            "short": 12,
            "long": 240,
            "ratio": 0.6500000001,
            "struct:smooth": 3,
            "struct:universe_n": 5,
            "struct:sizing": "equal",
            "struct:time_stop": 0,
        },
    )
    assert point.params_dict()["ratio"] == 0.65


@pytest.mark.parametrize("family", sorted(SPACES))
def test_random_and_tpe_are_deterministic_for_a_seed(family: str) -> None:
    space = SPACES[family]
    a = [RandomSearcher(space, 3).ask() for _ in range(5)]
    b = [RandomSearcher(space, 3).ask() for _ in range(5)]
    assert a == b
    ta, tb = TpeSearcher(space, 7, n_startup_trials=3), TpeSearcher(space, 7, n_startup_trials=3)
    pa: list[Point] = []
    pb: list[Point] = []
    for i in range(6):
        x, y = ta.ask(), tb.ask()
        pa.append(x)
        pb.append(y)
        ta.tell(x, float(i))
        tb.tell(y, float(i))
    assert pa == pb


def test_every_optuna_evaluation_is_exactly_one_s0_trial(ledger: TrialLedger) -> None:
    space = SPACES["trend"]
    s = TpeSearcher(space, seed=5, n_startup_trials=4)
    cands = propose(space, s, 25)
    out = run_s0(ledger, cands, CONFIG)
    assert out.generated == 25 == s.asked() == s.pending()
    for c in cands:  # the stage result is what the searcher learns from; refusals are told as pruned
        s.tell(c.point, None if c.spec is None else 1.0)
    assert s.pending() == 0 and s.internal_evaluations() == 25
    assert ledger.store.conn.execute("select count(*) from trial where stage = 'S0'").fetchone()[0] == 25
    with pytest.raises(KeyError):
        s.tell(cands[0].point, 1.0)  # told twice


def test_refused_proposals_keep_their_reasons() -> None:
    space = SPACES["trend"]
    cands = propose(space, RandomSearcher(space, 11), 300)
    refused = [c for c in cands if c.spec is None]
    assert refused and all(c.reasons for c in refused)
    assert all(r.startswith("constraint: fast") for c in refused for r in c.reasons)


def test_run_search_stops_at_the_cost_budget_and_tells_every_ask() -> None:
    space = SPACES["momentum"]
    land = planted_landscape(space, seed=2, noise=0.0)
    cost = LinearCost(0.5, 1e-6)
    s = TpeSearcher(space, 9, n_startup_trials=3)
    run = run_search(s, space, land.score, cost, rows=1_000, budget_s=10.0)
    assert run.cost_s <= 10.0 and run.evaluations >= 1
    assert s.pending() == 0 and s.internal_evaluations() == run.evaluations + 1  # the last ask was told pruned
    assert run.best is not None and 0.0 <= run.best <= 1.0
    assert land.score(candidate_of(space, land.target)) == pytest.approx(1.0)


def test_comparison_reports_recovery_for_both_searchers_under_one_budget() -> None:
    space = SPACES["mean_reversion"]
    land = planted_landscape(space, seed=4, width=0.35, noise=0.0)
    rep = compare(space, land, LinearCost(0.2, 0.0), rows=1, budget_s=12.0, seeds=[1, 2, 3], n_startup_trials=5)
    s = rep.summary()
    assert s["seeds"] == 3
    assert all(e == 60 for e in s["random"]["evaluations"]) and all(e == 60 for e in s["tpe"]["evaluations"])  # type: ignore[index]
    for key in ("random", "tpe"):
        rec = s[key]["recovery"]  # type: ignore[index]
        assert 0.0 <= rec <= 1.0
    # both are reported; no claim that one beats the other (that is a measurement, step 10)
    assert rep.optimum == 1.0 and rep.tolerance == 0.1
