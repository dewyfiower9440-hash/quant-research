"""Phase 5 step 11: lifecycle transition table (who may sign what), append-only recorder, pre-registered
degradation rules (CUSUM, slippage ratio, gap ratio), and the prior-knowledge helpers."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from quant_research.research.ledger import TrialLedger
from quant_research.research.store import ResearchStore
from quant_research.validator.lifecycle import (
    STATES,
    TRANSITIONS,
    DegradationRule,
    IllegalLifecycleTransition,
    LifecycleLedger,
    Transition,
    check_transition,
    cusum_lower,
    gap_degraded,
    slippage_degraded,
)
from quant_research.validator.prior_knowledge import (
    anonymization_difference,
    cutoffs_of,
    final_judgement_from,
    parse_cutoff,
)
from quant_research.validator.store import ValidatorStore


def test_every_state_is_reachable_and_the_return_path_goes_through_the_validator() -> None:
    reachable = {dst for _src, dst in TRANSITIONS}
    assert set(STATES) <= reachable
    assert TRANSITIONS[("Degraded", "Validated")] == frozenset({"validator"})
    assert TRANSITIONS[("Suspended", "Validated")] == frozenset({"validator"})
    assert ("Degraded", "Active") not in TRANSITIONS and ("Suspended", "Active") not in TRANSITIONS
    check_transition("Candidate", "Validated", "validator")
    check_transition("Paper", "Approved", "user:owner")
    with pytest.raises(IllegalLifecycleTransition):
        check_transition("Candidate", "Active", "user")  # not a transition
    with pytest.raises(IllegalLifecycleTransition):
        check_transition("Candidate", "Validated", "research")  # wrong signer
    with pytest.raises(IllegalLifecycleTransition):
        check_transition("Approved", "Active", "risk")  # only the user turns a strategy live


def test_recorder_enforces_current_state_signer_and_evidence(tmp_path: Path) -> None:
    store = ValidatorStore.open(tmp_path / "v.db")
    try:
        led = LifecycleLedger(store.conn, now_ns=lambda: 7)
        assert led.state("h1") is None
        led.move(Transition("h1", None, "Candidate", "search produced it", "trial:1", "research"))
        led.move(Transition("h1", "Candidate", "Validated", "holdout bit 1", "verdict:1", "validator"))
        with pytest.raises(IllegalLifecycleTransition):
            led.move(Transition("h1", "Candidate", "Validated", "stale", "verdict:1", "validator"))  # wrong from
        with pytest.raises(IllegalLifecycleTransition):
            led.move(Transition("h1", "Validated", "Paper", "no evidence", "", "paper"))
        led.move(Transition("h1", "Validated", "Paper", "paper started", "paper:run-1", "paper"))
        assert led.state("h1") == "Paper"
        assert [h[1] for h in led.history("h1")] == ["Candidate", "Validated", "Paper"]
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute("delete from strategy_transition")
    finally:
        store.close()


def test_cusum_alarms_on_a_sustained_shortfall_and_not_on_noise() -> None:
    rule = DegradationRule()
    rng = np.random.default_rng(1)
    noise = 0.001 + 0.01 * rng.standard_normal(200)
    _, alarm = cusum_lower(noise, 0.001, 0.01, rule)
    assert alarm is None
    bad = np.concatenate([noise[:100], -0.015 + 0.01 * rng.standard_normal(100)])  # 1.6 sigma shortfall
    path, alarm_bad = cusum_lower(bad, 0.001, 0.01, rule)
    assert alarm_bad is not None and 100 <= alarm_bad < 130 and path.max() > rule.cusum_h
    with pytest.raises(ValueError):
        cusum_lower(noise, 0.0, 0.0, rule)
    assert DegradationRule().hash() != DegradationRule(cusum_h=6.0).hash()
    with pytest.raises(ValueError):
        DegradationRule(gap_ratio_max=1.5)


def test_slippage_and_gap_ratios() -> None:
    rule = DegradationRule()
    assert slippage_degraded([10.0, 12.0], [5.0, 5.0], rule) == (True, pytest.approx(2.2))
    assert slippage_degraded([8.0], [5.0], rule) == (False, pytest.approx(1.6))
    assert slippage_degraded([1.0], [0.0], rule)[0] is False
    assert gap_degraded(100, 11, rule) == (True, pytest.approx(0.11))
    assert gap_degraded(100, 10, rule) == (False, pytest.approx(0.10))
    assert gap_degraded(0, 0, rule)[0] is False


def test_final_judgement_starts_after_the_latest_known_cutoff() -> None:
    # a month cutoff covers the whole month (review: "2025-03" must not open the window on 2025-03-02)
    assert parse_cutoff("2025-03") == date(2025, 3, 31) and parse_cutoff("2025-03-15") == date(2025, 3, 15)
    assert parse_cutoff("2024-02") == date(2024, 2, 29) and parse_cutoff("2025-12") == date(2025, 12, 31)
    assert parse_cutoff("garbage") is None and parse_cutoff(None) is None
    assert final_judgement_from(["2025-03", "2025-01-10"]) == date(2025, 4, 1)
    assert final_judgement_from(["2025-03", None]) is None  # an unknown cutoff forbids any claim
    assert final_judgement_from([]) is None


def test_anonymization_difference_splits_by_the_hypothesis_flag(tmp_path: Path) -> None:
    store = ResearchStore.open(tmp_path / "r.db")
    try:
        ledger = TrialLedger(store, env_id="E", code_sha="s")
        conn = store.conn
        for hid, flag in ((1, 1), (2, 0)):  # one hypothesis per cycle (unique constraint)
            conn.execute(
                "insert into cycle(cycle_id, run_id, seq, state, status, config_json, started_ns, updated_ns) "
                "values (?, 'r', ?, 'DONE', 'done', '{}', 1, 1)",
                (hid, hid),
            )
            conn.execute(
                "insert into hypothesis(hypothesis_id, cycle_id, family, statement, falsification_test, "
                "required_data_json, required_features_json, prior, anonymized, struct_key, created_ns) "
                "values (?, ?, 'trend', 's', 'f', '[]', '[]', 0.5, ?, ?, 1)",
                (hid, hid, flag, f"k{hid}"),
            )
        from quant_research.research.ledger import TrialOrigin

        for hid, sr, cutoff in ((1, 0.1, "2025-01"), (1, 0.3, "2025-01"), (2, 0.5, None)):
            key = ledger.register_experiment(f"h{hid}{sr}", "{}", {"x": sr})
            with ledger.evaluate(
                key, key, "S3", origin=TrialOrigin(model_id="m", llm_cutoff=cutoff, hypothesis_id=hid, cycle_id=1)
            ) as t:
                t.metrics = {"sr_daily": sr}
        diff = anonymization_difference(ledger)
        assert (diff.n_anonymized, diff.n_real) == (2, 1)
        assert diff.mean_anonymized == pytest.approx(0.2) and diff.mean_real == pytest.approx(0.5)
        assert diff.difference == pytest.approx(0.3)
        ids = [int(r[0]) for r in conn.execute("select trial_id from trial")]
        assert set(cutoffs_of(ledger, ids)) == {"2025-01", None} and cutoffs_of(ledger, []) == []
        assert final_judgement_from(cutoffs_of(ledger, ids)) is None
    finally:
        store.close()
