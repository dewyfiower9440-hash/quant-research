"""Phase 3 step 6: the Director research loop end to end with the fake LLM (no network, no API)."""

import json
from collections.abc import Iterable, Mapping, Sequence
from itertools import pairwise
from typing import cast

import polars as pl
import pytest

from director.conftest import Env
from quant_research.backtest import run as run_module
from quant_research.director import director as director_module
from quant_research.director.budget import Budget
from quant_research.director.director import DirectorConfig, LeakBlocked
from quant_research.director.llm import FakeLLM, LLMResponse, ReplayLLM
from quant_research.director.policy import Choice, PolicyMode
from quant_research.director.schemas import HypothesisDraft
from quant_research.director.states import State, check_transition
from quant_research.research.memory import CauseCode, struct_key
from quant_research.strategy.compile import Compiled
from quant_research.strategy.spec import StrategySpec

S = State
Row = tuple[str | None, str, str, dict[str, object]]


def transitions(env: Env, cycle_id: int | None = None) -> list[Row]:
    rows = env.store.conn.execute(
        "select from_state, to_state, reason, evidence_json from transition "
        "where ? is null or cycle_id = ? order by transition_id",
        (cycle_id, cycle_id),
    ).fetchall()
    return [(None if r[0] is None else str(r[0]), str(r[1]), str(r[2]), json.loads(r[3])) for r in rows]


def done_duplicates(env: Env) -> list[tuple[str, int]]:
    rows = env.store.conn.execute(
        "select experiment_key, count(*) from trial where status = 'done' group by experiment_key, stage "
        "having count(*) > 1"
    ).fetchall()
    return [(str(r[0]), int(r[1])) for r in rows]


def test_ten_unattended_cycles_record_every_transition_with_reason_and_evidence(env: Env) -> None:
    results = env.director().run(10)
    assert [r.status for r in results] == ["done"] * 10
    assert all(reason for _f, _t, reason, _ev in transitions(env))
    for cycle in {r.cycle_id for r in results}:
        chain = transitions(env, cycle)
        assert chain[0][0] is None and chain[0][1] == "OBSERVE" and chain[-1][1] == "DONE"
        for (_f1, to1, _r1, _e1), (from2, to2, _r2, _e2) in pairwise(chain):
            assert from2 == to1
            check_transition(State(to1), State(to2))  # every recorded move is a §10 transition
    assert done_duplicates(env) == []  # no experiment was evaluated twice
    total, running = env.store.conn.execute("select count(*), sum(status = 'running') from trial").fetchone()
    runs = env.store.conn.execute("select count(*) from backtest_run").fetchone()[0]
    assert running == 0 and total >= runs > 0  # every evaluation accounted for, none left running
    assert env.store.conn.execute("select count(*) from trial where cycle_id is null").fetchone()[0] == 0
    # the loop really researched: plans in most cycles, no schema failures from the well-behaved fake LLM
    plans = env.store.conn.execute("select count(*) from experiment_plan").fetchone()[0]
    invalid = env.store.conn.execute("select count(*) from llm_call where status <> 'ok'").fetchone()[0]
    assert invalid == 0 and plans >= 7


def test_memory_is_reused_failures_shape_the_next_cycle(env: Env) -> None:
    env.director().run(10)
    observe = [ev for _f, to, _r, ev in transitions(env) if to == "HYPOTHESIZE" and "family" in ev]
    assert any(ev.get("last_causes") for ev in observe[1:])  # later cycles read earlier failure causes
    assert env.store.conn.execute("select count(*) from failure").fetchone()[0] > 0
    findings = env.store.conn.execute("select evidence_refs_json from finding").fetchall()
    assert findings and all(json.loads(f[0]) for f in findings)
    requests = [
        json.loads(r[0])
        for r in env.store.conn.execute(
            "select request_json from llm_call where purpose = 'hypothesize' order by llm_call_id"
        )
    ]
    assert requests[0]["memory"]["families"] == [] and any(r["memory"]["families"] for r in requests[1:])


