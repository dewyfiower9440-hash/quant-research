"""Structured LLM outputs of the Director (design v2.1 §4 A: Hypothesis / ExperimentPlan / analysis).

Everything an LLM returns is untrusted data: it is parsed into these models (closed vocabularies, length
caps, no extra fields) and only the parsed values are used. Free text is stored and may be shown again as
quoted data, never executed as an instruction (§6-5, §15 prompt injection).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from quant_research.strategy.ops import MAX_WINDOW, OPS

Family = Literal["trend", "mean_reversion", "momentum", "funding_basis", "volatility"]
DataKind = Literal["klines", "mark", "funding", "premium"]
# F-016 (2026-11-10): digits allowed; every name of the old rule that starts with a letter stays valid.
FEATURE_NAME = r"^[a-z][a-z0-9_]{0,31}$"

# DSL feature grammar (strategy/ops.py is the vocabulary; windows are in bars, 1 bar = 1 hour):
#   <column> | <operator> | <windowed op>_<window> | <windowed op>_<window>_<column> | <plain op>_<column>
_COLUMNS = sorted((k for k, s in OPS.items() if s.kind == "data" and not s.window), key=len, reverse=True)
_WINDOWED = sorted((k for k, s in OPS.items() if s.window), key=len, reverse=True)
_PLAIN = sorted((k for k, s in OPS.items() if s.kind in ("xs", "regime")), key=len, reverse=True)


def _alt(names: list[str]) -> str:
    return "|".join(re.escape(n) for n in names)


_GRAMMAR = re.compile(
    rf"^(?:{_alt(sorted(OPS, key=len, reverse=True))}"
    rf"|(?:{_alt(_WINDOWED)})_(?P<n>[1-9][0-9]{{0,3}})(?:_(?:{_alt(_COLUMNS)}))?"
    rf"|(?:{_alt(_PLAIN)})_(?:{_alt(_COLUMNS)}))$"
)
FEATURE_RULE = (
    "each item is a DSL feature name: a data column ("
    + ", ".join(sorted(_COLUMNS))
    + "), an operator name, `<op>_<window>` or `<op>_<window>_<column>` for windowed operators ("
    + ", ".join(sorted(_WINDOWED))
    + f"; window = whole bars 1-{MAX_WINDOW}, 1 bar = 1 hour, no unit suffixes like h or d), or "
    "`<op>_<column>` for cross-section / regime operators ("
    + ", ".join(sorted(_PLAIN))
    + "); lowercase, starts with a letter, at most 32 characters. Examples: ema_50_close, ret_24, std_168, "
    "atr_14, rank_xs_close, funding_rate. Use [] when no extra feature is needed."
)


def feature_problem(name: str) -> str | None:
    """Why `name` is not a DSL feature name (None when it is one)."""
    if not re.fullmatch(FEATURE_NAME, name):
        return f"{name!r} does not match {FEATURE_NAME}"
    m = _GRAMMAR.fullmatch(name)
    if m is None:
        return f"{name!r} is not a DSL feature"
    n = m.group("n")
    if n is not None and int(n) > MAX_WINDOW:
        return f"{name!r}: window {n} exceeds {MAX_WINDOW} bars"
    return None


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class HypothesisDraft(_Strict):
    family: Family
    statement: str = Field(min_length=10, max_length=600)
    falsification_test: str = Field(min_length=10, max_length=600)
    required_data: list[DataKind] = Field(min_length=1, max_length=4)
    required_features: list[str] = Field(default_factory=lambda: list[str](), max_length=8, description=FEATURE_RULE)
    prior: float = Field(ge=0.0, le=1.0)

    @field_validator("required_features")
    @classmethod
    def _names(cls, v: list[str]) -> list[str]:
        bad = [p for p in (feature_problem(x) for x in v) if p is not None]
        if bad:
            raise ValueError(f"not DSL feature names: {'; '.join(bad)}. Rule: {FEATURE_RULE}")
        return v


class PlanDraft(_Strict):
    template: Family
    variants: list[int] = Field(min_length=1, max_length=3)  # indices into the template's fixed grid
    rationale: str = Field(min_length=10, max_length=600)

    @field_validator("variants")
    @classmethod
    def _unique(cls, v: list[int]) -> list[int]:
        if len(set(v)) != len(v) or any(i < 0 for i in v):
            raise ValueError("variants must be distinct non-negative grid indices")
        return v


class AnalysisDraft(_Strict):
    interpretation: str = Field(min_length=10, max_length=1_000)
    agrees_with_causes: bool
    suggested_next_family: Family | None = None
    notes: str = Field(default="", max_length=600)


SCHEMAS: dict[str, type[_Strict]] = {"hypothesize": HypothesisDraft, "plan": PlanDraft, "analyze": AnalysisDraft}
Purpose = Literal["hypothesize", "plan", "analyze"]


# Structured-output schema for the model (both adapters). Value constraints are not sent as JSON-schema
# keywords (structured outputs do not take them all); F-016: they are restated as text in `description` and in
# `constraint_notes`, so the model knows them. Local pydantic validation stays the authority.
_CONSTRAINTS = (
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "pattern",
    "minItems",
    "maxItems",
)
_DROP = {*_CONSTRAINTS, "title", "default"}
Json = dict[str, object]


def _num(v: object) -> object:
    return int(v) if isinstance(v, float) and v.is_integer() else v


def _span(lo: object, hi: object, unit: str) -> str | None:
    lo, hi = _num(lo), _num(hi)
    if lo is not None and hi is not None:
        return f"{lo}-{hi}{unit}"
    if hi is not None:
        return f"at most {hi}{unit}"
    if lo is not None:
        return f"at least {lo}{unit}"
    return None


def _limits(node: Mapping[str, object]) -> list[str]:
    out = [
        s
        for s in (
            _span(node.get("minLength"), node.get("maxLength"), " characters"),
            _span(node.get("minimum"), node.get("maximum"), " (inclusive)"),
            _span(node.get("minItems"), node.get("maxItems"), " items"),
        )
        if s is not None
    ]
    if "exclusiveMinimum" in node or "exclusiveMaximum" in node:
        out.append(f"exclusive range {node.get('exclusiveMinimum')}..{node.get('exclusiveMaximum')}")
    if "pattern" in node:
        out.append(f"must match {node['pattern']}")
    return out


def api_schema(schema: Mapping[str, object]) -> Json:
    """Pydantic JSON schema -> structured-output schema: constraint keywords become description text,
    `additionalProperties: false` on every object, all properties required."""

    def walk(node: object) -> object:
        if isinstance(node, dict):
            src = cast(Json, node)
            d: Json = {str(k): walk(v) for k, v in src.items() if k not in _DROP}
            limits = _limits(src)
            if limits:
                note = "Constraints (checked locally; a violating answer is rejected): " + "; ".join(limits) + "."
                d["description"] = f"{d['description']} {note}" if d.get("description") else note
            if d.get("type") == "object" and isinstance(d.get("properties"), dict):
                d["additionalProperties"] = False
                d["required"] = sorted(cast(Json, d["properties"]))
            return d
        if isinstance(node, list):
            return [walk(v) for v in cast(list[object], node)]
        return node

    return cast(Json, walk(dict(schema)))


def _describe(node: Mapping[str, object]) -> str:
    if "anyOf" in node:
        parts = [_describe(cast(Json, n)) for n in cast(list[object], node["anyOf"])]
        return " or ".join(parts)
    if "enum" in node:
        kind = "one of " + ", ".join(str(v) for v in cast(list[object], node["enum"]))
    elif node.get("type") == "array":
        kind = f"array of ({_describe(cast(Json, node.get('items', {})))})"
    else:
        kind = str(node.get("type", "value"))
    limits = _limits(node)
    return kind + (f", {', '.join(limits)}" if limits else "")


def constraint_notes(model: type[BaseModel]) -> str:
    """Every field of the answer with its type, allowed values and limits, as plain text for the prompt."""
    schema = model.model_json_schema()
    props = cast(dict[str, Json], schema.get("properties", {}))
    lines = [
        "Answer constraints (validated locally; a violating answer is rejected and asked again). "
        "Return exactly these fields, all of them, and no others:"
    ]
    for name, node in props.items():
        extra = f" Rule: {node['description']}" if node.get("description") else ""
        lines.append(f"- {name}: {_describe(node)}.{extra}")
    return "\n".join(lines)


def validation_feedback(error: ValidationError) -> list[dict[str, str]]:
    """Field-by-field problems of a rejected answer, without echoing the answer's text back."""
    out: list[dict[str, str]] = []
    for e in error.errors(include_input=False, include_url=False):
        field = ".".join(str(p) for p in e["loc"]) or "(answer)"
        out.append({"field": field, "problem": str(e["msg"])})
    return out
