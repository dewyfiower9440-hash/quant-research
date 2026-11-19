"""Phase 5 step 7: validator.db — pre-registration, the global query budget k per epoch (re-queries count),
one-bit acks, the hash-chained holdout access log, and the research role's holdout refusal."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from pathlib import Path

import polars as pl
import pytest

from quant_research.catalog.bootstrap import bootstrap
from quant_research.catalog.store import Catalog
from quant_research.data.access import PointInTime, SealViolationError
from quant_research.research.store import ResearchStore
from quant_research.validator.store import (
    BudgetExhausted,
    Submission,
    ValidatorError,
    ValidatorStore,
    open_readonly,
    rule_hash,
)

RULE = {"alpha": 0.05, "k": 20, "c": 0.5, "stress": [2.0, 3.0]}


def _sub(req: int, spec: str = "spec-a", epoch: int = 1) -> Submission:
    return Submission(req, spec, rule_hash(RULE), f"key-{req}", 0.12, ("binance.um.klines_1h",), epoch)


@pytest.fixture
def store(tmp_path: Path) -> Generator[ValidatorStore]:
    clock = {"t": 1_000}

    def now() -> int:
        clock["t"] += 1
        return clock["t"]

    s = ValidatorStore.open(tmp_path / "validator.db", now_ns=now)
    yield s
    s.close()


def test_budget_is_global_per_epoch_and_a_requery_of_the_same_spec_counts(store: ValidatorStore) -> None:
    store.ensure_budget(1, 3)
    ids = [store.register(_sub(i, spec="same-spec")) for i in range(1, 5)]
    assert store.judge(ids[0], False, {"p": 0.4}) and store.judge(ids[1], True, {"p": 0.001})
    assert store.budget(1) == (3, 2)
    store.judge(ids[2], False, {"p": 0.9})  # third query: the same spec again still costs one
    with pytest.raises(BudgetExhausted):
        store.judge(ids[3], True, {"p": 0.0})
    assert store.budget(1) == (3, 3)
    assert store.submission(ids[3])[1] == "registered"  # refused queries leave the submission untouched
    with pytest.raises(ValidatorError):
        store.ensure_budget(1, 5)  # k is pre-registered per epoch
    with pytest.raises(ValidatorError):
        store.budget(2)


def test_acks_carry_exactly_one_bit_and_research_reads_them_read_only(store: ValidatorStore, tmp_path: Path) -> None:
    store.ensure_budget(1, 20)
    sid = store.register(_sub(7))
    assert store.ack(7) is None
    store.judge(sid, True, {"sr_holdout": 0.2, "se": 0.05, "p": 0.0001, "note": "internal only"})
    assert store.ack(7) is True
    ro = open_readonly(tmp_path / "validator.db")
    assert ro.execute("select passed from ack where request_id = 7").fetchone() == (1,)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("insert into ack(request_id, submission_id, passed, at_ns) values (99, 1, 1, 1)")
    ro.close()
    with pytest.raises(ValidatorError):
        store.judge(sid, False, {})  # judged twice
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("update verdict set passed = 0")  # verdicts are immutable
    with pytest.raises(ValidatorError):
        store.register(_sub(7))  # duplicate request id


def test_access_log_is_a_hash_chain_that_detects_tampering(store: ValidatorStore) -> None:
    store.ensure_budget(1, 20)
    sid = store.register(_sub(1))
    a = store.log_access(sid, "binance.um.klines_1h", 10, 20, "holdout verdict")
    b = store.log_access(sid, "binance.um.klines_1h", 20, 30, "holdout verdict")
    assert b.prev_hash == a.hash and len(a.hash) == 64
    assert store.verify_chain() == (True, "ok")
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("update holdout_access_log set purpose = 'x' where seq = 1")
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("delete from holdout_access_log where seq = 2")
    # a forged row that skips the chain (written around the triggers) is detected
    store.conn.execute(
        "insert into holdout_access_log(submission_id, dataset_id, window_from_ns, window_to_ns, purpose, at_ns, "
        "prev_hash, hash) values (?, 'd', 1, 2, 'forged', 5, ?, 'deadbeef')",
        (sid, a.hash),
    )
    ok, why = store.verify_chain()
    assert not ok and "seq 3" in why


def test_research_role_cannot_decide_in_the_holdout_but_the_validator_can(tmp_path: Path) -> None:
    catalog = Catalog.open(tmp_path / "c.db")
    bootstrap(catalog)
    try:
        dataset = "binance.um.bulk.klines_1m"
        seal = catalog.seal(dataset)
        inside = seal.research_until_ns + 1
        research, validator = PointInTime(catalog, role="research"), PointInTime(catalog, role="validator")
        with pytest.raises(SealViolationError):
            research.check_decisions(dataset, inside)
        validator.check_decisions(dataset, inside)  # the only role that may read the holdout
        frame = pl.DataFrame({"event_ns": [seal.research_until_ns - 10, inside], "value": [1.0, 2.0]})
        assert research.rows_visible(frame, dataset).height <= 1
        assert validator.rows_visible(frame, dataset).height == 2
    finally:
        catalog.close()


def test_research_outbox_table_exists_for_validation_requests(tmp_path: Path) -> None:
    s = ResearchStore.open(tmp_path / "r.db")
    try:
        cols = [r[1] for r in s.conn.execute("pragma table_info(validation_request)")]
        assert {
            "request_id",
            "trial_id",
            "spec_hash",
            "experiment_key",
            "in_sample_sr",
            "rule_hash",
            "created_ns",
        } <= set(cols)
        assert int(s.conn.execute("pragma user_version").fetchone()[0]) >= 3
    finally:
        s.close()
