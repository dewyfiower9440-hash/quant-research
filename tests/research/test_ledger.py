"""Phase 2 step 1: trial accounting invariant (design v2.1 §8: one fitness evaluation = one trial)."""

import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest

from quant_research.research.ledger import TrialLedger, experiment_key
from quant_research.research.store import ResearchStore


@pytest.fixture
def ledger(tmp_path: Path) -> Generator[TrialLedger]:
    store = ResearchStore.open(tmp_path / "research.db")
    yield TrialLedger(store, env_id="ENV-T", code_sha="abc")
    store.close()


def _statuses(ledger: TrialLedger) -> list[tuple[str, str | None]]:
    rows = ledger.store.conn.execute("select status, reason from trial order by trial_id")
    return [(str(r[0]), None if r[1] is None else str(r[1])) for r in rows]


def test_experiment_key_changes_with_any_part_of_the_configuration() -> None:
    base = {"data_version": "d1", "lag_versions": {"x": 1}, "engine": "event", "seed": 0}
    assert experiment_key("h", base) == experiment_key("h", dict(reversed(list(base.items()))))  # order-free
    assert experiment_key("h", base) != experiment_key("h2", base)
    assert experiment_key("h", base) != experiment_key("h", {**base, "lag_versions": {"x": 2}})


def test_every_evaluation_records_exactly_one_trial_even_when_repeated(ledger: TrialLedger) -> None:
    key = ledger.register_experiment("h", '{"spec": 1}', {"seed": 0})
    for _ in range(3):  # re-evaluating the same experiment is a new trial (no free re-runs)
        with ledger.evaluate(key, "h", "smoke") as trial:
            trial.metrics = {"sr": 0.1}
    assert _statuses(ledger) == [("done", None)] * 3
    assert ledger.register_experiment("h", '{"spec": 1}', {"seed": 0}) == key  # idempotent registration


def test_a_failing_evaluation_is_recorded_as_failed_and_the_error_propagates(ledger: TrialLedger) -> None:
    key = ledger.register_experiment("h", "{}", {})
    with pytest.raises(ValueError, match="boom"), ledger.evaluate(key, "h", "smoke"):
        raise ValueError("boom")
    assert _statuses(ledger) == [("failed", "ValueError: boom")]


def test_a_rejected_evaluation_keeps_its_reason(ledger: TrialLedger) -> None:
    key = ledger.register_experiment("h", "{}", {})
    with ledger.evaluate(key, "h", "S0") as trial:
        trial.reject("L2 fidelity needs aggTrades (not collected)")
    assert _statuses(ledger) == [("rejected", "L2 fidelity needs aggTrades (not collected)")]


def test_an_interrupted_evaluation_is_recorded_as_aborted(ledger: TrialLedger) -> None:
    key = ledger.register_experiment("h", "{}", {})
    with pytest.raises(KeyboardInterrupt), ledger.evaluate(key, "h", "smoke"):
        raise KeyboardInterrupt
    assert _statuses(ledger) == [("aborted", "KeyboardInterrupt")]


def test_a_trial_left_running_by_a_dead_process_is_closed_as_aborted(tmp_path: Path) -> None:
    path = tmp_path / "research.db"
    first = TrialLedger(ResearchStore.open(path), env_id="E", code_sha="s")
    key = first.register_experiment("h", "{}", {})
    first.store.conn.execute(
        "insert into trial(experiment_key, spec_hash, stage, status, env_id, code_sha, started_ns) "
        "values (?, 'h', 'smoke', 'running', 'E', 's', 1)",
        (key,),
    )
    first.store.close()
    second = TrialLedger(ResearchStore.open(path), env_id="E", code_sha="s")
    assert second.close_abandoned() == 1
    assert _statuses(second) == [("aborted", "process ended during the evaluation")]
    second.store.close()


def test_finished_trials_cannot_be_changed_or_deleted(ledger: TrialLedger) -> None:
    key = ledger.register_experiment("h", "{}", {})
    with ledger.evaluate(key, "h", "smoke"):
        pass
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger.store.conn.execute("update trial set status = 'failed'")
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        ledger.store.conn.execute("delete from trial")


def test_trial_records_environment_and_code_version(ledger: TrialLedger) -> None:
    key = ledger.register_experiment("h", "{}", {})
    with ledger.evaluate(key, "h", "smoke", fold="f1"):
        pass
    row = ledger.store.conn.execute("select env_id, code_sha, fold, finished_ns >= started_ns from trial").fetchone()
    assert tuple(row) == ("ENV-T", "abc", "f1", 1)
