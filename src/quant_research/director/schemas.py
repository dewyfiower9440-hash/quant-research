"""Structured LLM outputs of the Director (design v2.1 §4 A: Hypothesis / ExperimentPlan / analysis).

Everything an LLM returns is untrusted data: it is parsed into these models (closed vocabularies, length
caps, no extra fields) and only the parsed values are used. Free text is stored and may be shown again as
quoted data, never executed as an instruction (§6-5, §15 prompt injection).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Family = Literal["trend", "mean_reversion", "momentum", "funding_basis", "volatility"]
DataKind = Literal["klines", "mark", "funding", "premium"]
FEATURE_NAME = r"^[a-z_]{1,32}$"  # operator / feature identifiers only (no prose, no paths)


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class HypothesisDraft(_Strict):
    family: Family
    statement: str = Field(min_length=10, max_length=600)
    falsification_test: str = Field(min_length=10, max_length=600)
    required_data: list[DataKind] = Field(min_length=1, max_length=4)
    required_features: list[str] = Field(default_factory=lambda: list[str](), max_length=8)
    prior: float = Field(ge=0.0, le=1.0)

    @field_validator("required_features")
    @classmethod
    def _names(cls, v: list[str]) -> list[str]:
        import re

        bad = [x for x in v if not re.fullmatch(FEATURE_NAME, x)]
        if bad:
            raise ValueError(f"feature names must match {FEATURE_NAME}: {bad}")
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
