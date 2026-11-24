"""v2.4 step 5 (ADR-052): a futures cell is not judged without funding data. c1 ran before the funding tier was in
the lake: 979 of its 1,068 S2 runs carried `funding_not_modelled` and still retired their cells."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from quant_research.backtest.panel import Panel
from quant_research.catalog.store import Catalog
from quant_research.research.ledger import TrialLedger
from quant_research.search.bandit import BanditPrior, FamilyBandit
from quant_research.search.campaign import BatchSummary, CampaignState, Cell, summary_from_json
from quant_research.search.campaign_runner import CampaignConfig, missing_data, run_campaign
from quant_research.search.pipeline import missing_inputs

FUNDING = "binance.um.bulk.funding"
CELL = Cell("momentum", "1h", 30, "w2")  # a price family: it does not read the funding column


def test_a_family_that_does_not_read_funding_still_needs_it_when_required(
    panels: tuple[Panel, Panel], funded_panels: tuple[Panel, Panel]
) -> None:
    bare, with_funding = panels[0], funded_panels[0]
    assert missing_inputs("momentum", bare) == ()  # the family reads prices only
    assert missing_inputs("momentum", bare, also=("funding",)) == (FUNDING,)
    assert missing_inputs("momentum", with_funding, also=("funding",)) == ()
    assert missing_inputs("funding_carry", bare, also=("funding",)) == (FUNDING,)  # listed once
    assert missing_data(CELL, bare) == () and missing_data(CELL, bare, require_funding=True) == (FUNDING,)
    assert missing_data(CELL, with_funding, require_funding=True) == ()
    # the column alone is not enough: without settlements the engines would pay no funding
    no_events = replace(with_funding, funding=with_funding.funding.clear())
    assert missing_data(CELL, no_events, require_funding=True) == (FUNDING,)
    with pytest.raises(ValueError, match="unknown data need"):
        missing_inputs("momentum", bare, also=("fundng",))


def _run(env: tuple[Catalog, TrialLedger], provider: tuple[Panel, Panel], version: int) -> tuple[CampaignState, int]:
    catalog, ledger = env
    state = CampaignState.new(f"f{version}", [CELL], max_pulls=1)
    run_campaign(
        state,
        FamilyBandit((CELL.key,), BanditPrior(min_pulls=0.0)),
        ledger=ledger,
        catalog=catalog,
        panels=lambda _cell: provider,
        config=CampaignConfig(
            seed=4, s1_count=8, top_k=2, promote_rate=0.0, s3_top=1, s3_random_rate=0.0, space_version=version
        ),
        gate=lambda: (True, ""),
        stop=lambda: False,
        persist=lambda _s, _b: None,
    )
    return state, int(ledger.store.conn.execute("select count(*) from trial").fetchone()[0])


def test_a_v3_campaign_does_not_judge_a_cell_without_funding(
    env: tuple[Catalog, TrialLedger], panels: tuple[Panel, Panel]
) -> None:
    state, trials = _run(env, panels, 3)
    assert [h["cause"] for h in state.history] == ["data_missing"] and trials == 0
    assert state.data_requests == [FUNDING] and CELL.key in state.retired  # reopened when the tier arrives


def test_a_v2_campaign_keeps_its_old_rule(env: tuple[Catalog, TrialLedger], panels: tuple[Panel, Panel]) -> None:
    state, trials = _run(env, panels, 2)  # c1's rule: only what the family reads is required
    assert state.history[0]["cause"] != "data_missing" and trials > 0


def test_a_v3_campaign_runs_on_funded_panels_and_reports_the_funding_share(
    env: tuple[Catalog, TrialLedger], funded_panels: tuple[Panel, Panel]
) -> None:
    state, trials = _run(env, funded_panels, 3)
    assert state.history[0]["cause"] != "data_missing" and trials > 0
    summary = summary_from_json(json.loads(json.dumps(state.history[0]["summary"])))
    if summary.best_sr_net is not None:  # an exposed S2 run exists: its funding share of capital is reported
        assert summary.best_funding_fraction is not None
    flags = [
        str(row[0]) for row in env[1].store.conn.execute("select metrics_json from trial where stage = 'S2'").fetchall()
    ]
    assert flags and not any("funding_not_modelled" in f for f in flags)


def test_the_funding_share_round_trips_and_defaults_for_old_summaries() -> None:
    b = BatchSummary(1, 1, 1, 1, 0.1, 0.05, 0.01, 0.5, 0.02, (), best_funding_fraction=-0.003)
    state = CampaignState.new("j", [CELL])
    state.record(CELL, "unstable", b, reward=0.0)
    stored: dict[str, object] = json.loads(json.dumps(state.history[0]["summary"]))
    assert summary_from_json(stored) == b
    del stored["best_funding_fraction"]
    assert summary_from_json(stored).best_funding_fraction is None
