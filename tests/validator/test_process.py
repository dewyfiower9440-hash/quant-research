"""v2.2 B8: the validator process on a small synthetic market (temporary databases; no real holdout)."""

from __future__ import annotations

import json
import math
from collections.abc import Generator
from dataclasses import replace
from pathlib import Path

import polars as pl
import pytest

from quant_research.backtest.panel import Panel
from quant_research.backtest.run import RunConfig, run_backtest
from quant_research.catalog.bootstrap import bootstrap
from quant_research.catalog.store import Catalog
from quant_research.research.ledger import TrialLedger
from quant_research.research.store import ResearchStore
from quant_research.strategy.spec import StrategySpec
from quant_research.strategy.templates import build
from quant_research.validator.process import (
    EPOCH,
    HoldoutProvider,
    HoldoutRole,
    final_bit,
    finite_detail,
    pending_requests,
    process_pending,
    submit_candidate,
)
from quant_research.validator.store import ValidatorError, ValidatorStore, open_readonly
from quant_research.validator.verdict import DecisionRule

LENIENT = DecisionRule(min_active_days=1, min_entries=1, min_mean_exposure=0.0, min_holdout_days=5)


class Env:
    def __init__(self, tmp: Path) -> None:
        self.catalog = Catalog.open(tmp / "c.db")
        bootstrap(self.catalog)
        self.research = ResearchStore.open(tmp / "r.db")
        self.ledger = TrialLedger(self.research, env_id="ENV-T", code_sha="abc")
        self.vstore = ValidatorStore.open(tmp / "v.db")
        self.vresearch = ResearchStore.open(tmp / "v-trials.db")
        self.vledger = TrialLedger(self.vresearch, env_id="ENV-T", code_sha="abc")
        self.research_path = tmp / "r.db"

    def run(self, holdout: HoldoutProvider, rule: DecisionRule) -> list[tuple[int, str, bool | None]]:
        ro = open_readonly(self.research_path)
        try:
            out = process_pending(
                ro, store=self.vstore, ledger=self.vledger, catalog=self.catalog, holdout=holdout, rule=rule
            )
        finally:
            ro.close()
        return [(o.request_id, o.status, o.passed) for o in out]

    def status(self, request_id: int) -> str:
        row = self.vstore.conn.execute("select status from submission where request_id = ?", (request_id,)).fetchone()
        return "unregistered" if row is None else str(row[0])

    def close(self) -> None:
        for db in (self.vresearch, self.research):
            db.close()
        self.vstore.close()
        self.catalog.close()


@pytest.fixture
def venv(tmp_path: Path) -> Generator[Env]:
    e = Env(tmp_path)
    yield e
    e.close()


def _spec(lookback: int = 24) -> StrategySpec:
    body = build("momentum", {"lookback": lookback, "quantile": 0.6}, n=2).model_dump(mode="json")
    body["sizing"] = {**body["sizing"], "method": "equal"}
    return StrategySpec.model_validate(body)


def _s3_trial(venv: Env, panel: Panel, lookback: int = 24) -> int:
    out = run_backtest(
        _spec(lookback), panel, venv.catalog, venv.ledger, RunConfig(engine="event"), stage="S3", fold=f"f{lookback}"
    )
    assert out.status == "done"
    return out.trial_id


def _holdout(panel: Panel) -> HoldoutProvider:
    def provider(_spec: StrategySpec, _role: HoldoutRole, _latency: int) -> Panel:
        return panel  # the synthetic market stands in for H_hist and H_fwd

    return provider


def test_only_a_finished_s3_trial_can_be_submitted_once_and_a_spec_only_once(
    venv: Env, panels: tuple[Panel, Panel]
) -> None:
    sel, _conf = panels
    trial = _s3_trial(venv, sel)
    assert submit_candidate(venv.research, trial, rule=LENIENT, now_ns=1) > 0
    with pytest.raises(ValidatorError, match="already submitted"):
        submit_candidate(venv.research, trial, rule=LENIENT, now_ns=2)
    again = run_backtest(_spec(), sel, venv.catalog, venv.ledger, RunConfig(engine="event"), stage="S3", fold="other")
    with pytest.raises(ValidatorError, match="spec"):  # B8 review 6: the same spec from another cell, another query
        submit_candidate(venv.research, again.trial_id, rule=LENIENT, now_ns=3)
    s2 = run_backtest(_spec(), sel, venv.catalog, venv.ledger, RunConfig(engine="vector"), stage="S2", fold="g")
    with pytest.raises(ValidatorError, match="S3"):
        submit_candidate(venv.research, s2.trial_id, rule=LENIENT, now_ns=4)


