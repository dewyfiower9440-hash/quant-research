"""F-016: the structured-output contract between the Director and a real model.

The first real-LLM run (cli-s7-on-posterior-20261110) failed 6/6 hypothesis answers: the value constraints
were stripped from the schema sent to the model, and the feature-name rule allowed no digits and named no
vocabulary. These tests pin the fixed contract: the model is told every constraint, feature names follow the
DSL grammar, local validation stays the authority, and a violation is fed back field by field.
"""

import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from director.conftest import Env
from quant_research.director.llm import Behaviour, FakeLLM, LLMRequest, LLMResponse
from quant_research.director.schemas import (
    FEATURE_NAME,
    AnalysisDraft,
    HypothesisDraft,
    PlanDraft,
    api_schema,
    constraint_notes,
    feature_problem,
    validation_feedback,
)

FIXTURE = Path(__file__).parent / "fixtures" / "f016_real_answers.json"
REAL = cast(list[dict[str, object]], json.loads(FIXTURE.read_text(encoding="utf-8")))


def base(**over: object) -> dict[str, object]:
    body: dict[str, object] = {
        "family": "trend",
        "statement": "trend persists after costs",
        "falsification_test": "confirmation net return <= 0",
        "required_data": ["klines"],
        "required_features": [],
        "prior": 0.3,
    }
    body.update(over)
    return body


def errors_of(body: str | dict[str, object]) -> list[dict[str, str]]:
    with pytest.raises(ValidationError) as info:
        if isinstance(body, str):
            HypothesisDraft.model_validate_json(body)
        else:
            HypothesisDraft.model_validate(body)
    return validation_feedback(info.value)


# ------------------------------------------------------------------ regression: the 6 real answers
def test_the_six_recorded_real_answers_are_still_rejected_for_the_recorded_reasons() -> None:
    assert len(REAL) == 6
    for item in REAL:
        text = str(item["response_text"])
        fields = {e["field"] for e in errors_of(text)}
        assert "required_features" in fields  # free-form names such as ret_24h are not DSL features
        if item["attempt"] == 1:
            assert len(json.loads(text)["statement"]) > 600 and "statement" in fields


def test_feedback_names_the_field_and_constraint_and_never_echoes_the_answer() -> None:
    first = str(REAL[0]["response_text"])
    feedback = errors_of(first)
    statement = json.loads(first)["statement"]
    by_field = {e["field"]: e["problem"] for e in feedback}
    assert "600" in by_field["statement"]
    assert "ret_24h" not in by_field["required_features"]  # this answer used other names
    assert "return_20d" in by_field["required_features"] and "ema_50_close" in by_field["required_features"]
    assert statement[:40] not in json.dumps(feedback)


# ------------------------------------------------------------------ feature names
ACCEPTED = [
    "close",
    "ema",
    "rank_xs",
    "funding_rate",
    "ema_50_close",
    "ret_24",
    "std_168",
    "atr_14",
    "zscore_48_funding_rate",
    "rank_xs_close",
    "expanding_quantile_volume",
    "rolling_max_2000_high",
]


@pytest.mark.parametrize("name", ACCEPTED)
def test_dsl_feature_names_with_digits_and_old_names_are_accepted(name: str) -> None:
    assert feature_problem(name) is None
    assert HypothesisDraft.model_validate(base(required_features=[name])).required_features == [name]


@pytest.mark.parametrize(
    "name",
    ["realized_vol_60d", "ret_24h", "ema_0", "ema_2001", "ema_050", "foo", "rank_xs_7", "close_50", "ema_50_foo"],
)
def test_names_that_are_not_dsl_features_are_rejected(name: str) -> None:
    assert feature_problem(name) is not None
    problems = {e["field"]: e["problem"] for e in errors_of(base(required_features=[name]))}
    assert name in problems["required_features"]


@pytest.mark.parametrize("name", ["1ema", "Ema_50", "_close", "e" * 33, "ema-50", "", "../etc/passwd"])
def test_names_outside_the_pattern_are_rejected(name: str) -> None:
    assert FEATURE_NAME == r"^[a-z][a-z0-9_]{0,31}$"
    assert feature_problem(name) is not None
    errors_of(base(required_features=[name]))


# ------------------------------------------------------------------ lengths and ranges stay local
def test_statement_length_boundary_is_enforced_locally() -> None:
    assert HypothesisDraft.model_validate(base(statement="x" * 600))
    problems = {e["field"]: e["problem"] for e in errors_of(base(statement="x" * 601))}
    assert "600" in problems["statement"]
    problems = {e["field"]: e["problem"] for e in errors_of(base(prior=1.5))}
    assert "1" in problems["prior"]


# ------------------------------------------------------------------ the model is told every constraint
def test_the_sent_schema_carries_dropped_constraints_as_descriptions() -> None:
    schema = api_schema(HypothesisDraft.model_json_schema())
    text = json.dumps(schema)
    assert "maxLength" not in text and "pattern" not in text and "minimum" not in text
    props = cast(dict[str, dict[str, object]], schema["properties"])
    assert "600" in str(props["statement"]["description"]) and "10" in str(props["statement"]["description"])
    assert "0" in str(props["prior"]["description"]) and "1" in str(props["prior"]["description"])
    features = json.dumps(props["required_features"])
    assert "ema_50_close" in features and "8" in features


def test_constraint_notes_list_every_field_with_its_type_and_limits() -> None:
    notes = constraint_notes(HypothesisDraft)
    needles = ["family", "trend", "mean_reversion", "statement", "10-600 characters", "prior", "0-1"]
    needles += ["required_features", "at most 8", "ema_50_close", "no unit suffixes"]
    for needle in needles:
        assert needle in notes, needle
    for model in (PlanDraft, AnalysisDraft):
        assert "characters" in constraint_notes(model)


# ------------------------------------------------------------------ the Director re-asks with the feedback
@pytest.mark.parametrize("behaviour", ["too_long", "unknown_feature"])
def test_a_violation_is_fed_back_and_the_regenerated_answer_is_accepted(env: Env, behaviour: Behaviour) -> None:
    llm = FakeLLM(seed=4, script={1: behaviour})
    env.director(llm).run(1)
    calls = env.store.conn.execute(
        "select attempt, status, request_json from llm_call where purpose = 'hypothesize' order by llm_call_id"
    ).fetchall()
    assert [(a, s) for a, s, _r in calls] == [(1, "schema_invalid"), (2, "ok")]
    feedback = json.loads(calls[1][2])["previous_answer_error"]
    field = "statement" if behaviour == "too_long" else "required_features"
    assert any(e["field"] == field for e in feedback)
    assert env.store.conn.execute("select count(*) from hypothesis").fetchone()[0] == 1


def test_every_request_tells_the_model_the_constraints(env: Env) -> None:
    seen: list[str] = []

    class Spy(FakeLLM):
        def complete(self, request: LLMRequest) -> LLMResponse:
            seen.append(request.system)
            return super().complete(request)

    env.director(Spy(seed=1)).run(1)
    assert seen and all("Answer constraints" in s for s in seen)
