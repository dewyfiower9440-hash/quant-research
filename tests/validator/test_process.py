"""v2.2 B8: the validator process on a small synthetic market (temporary databases; no real holdout)."""

from __future__ import annotations

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
