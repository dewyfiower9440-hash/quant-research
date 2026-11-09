"""Phase 3 steps 3-5: schemas, fake LLM, budget, state machine, failure classifier, policy (pure parts)."""

import json
from collections.abc import Sequence
from typing import cast

import pytest
from pydantic import ValidationError

from quant_research.director.budget import PRICES, Budget, BudgetExceeded, cost_usd, estimate_tokens
from quant_research.director.failures import Cause, RunView, classify
from quant_research.director.llm import FakeLLM, LLMRequest
from quant_research.director.policy import choose_family
from quant_research.director.schemas import AnalysisDraft, HypothesisDraft, PlanDraft, Purpose
from quant_research.director.states import TRANSITIONS, IllegalTransition, State, check_transition
from quant_research.strategy.templates import FAMILIES, GRID

S = State


def request(purpose: Purpose = "hypothesize", family: str = "trend") -> LLMRequest:
    payload: dict[str, object] = {"required_family": family, "rule_based_causes": ["weak_signal"]}
    return LLMRequest(purpose, "system", payload, {}, 500)


# ------------------------------------------------------------------ schemas
def test_schemas_accept_a_valid_hypothesis_and_reject_extras_and_closed_vocabulary_violations() -> None:
    ok = HypothesisDraft.model_validate(
        {
            "family": "trend",
            "statement": "trend persists after costs",
            "falsification_test": "confirmation net return <= 0",
            "required_data": ["klines"],
            "required_features": ["ema"],
            "prior": 0.3,
        }
    )
    assert ok.family == "trend"
    for bad in (
        {**ok.model_dump(), "family": "astrology"},
        {**ok.model_dump(), "required_data": ["twitter"]},
        {**ok.model_dump(), "required_features": ["../etc/passwd"]},
        {**ok.model_dump(), "prior": 1.5},
        {**ok.model_dump(), "execute": "rm -rf /"},
        {**ok.model_dump(), "statement": "x" * 601},
    ):
        with pytest.raises(ValidationError):
            HypothesisDraft.model_validate(bad)


def test_plan_variants_must_be_distinct_grid_indices_and_analysis_is_bounded() -> None:
    assert PlanDraft.model_validate({"template": "trend", "variants": [0, 2], "rationale": "spread the grid"})
    for variants in ([1, 1], [-1], [0, 1, 2, 3]):
        with pytest.raises(ValidationError):
            PlanDraft.model_validate({"template": "trend", "variants": variants, "rationale": "spread the grid"})
    with pytest.raises(ValidationError):
        AnalysisDraft.model_validate({"interpretation": "short", "agrees_with_causes": True})


# ------------------------------------------------------------------ fake LLM
def test_fake_llm_answers_follow_the_requested_family_and_parse() -> None:
    llm = FakeLLM(seed=3)
    for family in FAMILIES:
        h = HypothesisDraft.model_validate_json(llm.complete(request("hypothesize", family)).text or "")
        p = PlanDraft.model_validate_json(llm.complete(request("plan", family)).text or "")
        assert h.family == family and p.template == family and max(p.variants) < len(GRID[family])
    a = AnalysisDraft.model_validate_json(llm.complete(request("analyze")).text or "")
    assert a.agrees_with_causes and llm.billed is False


def test_fake_llm_scripted_misbehaviour() -> None:
    llm = FakeLLM(script={1: "invalid_json", 2: "wrong_schema", 3: "refusal", 4: "error", 5: "injection"})
    assert llm.complete(request()).status == "ok"  # invalid JSON is still an "ok" transport result
    with pytest.raises(ValidationError):
        HypothesisDraft.model_validate_json(llm.complete(request()).text or "")
    assert llm.complete(request()).status == "refusal"
    assert llm.complete(request()).status == "error"
    injected = HypothesisDraft.model_validate_json(llm.complete(request()).text or "")
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in injected.statement and injected.family == "trend"


def test_fake_llm_is_deterministic_for_a_seed() -> None:
    a = [FakeLLM(seed=7).complete(request("plan", "momentum")).text for _ in range(1)]
    b = [FakeLLM(seed=7).complete(request("plan", "momentum")).text for _ in range(1)]
    assert a == b


