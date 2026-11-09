"""Rule-based failure analysis (design v2.1 §6 step 1: eight cause codes). Pure.

Rules are Phase 3 priors (CLAUDE.md §9: re-tune from recorded outcomes). They read the metrics of the
selection-split screening runs (S2) and the confirmation-split run (S3) of one cycle and return every cause
that applies, each with its evidence (trial ids and the numbers used). The LLM may interpret, never override:
the policy acts on these codes.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

from quant_research.research.memory import CauseCode

CAPITAL = 10_000.0
BASE_COST_RATE = 0.0008  # fee + half spread prior per traded notional (ADR-021): above 2x -> impact dominates


@dataclass(frozen=True, slots=True)
class RunView:
    """What the classifier needs from one evaluated trial."""

    trial_id: int
    status: str  # done | rejected | failed | aborted
    reason: str | None = None
    metrics: Mapping[str, object] = field(default_factory=lambda: dict[str, object]())
    daily: Sequence[float] = ()


@dataclass(frozen=True, slots=True)
class Cause:
    code: CauseCode
    detail: str
    evidence: Mapping[str, object]


def _f(m: Mapping[str, object], key: str) -> float:
    v = m.get(key)
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(float(v)) else math.nan


def _gross(m: Mapping[str, object]) -> float:
    """Return before trading costs (funding stays in: it is a cash flow, not an execution cost)."""
    return _f(m, "total_return") + _f(m, "costs") / CAPITAL


def _regime_flip(daily: Sequence[float]) -> bool:
    n = len(daily)
    if n < 20:
        return False
    half = n // 2
    a, b = daily[:half], daily[half:]

    def stats(x: Sequence[float]) -> tuple[float, float]:
        mean = sum(x) / len(x)
        var = sum((v - mean) ** 2 for v in x) / max(1, len(x) - 1)
        return mean, math.sqrt(var / len(x))

    (ma, sa), (mb, sb) = stats(a), stats(b)
    return ma * mb < 0 and abs(ma) > 0.5 * sa and abs(mb) > 0.5 * sb


def classify(
    selection: Sequence[RunView],
    confirmation: RunView | None,
    *,
    data_missing: Sequence[str] = (),
    named_vs_anonymized_gap: float | None = None,
) -> list[Cause]:
    causes: list[Cause] = []
    runs = [*selection, *([confirmation] if confirmation else [])]
    ids = [r.trial_id for r in runs]
    flags: list[str] = []
    for r in runs:
        raw = r.metrics.get("flags")
        if isinstance(raw, list):
            flags += [str(f) for f in cast(list[object], raw)]
    if data_missing or any("not_modelled" in f for f in flags):
        causes.append(
            Cause(
                "missing_data",
                "required data is not available or not modelled",
                {"trials": ids, "needs": list(data_missing), "flags": sorted(set(flags))},
            )
        )
    done = [r for r in selection if r.status == "done"]
    refused = [r for r in selection if r.status == "rejected"]
    if selection and not done:
        causes.append(
            Cause(
                "infeasible",
                "no variant could be evaluated (all refused: S0 / fidelity / engine rules)",
                {"trials": [r.trial_id for r in refused], "reasons": [r.reason for r in refused]},
            )
        )
        return causes
    if not done:
        return causes
    best = max(
        done, key=lambda r: _f(r.metrics, "sr_daily") if not math.isnan(_f(r.metrics, "sr_daily")) else -math.inf
    )
    view = confirmation if confirmation is not None and confirmation.status == "done" else best
    trades = _f(view.metrics, "trades")
    net, gross = _f(view.metrics, "total_return"), _gross(view.metrics)
    ev = {"trial": view.trial_id, "net": net, "gross": gross, "trades": trades}
    if trades == 0 or (not math.isnan(gross) and gross <= 0):
        causes.append(Cause("weak_signal", "no edge before costs (gross return <= 0 or no trades)", ev))
    elif not math.isnan(net) and net <= 0 < gross:
        causes.append(Cause("cost_erosion", "positive before costs, not after", ev))
    turnover, costs = _f(view.metrics, "turnover"), _f(view.metrics, "costs")
    if turnover > 0 and costs / (turnover * CAPITAL) > 2 * BASE_COST_RATE:
        causes.append(
            Cause(
                "capacity",
                "impact dominates the cost per traded notional",
                {**ev, "cost_rate": costs / (turnover * CAPITAL)},
            )
        )
    sel_sr = _f(best.metrics, "sr_daily")
    if confirmation is not None and confirmation.status == "done":
        conf_sr = _f(confirmation.metrics, "sr_daily")
        if sel_sr > 0 and not conf_sr > 0:
            causes.append(
                Cause(
                    "overfit",
                    "selection edge does not hold on the confirmation split",
                    {
                        "selection_trial": best.trial_id,
                        "confirmation_trial": confirmation.trial_id,
                        "sr_selection": sel_sr,
                        "sr_confirmation": conf_sr,
                    },
                )
            )
        if _regime_flip(confirmation.daily):
            causes.append(
                Cause(
                    "regime_dependent",
                    "daily pnl changes sign between the two halves",
                    {"trial": confirmation.trial_id},
                )
            )
    if named_vs_anonymized_gap is not None and abs(named_vs_anonymized_gap) > 0.5:
        causes.append(
            Cause(
                "prior_knowledge",
                "results differ between named and anonymized prompts",
                {"gap": named_vs_anonymized_gap},
            )
        )
    return causes