def test_memory_off_hides_the_memory_and_the_policy_ignores_it(env: Env) -> None:
    env.director(config=DirectorConfig(run_id="r-off", seed=11, universe_n=2, memory=False)).run(4)
    requests = [
        json.loads(r[0])
        for r in env.store.conn.execute("select request_json from llm_call where purpose = 'hypothesize'")
    ]
    assert requests and all(r["memory"]["families"] == [] and r["memory"]["findings"] == [] for r in requests)
    observe = [ev for _f, to, _r, ev in transitions(env) if to == "HYPOTHESIZE" and "family" in ev]
    assert all(ev["memory"] is False and ev["last_causes"] == [] for ev in observe)


def test_uniform_policy_switch_is_recorded(env: Env) -> None:
    env.director(config=DirectorConfig(run_id="r-uni", seed=11, universe_n=2, policy="uniform")).run(2)
    observe = [ev for _f, to, _r, ev in transitions(env) if to == "HYPOTHESIZE" and "family" in ev]
    assert observe and all(ev["policy"] == "uniform" for ev in observe)


def test_missing_funding_blocks_the_family_through_a_recorded_data_request(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = director_module.choose_family

    def first_funding(
        *,
        mode: PolicyMode,
        outcomes: Mapping[str, tuple[int, int]],
        blocked: Iterable[str],
        last_family: str | None,
        last_causes: Sequence[CauseCode],
        consecutive_in_family: int = 0,
        seed: int,
        seq: int,
    ) -> Choice:
        if seq == 1:
            return Choice("funding_basis", "test: forced", {})
        return real(
            mode=mode,
            outcomes=outcomes,
            blocked=blocked,
            last_family=last_family,
            last_causes=last_causes,
            consecutive_in_family=consecutive_in_family,
            seed=seed,
            seq=seq,
        )

    monkeypatch.setattr(director_module, "choose_family", first_funding)
    env.director().run(3)
    need = json.loads(env.store.conn.execute("select need_json from data_request").fetchone()[0])
    assert need["missing"] == ["funding"] and need["family"] == "funding_basis"
    cause = env.store.conn.execute("select cause_code from failure where family = 'funding_basis'").fetchone()
    assert cause == ("missing_data",)
    later = [ev for _f, to, _r, ev in transitions(env) if to == "HYPOTHESIZE" and "blocked" in ev][1:]
    assert later
    for ev in later:
        assert "funding_basis" in cast(list[str], ev["blocked"]) and ev["family"] != "funding_basis"


def test_restart_after_a_crash_between_states_resumes_without_repeating_trials(env: Env) -> None:
    with pytest.raises(KeyboardInterrupt):
        env.director(crash_at=S.BACKTEST).run(1)
    before = env.store.conn.execute("select count(*) from trial").fetchone()[0]
    assert env.store.conn.execute("select state from cycle").fetchone()[0] == "BACKTEST"
    env.reopen()
    results = env.director().run(1)
    assert results[0].status == "done" and results[0].states[0] == "BACKTEST"
    s2 = env.store.conn.execute("select count(*) from trial where stage = 'S2'").fetchone()[0]
    assert s2 == before  # selection trials were not run again
    assert done_duplicates(env) == []


def test_a_crash_inside_an_evaluation_leaves_an_aborted_trial_and_resume_evaluates_it_once(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = run_module.compile_spec
    calls = {"n": 0}

    def flaky(spec: StrategySpec, panel: pl.DataFrame) -> Compiled:
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt("power loss during the evaluation")
        return real(spec, panel)

    monkeypatch.setattr(run_module, "compile_spec", flaky)
    with pytest.raises(KeyboardInterrupt):
        env.director().run(1)
    assert env.store.conn.execute("select status from trial").fetchall() == [("aborted",)]
    env.reopen()
    env.director().run(1)
    statuses = [r[0] for r in env.store.conn.execute("select status from trial order by trial_id")]
    assert statuses[0] == "aborted" and "done" in statuses[1:]
    assert done_duplicates(env) == []  # the aborted evaluation counts as a trial; nothing is lost or doubled


def test_schema_violations_are_retried_once_then_discarded(env: Env) -> None:
    res = env.director(FakeLLM(seed=1, script={1: "invalid_json", 2: "wrong_schema"})).run(1)
    assert res[0].status == "done"
    calls = env.store.conn.execute("select attempt, status from llm_call order by llm_call_id").fetchall()
    assert calls == [(1, "schema_invalid"), (2, "schema_invalid")]
    assert [t for _f, t, _r, _e in transitions(env)][-2:] == ["REPLAN", "DONE"]
    assert env.store.conn.execute("select count(*) from hypothesis").fetchone()[0] == 0


def test_a_refusal_is_recorded_and_the_cycle_replans(env: Env) -> None:
    env.director(FakeLLM(script={1: "refusal"})).run(1)
    assert env.store.conn.execute("select status from llm_call").fetchall() == [("refusal",)]
    assert "REPLAN" in [t for _f, t, _r, _e in transitions(env)]


def test_prompt_injection_text_is_stored_as_data_and_changes_nothing(env: Env) -> None:
    env.director(FakeLLM(seed=2, script={1: "injection", 2: "injection", 3: "injection"})).run(1)
    statement, family = env.store.conn.execute("select statement, family from hypothesis").fetchone()
    chosen = next(ev["family"] for _f, to, _r, ev in transitions(env) if to == "HYPOTHESIZE")
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in statement and family == chosen
    validate = [r for _f, to, r, _e in transitions(env) if to == "ANALYZE" and "validator" in r]
    assert all("no holdout access" in r for r in validate)


def test_a_payload_with_a_real_symbol_is_never_sent(env: Env) -> None:
    d = env.director()
    cycle_id, _state, _status = d.open_cycle(1)
    with pytest.raises(LeakBlocked):
        d.ask(cycle_id, "hypothesize", {"note": "AAA looked strong"}, HypothesisDraft)
    assert env.store.conn.execute("select status from llm_call").fetchall() == [("blocked",)]


def test_budget_exhaustion_pauses_and_resume_continues(env: Env) -> None:
    tight = Budget(max_calls_per_cycle=1)
    res = env.director(budget=tight).run(1)
    assert res[0].status == "paused"
    assert env.store.conn.execute("select status from cycle").fetchone()[0] == "paused"
    again = env.director().run(1)
    assert again[0].status == "done" and again[0].states[0] == "OBSERVE"
    reasons = [r for _f, t, r, _e in transitions(env) if t == "OBSERVE" and r == "resumed after pause"]
    assert reasons == ["resumed after pause"]


def test_a_blocked_resource_gate_pauses_before_any_evaluation(env: Env) -> None:
    res = env.director(gate=lambda: (False, "SSD critical")).run(1)
    assert res[0].status == "paused"
    assert env.store.conn.execute("select count(*) from trial").fetchone()[0] == 0
    assert any("SSD critical" in r for _f, _t, r, _e in transitions(env))


def test_replaying_stored_answers_reproduces_the_decisions(env: Env, tmp_path_factory: pytest.TempPathFactory) -> None:
    env.director().run(3)
    first = [(t, r) for _f, t, r, _e in transitions(env)]
    stored = [
        LLMResponse("ok" if s in ("ok", "schema_invalid") else "refusal", text, i, o)
        for s, text, i, o in env.store.conn.execute(
            "select status, response_text, input_tokens, output_tokens from llm_call order by llm_call_id"
        )
    ]
    other = tmp_path_factory.mktemp("replay")
    from quant_research.catalog.bootstrap import bootstrap
    from quant_research.catalog.store import Catalog
    from quant_research.research.ledger import TrialLedger
    from quant_research.research.store import ResearchStore

    cat = Catalog.open(other / "c.db")
    bootstrap(cat)
    store = ResearchStore.open(other / "r.db")
    try:
        replay = Env(
            cat, store, TrialLedger(store, env_id="ENV-T", code_sha="sha"), env.selection, env.confirmation, other
        )
        replay.director(ReplayLLM(stored)).run(3)
        assert [(t, r) for _f, t, r, _e in transitions(replay)] == first
    finally:
        store.close()
        cat.close()


def test_the_policy_skips_families_in_cooldown_so_cycles_stay_productive(env: Env) -> None:
    """Real-data run 2026-11-10: cycles 6, 8-10 picked a family in cooldown and ended idle."""
    for seed in range(8):
        run = f"cool-{seed}"
        cur = env.store.conn.execute(
            "insert into cycle(run_id, seq, state, status, config_json, started_ns, updated_ns) "
            "values (?, 1, 'DONE', 'done', '{}', 1, 1)",
            (run,),
        )
        prev = int(cur.lastrowid or 0)
        hid = env.store.conn.execute(
            "insert into hypothesis(cycle_id, family, statement, falsification_test, required_data_json, "
            "required_features_json, prior, struct_key, created_ns) values (?,?,?,?,?,?,?,?,1)",
            (prev, "trend", "s", "f", '["klines"]', "[]", 0.3, struct_key("trend", "trend", ["klines"], [])),
        ).lastrowid
        env.store.conn.execute(
            "insert into failure(cycle_id, hypothesis_id, family, cause_code, detail, evidence_json, created_ns) "
            "values (?, ?, 'trend', 'weak_signal', 'd', '{}', 1)",
            (prev, hid),
        )
        res = env.director(config=DirectorConfig(run_id=run, seed=seed, universe_n=2)).run_cycle(2)
        chain = transitions(env, res.cycle_id)
        observe = next(ev for _f, to, _r, ev in chain if to == "HYPOTHESIZE" and "family" in ev)
        assert observe["family"] != "trend" and "trend" in cast(list[str], observe["cooled_down"])
        assert not any("no novel hypothesis" in r for _f, _t, r, _e in chain)


def test_a_family_whose_fixed_grid_is_fully_evaluated_is_skipped_as_exhausted(env: Env) -> None:
    """Real-data rerun 2026-11-10: the trend grid was exhausted across runs and cycles 5, 7-9 idled."""
    d = env.director()
    for key in d.grid_keys("trend"):
        env.store.conn.execute(
            "insert into experiment(experiment_key, spec_hash, spec_json, config_json, created_ns) "
            "values (?, 'h', '{}', '{}', 1)",
            (key,),
        )
        env.store.conn.execute(
            "insert into trial(experiment_key, spec_hash, stage, status, env_id, code_sha, started_ns, finished_ns) "
            "values (?, 'h', 'S2', 'done', 'E', 's', 1, 2)",
            (key,),
        )
    res = d.run_cycle(1)
    observe = next(ev for _f, to, _r, ev in transitions(env, res.cycle_id) if to == "HYPOTHESIZE" and "family" in ev)
    assert observe["family"] != "trend" and observe["exhausted"] == ["trend"]


def test_when_every_family_is_exhausted_or_blocked_the_run_pauses_instead_of_spending_llm_calls(env: Env) -> None:
    d = env.director()
    for family in ("trend", "mean_reversion", "momentum", "funding_basis", "volatility"):
        for key in d.grid_keys(family):
            env.store.conn.execute(
                "insert into experiment(experiment_key, spec_hash, spec_json, config_json, created_ns) "
                "values (?, 'h', '{}', '{}', 1)",
                (key,),
            )
            env.store.conn.execute(
                "insert into trial(experiment_key, spec_hash, stage, status, env_id, code_sha, started_ns, "
                "finished_ns) values (?, 'h', 'S2', 'done', 'E', 's', 1, 2)",
                (key,),
            )
    results = d.run(5)
    assert [r.status for r in results] == ["paused"]  # the run stops at the first idle cycle
    assert env.store.conn.execute("select count(*) from llm_call").fetchone()[0] == 0
    reason = transitions(env)[-1][2]
    assert "nothing left to research" in reason
