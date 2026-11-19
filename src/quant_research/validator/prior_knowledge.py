"""LLM prior-knowledge checks (Phase 5 step 11; design v2.1 §9-6, §8 "model_id and LLM cutoff on every trial").

- `final_judgement_from`: the final one-shot judgement window must start AFTER the latest training cutoff of
  every model that touched the candidate (read from the trials' `llm_cutoff`); None when a model's cutoff is
  unknown (then no post-cutoff window can be claimed and the report says so).
- `anonymization_difference`: performance of trials whose hypothesis was produced with anonymized context
  versus real names (the §9-6 measurement arm, fake LLM only). Reported, never used for selection."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

import numpy as np

from quant_research.research.ledger import TrialLedger


def parse_cutoff(text: str | None) -> date | None:
    """ISO date ('2025-03-01') or month ('2025-03'); None when unknown."""
    if not text:
        return None
    try:
        if len(text) == 7:
            return datetime.strptime(text, "%Y-%m").replace(tzinfo=UTC).date()
        return date.fromisoformat(text)
    except ValueError:
        return None


def final_judgement_from(cutoffs: Iterable[str | None]) -> date | None:
    """Earliest allowed start of the final judgement window: the day after the latest known cutoff. None if
    any model has an unknown cutoff (nothing can be claimed about post-cutoff data)."""
    latest: date | None = None
    for raw in cutoffs:
        d = parse_cutoff(raw)
        if d is None:
            return None
        latest = d if latest is None or d > latest else latest
    return None if latest is None else date.fromordinal(latest.toordinal() + 1)


def cutoffs_of(ledger: TrialLedger, trial_ids: Sequence[int]) -> list[str | None]:
    if not trial_ids:
        return []
    rows = ledger.store.conn.execute(
        "select distinct llm_cutoff from trial where trial_id in (select value from json_each(?))",
        (json.dumps([int(t) for t in trial_ids]),),
    ).fetchall()
    return [None if r[0] is None else str(r[0]) for r in rows]


@dataclass(frozen=True, slots=True)
class AnonymizationDifference:
    metric: str
    n_anonymized: int
    n_real: int
    mean_anonymized: float
    mean_real: float

    @property
    def difference(self) -> float:
        return self.mean_real - self.mean_anonymized  # > 0: real names helped (a prior-knowledge warning sign)


def anonymization_difference(
    ledger: TrialLedger, *, stage: str = "S3", metric: str = "sr_daily"
) -> AnonymizationDifference:
    """Mean `metric` of done trials of `stage`, split by the hypothesis' anonymized flag (cycle join)."""
    rows = ledger.store.conn.execute(
        "select h.anonymized, t.metrics_json from trial t join hypothesis h on h.hypothesis_id = t.hypothesis_id "
        "where t.stage = ? and t.status = 'done'",
        (stage,),
    ).fetchall()
    anon: list[float] = []
    real: list[float] = []
    for flag, m in rows:
        v = json.loads(str(m)).get(metric) if m else None
        if isinstance(v, int | float) and math.isfinite(float(v)):
            (anon if int(flag) == 1 else real).append(float(v))
    return AnonymizationDifference(metric, len(anon), len(real), _mean(anon), _mean(real))


def _mean(xs: Sequence[float]) -> float:
    return float(np.mean(np.asarray(xs, dtype=np.float64))) if xs else math.nan
