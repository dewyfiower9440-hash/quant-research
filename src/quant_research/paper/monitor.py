"""Paper-run degradation monitoring and lifecycle moves (Phase 6 step 15; design v2.1 §9-10). Pure decision
+ thin recorder: the pre-registered `DegradationRule` is evaluated on realised daily pnl, slippage and gap
ratios; an alarm moves the strategy Paper/Active -> Degraded in the lifecycle ledger (signer 'paper'),
and nothing here can move a strategy back towards Active (only the validator can, as a new trial)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from quant_research.validator.lifecycle import (
    DegradationRule,
    LifecycleLedger,
    StrategyState,
    Transition,
    cusum_lower,
    gap_degraded,
    slippage_degraded,
)


@dataclass(frozen=True, slots=True)
class Health:
    cusum_alarm_index: int | None
    slippage_ratio: float
    gap_ratio: float
    degraded: bool
    reasons: tuple[str, ...]


def assess(
    realised_daily: Sequence[float] | NDArray[np.float64],
    expected_mean: float,
    expected_sd: float,
    realised_slippage_bps: Sequence[float],
    modelled_slippage_bps: Sequence[float],
    intervals_total: int,
    intervals_with_gap: int,
    rule: DegradationRule,
) -> Health:
    reasons: list[str] = []
    alarm: int | None = None
    if len(realised_daily) >= 2 and expected_sd > 0:
        _, alarm = cusum_lower(np.asarray(realised_daily, dtype=np.float64), expected_mean, expected_sd, rule)
        if alarm is not None:
            reasons.append(f"CUSUM shortfall alarm on day {alarm}")
    slip_bad, slip_ratio = slippage_degraded(realised_slippage_bps, modelled_slippage_bps, rule)
    if slip_bad:
        reasons.append(f"realised/modelled slippage {slip_ratio:.2f} > {rule.slippage_ratio_max}")
    gap_bad, gap_ratio = gap_degraded(intervals_total, intervals_with_gap, rule)
    if gap_bad:
        reasons.append(f"gap ratio {gap_ratio:.3f} > {rule.gap_ratio_max}")
    return Health(alarm, slip_ratio, gap_ratio, bool(reasons), tuple(reasons))


def apply_health(ledger: LifecycleLedger, spec_hash: str, health: Health, *, evidence_ref: str) -> StrategyState | None:
    """Degrade a Paper / Active strategy when the health says so; returns the new state (None: unchanged)."""
    if not health.degraded:
        return None
    state = ledger.state(spec_hash)
    if state not in ("Paper", "Active"):
        return None
    ledger.move(Transition(spec_hash, state, "Degraded", "; ".join(health.reasons), evidence_ref, "paper"))
    return "Degraded"
