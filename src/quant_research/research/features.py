"""Phase 4 interface designed in Phase 3 (ADR-023): IC feature evaluation for S1 candidate screening.

Only the contract lives here. Phase 4 implements `FeatureEvaluator` (each evaluation is a trial, design §8)
and writes `feature_eval` rows; Phase 3 hypotheses already name `required_features` and memory findings
already cite evidence as `trial:<id>` / `feature_eval:<id>` strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from quant_research.backtest.panel import Panel
from quant_research.strategy.spec import Node

EvidenceKind = Literal["trial", "feature_eval", "llm_call", "transition"]


def evidence_ref(kind: EvidenceKind, row_id: int) -> str:
    if row_id <= 0:
        raise ValueError(f"evidence id must be positive, got {row_id}")
    return f"{kind}:{row_id}"


@dataclass(frozen=True, slots=True)
class FeatureEvalRecord:
    feature: Node
    horizon_bars: int
    ic: float | None
    ic_hac_t: float | None
    samples: int
    data_version: str


class FeatureEvaluator(Protocol):
    """Phase 4 S1: information coefficient of a point-in-time feature against forward returns."""

    def evaluate(self, feature: Node, horizon_bars: int, panel: Panel) -> FeatureEvalRecord: ...