def test_a_campaign_tick_submits_each_new_candidate_once_and_reports_the_rest(
    venv: Env, panels: tuple[Panel, Panel]
) -> None:
    """v2.3 step 9: the campaign submits its candidates at the end of every tick; a later tick skips them."""
    from quant_research.search.campaign import BatchSummary, CampaignState, Cell, candidate_trials
    from quant_research.validator.process import submit_campaign_candidates

    sel, _conf = panels
    trial = _s3_trial(venv, sel)
    cell = Cell("momentum", "1h", 30, "w2")
    state = CampaignState.new("t", [cell], max_pulls=5)
    won = BatchSummary(10, 10, 3, 3, 0.1, 0.1, 0.0, 0.5, 0.05, (), s3_trial_id=trial)
    state.record(cell, "unstable", BatchSummary(10, 10, 3, 3, 0.1, 0.1, 0.0, 0.5, -0.01, (), s3_trial_id=999), reward=0)
    state.record(cell, "candidate", won, reward=0.0)
    assert candidate_trials(state.history) == [trial]  # only candidate batches, by their own S3 trial
    first = submit_campaign_candidates(venv.research, state.history, rule=LENIENT, now_ns=1)
    assert [s["trial_id"] for s in first.submitted] == [trial] and first.skipped == {}
    again = submit_campaign_candidates(venv.research, state.history, rule=LENIENT, now_ns=2)
    assert again.submitted == [] and "already submitted" in again.skipped[str(trial)]


def test_a_holdout_too_short_for_the_rule_is_refused_without_spending_a_query(
    venv: Env, panels: tuple[Panel, Panel]
) -> None:
    """ADR-042 under the real rule: a 30-day holdout fails min_holdout_days = 600 -> refused, k unchanged."""
    sel, conf = panels
    rule = DecisionRule()
    rid = submit_candidate(venv.research, _s3_trial(venv, sel), rule=rule, now_ns=1)
    assert venv.run(_holdout(conf), rule) == [(rid, "refused", None)]
    assert venv.vstore.budget(EPOCH) == (20, 0)  # (k, used): nothing spent
    assert venv.vstore.ack(rid) is None  # research learns nothing
    ro = open_readonly(venv.research_path)
    try:
        assert pending_requests(ro, venv.vstore) == []  # processed once
    finally:
        ro.close()
    purposes = {r.purpose for r in venv.vstore.access_log()}
    assert purposes == {"holdout_hist"} and venv.vstore.verify_chain()[0]  # H_fwd is not read for a refusal


def test_a_judged_request_leaves_one_bit_and_no_holdout_trial_in_research(
    venv: Env, panels: tuple[Panel, Panel]
) -> None:
    sel, conf = panels
    rid = submit_candidate(venv.research, _s3_trial(venv, sel), rule=LENIENT, now_ns=1)
    research_trials = int(venv.research.conn.execute("select count(*) from trial").fetchone()[0])
    ((_rid, status, passed),) = venv.run(_holdout(conf), LENIENT)
    assert status == "judged" and isinstance(passed, bool)
    assert venv.vstore.ack(rid) is passed and venv.vstore.budget(EPOCH) == (LENIENT.k, 1)
    assert int(venv.research.conn.execute("select count(*) from trial").fetchone()[0]) == research_trials
    holdout_trials = int(venv.vresearch.conn.execute("select count(*) from trial").fetchone()[0])
    assert holdout_trials >= 5  # base + 2 cost + 2 lag stresses (+ forward) live in the validator's own ledger
    log = venv.vstore.access_log()
    assert {r.purpose for r in log} >= {"holdout_hist"} and venv.vstore.verify_chain()[0]
    assert "binance.um.bulk.funding" in {r.dataset_id for r in log}  # B8 review 7: every dataset the panel reads


def test_the_access_log_names_every_dataset_the_holdout_panel_of_the_family_reads(
    venv: Env, panels: tuple[Panel, Panel]
) -> None:
    """v2.3 step 9 review 2: the holdout panel reads the family's datasets (panel_datasets), the log only the spec's
    ops: a spot_basis spec whose ops never read spot left the spot holdout read out of the chained log."""
    from quant_research.strategy.ops import SPOT

    sel, conf = panels
    body = _spec().model_dump(mode="json")
    body["family"] = "spot_basis"  # ops of the momentum template: no spot_close
    out = run_backtest(
        StrategySpec.model_validate(body),
        sel,
        venv.catalog,
        venv.ledger,
        RunConfig(engine="event"),
        stage="S3",
        fold="x",
    )
    submit_candidate(venv.research, out.trial_id, rule=LENIENT, now_ns=1)
    venv.run(_holdout(conf), LENIENT)
    assert SPOT in {r.dataset_id for r in venv.vstore.access_log()}