# ------------------------------------------------------------------ budget
def test_cost_uses_the_official_price_table() -> None:
    assert PRICES["claude-opus-5-5"].input == 4.0 and PRICES["claude-opus-5-5"].output == 20.0
    assert cost_usd("claude-opus-5-5", input_tokens=1_000_000, output_tokens=0) == pytest.approx(4.0)
    assert cost_usd("claude-opus-5-5", input_tokens=0, output_tokens=1_000_000, cache_read_tokens=1_000_000) == (
        pytest.approx(20.2)
    )
    assert estimate_tokens("x" * 300) == 100


def test_budget_blocks_a_call_that_could_break_the_total_or_cycle_cap() -> None:
    b = Budget(max_total_usd=5.0, max_cycle_usd=1.0, max_output_tokens=8_000)
    one_call = b.check(model_id="claude-opus-5-5", prompt_text="x" * 3_000, billed=True)
    assert one_call == pytest.approx((1_000 * 4 + 8_000 * 20) / 1e6)
    b.spent_billed_usd = 4.9
    with pytest.raises(BudgetExceeded, match="total cap"):
        b.check(model_id="claude-opus-5-5", prompt_text="x" * 3_000, billed=True)
    b.spent_billed_usd, b.cycle_usd = 0.0, 0.9
    with pytest.raises(BudgetExceeded, match="cycle cap"):
        b.check(model_id="claude-opus-5-5", prompt_text="x" * 3_000, billed=True)
    assert b.check(model_id="claude-opus-5-5", prompt_text="x", billed=False) > 0  # fake: estimate, no cap


def test_budget_limits_prompt_size_and_calls_per_cycle_for_every_llm() -> None:
    b = Budget(max_input_tokens=10, max_calls_per_cycle=1)
    with pytest.raises(BudgetExceeded, match="per-call limit"):
        b.check(model_id="fake", prompt_text="x" * 31, billed=False)
    b.record(usage_cost=0.0, billed=False)
    with pytest.raises(BudgetExceeded, match="calls per cycle"):
        b.check(model_id="fake", prompt_text="x", billed=False)


# ------------------------------------------------------------------ state machine
def test_every_listed_transition_is_allowed_and_others_are_not() -> None:
    for src, dsts in TRANSITIONS.items():
        for dst in State:
            if dst in dsts or (dst == S.PAUSED and src not in (S.PAUSED, S.DONE)):
                check_transition(src, dst)
            else:
                with pytest.raises(IllegalTransition):
                    check_transition(src, dst)


def test_the_happy_path_of_section_10_is_a_chain_of_allowed_transitions() -> None:
    path = [S.OBSERVE, S.HYPOTHESIZE, S.PLAN, S.GENERATE, S.SEARCH, S.BACKTEST, S.VALIDATE, S.ANALYZE, S.LEARN]
    for a, b in zip([*path, S.REPLAN], [*path[1:], S.REPLAN, S.DONE], strict=True):
        check_transition(a, b)
    with pytest.raises(IllegalTransition):
        check_transition(S.SEARCH, S.VALIDATE)  # no validation without the confirmation backtest


# ------------------------------------------------------------------ failure classifier
def run(tid: int, status: str = "done", **m: object) -> RunView:
    return RunView(tid, status, metrics=m)


def codes(causes: Sequence[Cause]) -> set[str]:
    return {c.code for c in causes}


