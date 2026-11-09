"""Phase 3 step 1: research memory rules (design v2.1 §4 module I, §10)."""

import json
import sqlite3
from collections.abc import Generator
from pathlib import Path
from typing import cast

import pytest

from quant_research.research.ledger import TrialLedger, TrialOrigin
from quant_research.research.memory import CauseCode, MemoryRuleError, MemoryStore, clean_text, struct_key
from quant_research.research.store import ResearchStore


@pytest.fixture
def store(tmp_path: Path) -> Generator[ResearchStore]:
    s = ResearchStore.open(tmp_path / "r.db")
    yield s
    s.close()


def cycle(store: ResearchStore, seq: int, run: str = "run1") -> int:
    cur = store.conn.execute(
        "insert into cycle(run_id, seq, state, status, config_json, started_ns, updated_ns) "
        "values (?, ?, 'OBSERVE', 'running', '{}', 1, 1)",
        (run, seq),
    )
    return int(cur.lastrowid or 0)


def hypothesis(store: ResearchStore, cycle_id: int, key: str, family: str = "trend") -> int:
    cur = store.conn.execute(
        "insert into hypothesis(cycle_id, family, statement, falsification_test, required_data_json, "
        "required_features_json, prior, struct_key, created_ns) values (?,?,?,?,?,?,?,?,1)",
        (cycle_id, family, "s", "f", "[]", "[]", 0.3, key),
    )
    return int(cur.lastrowid or 0)


def test_a_finding_needs_evidence_in_code_and_in_sql(store: ResearchStore) -> None:
    mem = MemoryStore(store)
    c = cycle(store, 1)
    with pytest.raises(MemoryRuleError, match="evidence"):
        mem.record_finding(c, "trend", "x", [], hypothesis_id=None)
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "insert into finding(cycle_id, family, statement, evidence_refs_json, created_ns) "
            "values (?, 't', 's', '[]', 1)",
            (c,),
        )
    fid = mem.record_finding(c, "trend", "costs dominate", ["trial:7"], hypothesis_id=None)
    assert mem.active_findings() == [(fid, "trend", "costs dominate", ["trial:7"])]


def test_a_finding_is_superseded_once_and_never_rewritten(store: ResearchStore) -> None:
    mem = MemoryStore(store)
    c = cycle(store, 1)
    old = mem.record_finding(c, "trend", "a", ["trial:1"], hypothesis_id=None)
    new = mem.record_finding(c, "trend", "b", ["trial:2"], hypothesis_id=None)
    mem.supersede(old, new)
    assert [f[0] for f in mem.active_findings()] == [new]
    with pytest.raises(MemoryRuleError, match="already superseded"):
        mem.supersede(old, new)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store.conn.execute("update finding set statement = 'z' where finding_id = ?", (new,))


def test_failures_take_the_eight_cause_codes_only(store: ResearchStore) -> None:
    mem = MemoryStore(store)
    c = cycle(store, 1)
    mem.record_failure(c, "trend", "cost_erosion", "net < 0 < gross", {"trials": [1]}, hypothesis_id=None)
    with pytest.raises(MemoryRuleError, match="unknown cause"):
        mem.record_failure(c, "trend", cast(CauseCode, "bad_luck"), "x", {}, hypothesis_id=None)
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "insert into failure(cycle_id, family, cause_code, detail, evidence_json, created_ns) "
            "values (?, 't', 'x', 'd', '{}', 1)",
            (c,),
        )


def test_cooldown_covers_recent_failed_hypotheses_of_the_same_run_and_memory_off_disables_it(
    store: ResearchStore,
) -> None:
    mem = MemoryStore(store, cooldown_cycles=2)
    for seq, key in ((1, "k1"), (2, "k2"), (3, "k3")):
        c = cycle(store, seq)
        h = hypothesis(store, c, key)
        MemoryStore(store).record_failure(c, "trend", "weak_signal", "d", {}, hypothesis_id=h)
    assert mem.cooled_down(current_seq=4, run_id="run1") == {"k2", "k3"}  # k1 is older than 2 cycles
    assert mem.cooled_down(current_seq=4, run_id="other") == set()
    assert MemoryStore(store, enabled=False).cooled_down(current_seq=4, run_id="run1") == set()


def test_summary_aggregates_and_memory_off_is_empty(store: ResearchStore) -> None:
    mem = MemoryStore(store)
    c = cycle(store, 1)
    h = hypothesis(store, c, "k1", family="momentum")
    mem.record_failure(c, "momentum", "cost_erosion", "d", {}, hypothesis_id=h)
    mem.record_finding(c, "momentum", "turnover drives the loss", ["trial:3"], hypothesis_id=h)
    payload = mem.summary("run1", current_seq=2).as_payload()
    assert payload["families"] == [
        {"family": "momentum", "cycles": 1, "confirmed": 0, "failure_causes": {"cost_erosion": 1}}
    ]
    assert payload["findings"] == ["turnover drives the loss"]
    assert MemoryStore(store, enabled=False).summary("run1", 2).as_payload()["findings"] == []


def test_memory_survives_a_restart(tmp_path: Path) -> None:
    first = ResearchStore.open(tmp_path / "r.db")
    c = cycle(first, 1)
    MemoryStore(first).record_finding(c, "trend", "kept", ["trial:9"], hypothesis_id=None)
    first.close()
    again = ResearchStore.open(tmp_path / "r.db")
    try:
        assert [f[2] for f in MemoryStore(again).active_findings()] == ["kept"]
    finally:
        again.close()


def test_a_write_error_propagates_so_the_loop_stops(tmp_path: Path) -> None:
    s = ResearchStore.open(tmp_path / "r.db")
    c = cycle(s, 1)
    s.close()
    with pytest.raises(sqlite3.ProgrammingError):
        MemoryStore(s).record_finding(c, "trend", "x", ["trial:1"], hypothesis_id=None)


def test_struct_key_ignores_order_and_prose_and_text_is_cleaned() -> None:
    assert struct_key("trend", "trend", ["b", "a"], ["f"]) == struct_key("trend", "trend", ["a", "b"], ["f"])
    assert struct_key("trend", "trend", ["a"], []) != struct_key("momentum", "momentum", ["a"], [])
    assert clean_text("ok\x00\x1b[31mred\nline" + "x" * 3000, limit=20) == "ok[31mred\nlinexxxxxx"


def test_trials_carry_model_and_hypothesis_origin(store: ResearchStore) -> None:
    ledger = TrialLedger(store, env_id="E", code_sha="s")
    key = ledger.register_experiment("h", "{}", {})
    origin = TrialOrigin(model_id="fake-llm-v1", llm_cutoff="n/a", hypothesis_id=None, cycle_id=None)
    with ledger.evaluate(key, "h", "S2", origin=origin):
        pass
    row = store.conn.execute("select model_id, llm_cutoff, status from trial").fetchone()
    assert tuple(row) == ("fake-llm-v1", "n/a", "done")
    assert json.loads(store.conn.execute("select json_array(1, 2)").fetchone()[0]) == [1, 2]  # JSON1 available


def test_only_recorded_data_requests_block_a_family_not_modelling_caveats(store: ResearchStore) -> None:
    mem = MemoryStore(store)
    c = cycle(store, 1)
    mem.record_failure(c, "trend", "missing_data", "funding not modelled", {"flags": ["x"]}, hypothesis_id=None)
    assert mem.data_blocked() == set()  # a modelling caveat of a run does not block the family
    mem.record_failure(c, "funding_basis", "missing_data", "needs funding", {"data_request": 3}, hypothesis_id=None)
    assert mem.data_blocked() == {"funding_basis"}