def test_a_bad_request_is_refused_and_does_not_block_the_queue(venv: Env, panels: tuple[Panel, Panel]) -> None:
    """B8 review 1a: a request under another rule raised before registration, so every later request was stuck."""
    sel, conf = panels
    bad = submit_candidate(venv.research, _s3_trial(venv, sel, 24), rule=DecisionRule(alpha=0.10), now_ns=1)
    good = submit_candidate(venv.research, _s3_trial(venv, sel, 48), rule=LENIENT, now_ns=2)
    out = dict((r, s) for r, s, _p in venv.run(_holdout(conf), LENIENT))
    assert out == {bad: "refused", good: "judged"}
    assert venv.status(bad) == "refused" and venv.vstore.budget(EPOCH) == (LENIENT.k, 1)
    assert venv.run(_holdout(conf), LENIENT) == []  # nothing left


def test_a_request_that_crashed_after_registration_is_retried_on_the_next_run(
    venv: Env, panels: tuple[Panel, Panel]
) -> None:
    """B8 review 1b: a crash between registration and verdict left the submission 'registered' forever."""
    sel, conf = panels
    rid = submit_candidate(venv.research, _s3_trial(venv, sel), rule=LENIENT, now_ns=1)

    def broken(spec: StrategySpec, role: HoldoutRole, latency: int) -> Panel:
        if latency > 0:  # the lag-stress panel, after registration and the base run
            raise FileNotFoundError("no lake files for the stressed latency yet")
        return conf

    assert venv.run(broken, LENIENT) == [(rid, "error", None)]
    assert venv.status(rid) == "registered" and venv.vstore.budget(EPOCH) == (LENIENT.k, 0)
    ((again, status, _p),) = venv.run(_holdout(conf), LENIENT)
    assert (again, status) == (rid, "judged") and venv.vstore.budget(EPOCH) == (LENIENT.k, 1)


def test_a_used_up_budget_refuses_before_any_holdout_read(venv: Env, panels: tuple[Panel, Panel]) -> None:
    """B8 review 4: with k used up, H_hist and H_fwd were read and evaluated before judge() refused."""
    sel, conf = panels
    rule = replace(LENIENT, k=1)
    first = submit_candidate(venv.research, _s3_trial(venv, sel, 24), rule=rule, now_ns=1)
    second = submit_candidate(venv.research, _s3_trial(venv, sel, 48), rule=rule, now_ns=2)
    out = venv.run(_holdout(conf), rule)
    assert [(r, s) for r, s, _p in out] == [(first, "judged"), (second, "refused")]
    second_sid = venv.vstore.conn.execute("select submission_id from submission where request_id = ?", (second,))
    sid = int(second_sid.fetchone()[0])
    assert all(r.submission_id != sid for r in venv.vstore.access_log())  # nothing of the holdout was read for it


def test_non_finite_numbers_in_a_verdict_detail_become_null() -> None:
    """B8 review 2: a flat forward pnl gave SR NaN; json_valid rejects NaN, judge() failed and the request stuck."""
    detail = finite_detail({"fwd_sr": math.nan, "stressed": {"cost x2": math.inf, "lag+1": 0.1}, "ok": 1.0, "s": "x"})
    assert detail == {"fwd_sr": None, "stressed": {"cost x2": None, "lag+1": 0.1}, "ok": 1.0, "s": "x"}


def test_a_flat_forward_holdout_is_judged_not_stuck(venv: Env, panels: tuple[Panel, Panel]) -> None:
    sel, conf = panels
    flat = replace(conf, frame=conf.frame.with_columns(pl.lit(False).alias("complete")))  # never trades: SR NaN

    def provider(_spec: StrategySpec, role: HoldoutRole, _latency: int) -> Panel:
        return flat if role == "fwd" else conf

    rid = submit_candidate(venv.research, _s3_trial(venv, sel), rule=LENIENT, now_ns=1)
    ((_r, status, _p),) = venv.run(provider, LENIENT)
    assert status == "judged" and venv.status(rid) == "judged"