@pytest.mark.parametrize(
    ("selection", "confirmation", "kw", "expected"),
    [
        ([run(1, total_return=-0.05, costs=100.0, trades=50)], None, {}, {"weak_signal"}),
        ([run(1, total_return=-0.01, costs=300.0, trades=50)], None, {}, {"cost_erosion"}),  # gross +2 %
        ([run(1, total_return=0.0, costs=0.0, trades=0)], None, {}, {"weak_signal"}),
        ([run(1, "rejected")], None, {}, {"infeasible"}),
        (
            [run(1, total_return=0.01, costs=10.0, trades=5, flags=["funding_not_modelled: x"])],
            None,
            {},
            {"missing_data"},
        ),
        ([run(1, total_return=0.01, costs=10.0, trades=5)], None, {"data_missing": ["funding"]}, {"missing_data"}),
        (
            [run(1, total_return=0.02, costs=10.0, trades=5, sr_daily=0.2)],
            run(2, total_return=-0.03, costs=10.0, trades=5, sr_daily=-0.1),
            {},
            {"overfit", "weak_signal"},
        ),
        ([run(1, total_return=0.01, costs=500.0, trades=5, turnover=1.0)], None, {}, {"capacity"}),
        (
            [run(1, total_return=0.01, costs=10.0, trades=5)],
            None,
            {"named_vs_anonymized_gap": 0.9},
            {"prior_knowledge"},
        ),
    ],
)
def test_cause_codes(
    selection: list[RunView], confirmation: RunView | None, kw: dict[str, object], expected: set[str]
) -> None:
    data_missing = kw.get("data_missing", [])
    gap = kw.get("named_vs_anonymized_gap")
    out = classify(
        selection,
        confirmation,
        data_missing=[str(x) for x in cast(list[object], data_missing)] if isinstance(data_missing, list) else [],
        named_vs_anonymized_gap=float(gap) if isinstance(gap, float) else None,
    )
    assert codes(list(out)) == expected
    assert all(c.evidence for c in out)


def test_regime_dependence_from_a_sign_flip_between_halves() -> None:
    daily = [0.01] * 15 + [-0.01] * 15
    conf = RunView(2, "done", metrics={"total_return": 0.0, "costs": 0.0, "trades": 3, "sr_daily": 0.0}, daily=daily)
    out = classify([run(1, total_return=0.0, costs=0.0, trades=3, sr_daily=0.0)], conf)
    assert "regime_dependent" in codes(list(out))


# ------------------------------------------------------------------ policy
def test_policy_stays_once_after_cost_erosion_then_leaves() -> None:
    first = choose_family(
        mode="posterior", outcomes={}, blocked=[], last_family="momentum", last_causes=["cost_erosion"], seed=1, seq=2
    )
    assert first.family == "momentum" and "stay" in first.reason
    again = choose_family(
        mode="posterior",
        outcomes={},
        blocked=[],
        last_family="momentum",
        last_causes=["cost_erosion"],
        consecutive_in_family=2,
        seed=1,
        seq=3,
    )
    assert again.family != "momentum"


def test_policy_leaves_after_weak_signal_and_respects_blocked_families() -> None:
    for seq in range(20):
        c = choose_family(
            mode="posterior",
            outcomes={},
            blocked=["funding_basis"],
            last_family="trend",
            last_causes=["weak_signal"],
            seed=5,
            seq=seq,
        )
        assert c.family not in ("trend", "funding_basis")


def test_posterior_favours_families_that_passed_and_uniform_ignores_it() -> None:
    outcomes = {"momentum": (8, 0), "trend": (0, 8), "mean_reversion": (0, 8), "volatility": (0, 8)}
    picks = [
        choose_family(
            mode="posterior",
            outcomes=outcomes,
            blocked=["funding_basis"],
            last_family=None,
            last_causes=[],
            seed=9,
            seq=s,
        ).family
        for s in range(40)
    ]
    assert picks.count("momentum") > 30
    uniform = [
        choose_family(
            mode="uniform",
            outcomes=outcomes,
            blocked=["funding_basis"],
            last_family=None,
            last_causes=[],
            seed=9,
            seq=s,
        ).family
        for s in range(400)
    ]
    assert 60 < uniform.count("momentum") < 140  # ~1/4 of 400
    same = choose_family(
        mode="posterior", outcomes=outcomes, blocked=[], last_family=None, last_causes=[], seed=9, seq=1
    )
    assert same == choose_family(
        mode="posterior", outcomes=outcomes, blocked=[], last_family=None, last_causes=[], seed=9, seq=1
    )  # reproducible for a seed
    assert json.dumps(same.posterior)  # serializable for the transition record