@pytest.mark.parametrize(
    ("hist", "fwd", "llm", "bit"),
    [
        (True, 0.1, True, True),
        (True, -0.1, True, False),  # ADR-041: an LLM-origin candidate must be positive on H_fwd too
        (True, None, True, False),
        (True, -0.1, False, True),  # Random / TPE: H_fwd is reported, not required
        (False, 0.5, False, False),
    ],
)
def test_the_final_bit_follows_the_origin_rule(hist: bool, fwd: float | None, llm: bool, bit: bool) -> None:
    assert final_bit(hist, fwd, llm_origin=llm) is bit


def test_a_retried_request_checks_the_budget_before_reading_again(venv: Env, panels: tuple[Panel, Panel]) -> None:
    """Re-review 2: a request left registered by a crash skipped the budget check on retry; with k used up it read
    and evaluated the holdout again and failed in judge(), forever."""
    sel, conf = panels
    rule = replace(LENIENT, k=1)
    first = submit_candidate(venv.research, _s3_trial(venv, sel, 24), rule=rule, now_ns=1)
    second = submit_candidate(venv.research, _s3_trial(venv, sel, 48), rule=rule, now_ns=2)

    def crash_first(spec: StrategySpec, role: HoldoutRole, latency: int) -> Panel:
        if spec == _spec(24) and latency > 0:
            raise FileNotFoundError("stressed panel missing")
        return conf

    out = dict((r, s) for r, s, _p in venv.run(crash_first, rule))
    assert out == {first: "error", second: "judged"}  # the second used the only query
    reads = len(venv.vstore.access_log())
    assert venv.run(_holdout(conf), rule) == [(first, "refused", None)]
    assert len(venv.vstore.access_log()) == reads  # refused before any new holdout read
    assert venv.status(first) == "refused" and venv.run(_holdout(conf), rule) == []


def test_the_rule_hash_identifies_the_stress_definition() -> None:
    """Re-review 3: lagx2 changed meaning but the rule hash did not; the hash must name the stress definition."""
    from dataclasses import asdict

    assert DecisionRule().stress_version == 2 and "stress_version" in asdict(DecisionRule())  # part of the hash
    with pytest.raises(ValueError, match="stress_version"):  # a version the code does not implement
        DecisionRule(stress_version=1)


def test_the_validator_judges_a_portfolio_sized_spec_with_the_portfolio_weights(
    venv: Env, panels: tuple[Panel, Panel]
) -> None:
    """ADR-059: a candidate of space v4 reaches the validator as its stored spec; the holdout run must size it with
    the portfolio-level volatility target (the same weights a direct run of that spec gives), not the per-name one."""
    sel, conf = panels

    def sized(method: str) -> StrategySpec:
        body = _spec().model_dump(mode="json")
        body["sizing"] = {**body["sizing"], "method": method, "vol_window": 24}
        return StrategySpec.model_validate(body)

    spec = sized("portfolio_vol_target")
    s3 = run_backtest(spec, sel, venv.catalog, venv.ledger, RunConfig(engine="event"), stage="S3", fold="v4")
    assert s3.status == "done"
    submit_candidate(venv.research, s3.trial_id, rule=LENIENT, now_ns=1)
    ((_rid, status, _passed),) = venv.run(_holdout(conf), LENIENT)
    assert status == "judged"
    held = venv.vresearch.conn.execute(
        "select t.spec_hash, t.metrics_json from trial t where t.fold = 'holdout' order by t.trial_id limit 1"
    ).fetchone()
    assert held is not None  # precondition: the base holdout run is in the validator's own ledger
    exposure = json.loads(str(held[1]))["gross_exposure"]
    direct = {
        method: run_backtest(
            sized(method), conf, venv.catalog, venv.ledger, RunConfig(engine="event"), stage="reference", fold=method
        )
        for method in ("portfolio_vol_target", "vol_target")
    }
    assert str(held[0]) == s3_hash(venv, s3.trial_id)  # the spec the validator ran is the candidate's
    assert exposure > 0  # precondition: the spec trades on the holdout stand-in
    assert exposure == pytest.approx(direct["portfolio_vol_target"].metrics["gross_exposure"], rel=1e-12)
    assert exposure != pytest.approx(direct["vol_target"].metrics["gross_exposure"], rel=1e-3)


def s3_hash(venv: Env, trial_id: int) -> str:
    return str(venv.research.conn.execute("select spec_hash from trial where trial_id = ?", (trial_id,)).fetchone()[0])
