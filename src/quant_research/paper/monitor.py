"""Paper-run degradation monitoring and lifecycle moves (Phase 6 step 15; design v2.1 §9-10). Pure decision
+ thin recorder: the pre-registered `DegradationRule` is evaluated on realised daily pnl, slippage and gap
ratios; an alarm moves the strategy Paper/Active -> Degraded in the lifecycle ledger (signer 'paper'),
and nothing here can move a strategy back towards Active (only the validator can, as a new trial)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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

DAY_NS = 86_400 * 10**9


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


@dataclass(frozen=True, slots=True)
class StrategyFill:
    at_ns: int
    symbol: str
    signed_qty: float  # + buy, - sell
    price: float
    fee: float


def daily_returns_from_fills(
    fills: Sequence[StrategyFill], closes: Mapping[int, Mapping[str, float]], *, capital: float
) -> list[float]:
    """Mark-to-market daily returns of ONE strategy. `closes`: UTC day start -> that day's closing mid per
    symbol. The FIRST day is the baseline: every fill up to its end only builds the holdings (fills before the
    window included, review 2 finding 2), and its closes value them. Each later day's return is the change in
    value of the holdings at the start of the day, plus the day's fills marked to the day's close, minus fees,
    over the strategy's capital. A symbol missing from a day's closes keeps its last known close (finding 3)."""
    if capital <= 0:
        raise ValueError(f"capital must be > 0, got {capital}")
    days = sorted(closes)
    if not days:
        return []
    ordered = sorted(fills, key=lambda f: f.at_ns)
    held: dict[str, float] = {}
    i = 0
    first_end = days[1] if len(days) > 1 else days[0] + DAY_NS
    while i < len(ordered) and ordered[i].at_ns < first_end:  # the baseline: holdings only
        held[ordered[i].symbol] = held.get(ordered[i].symbol, 0.0) + ordered[i].signed_qty
        i += 1
    last: dict[str, float] = dict(closes[days[0]])
    out: list[float] = []
    for k in range(1, len(days)):
        day = days[k]
        end = days[k + 1] if k + 1 < len(days) else day + DAY_NS
        close = {**last, **closes[day]}
        pnl = sum(q * (close[s] - last[s]) for s, q in held.items() if s in close and s in last)
        while i < len(ordered) and ordered[i].at_ns < end:
            f = ordered[i]
            pnl += f.signed_qty * (close.get(f.symbol, f.price) - f.price) - f.fee
            held[f.symbol] = held.get(f.symbol, 0.0) + f.signed_qty
            i += 1
        out.append(pnl / capital)
        last = close
    return out


def promote_validated(ledger: LifecycleLedger, spec_hashes: Sequence[str], *, evidence_ref: str) -> list[str]:
    """Validated -> Paper (signer 'paper') for the strategies the paper process starts trading; returns the
    moved ones. A strategy already in Paper is left alone."""
    moved: list[str] = []
    for h in spec_hashes:
        if ledger.state(h) == "Validated":
            ledger.move(Transition(h, "Validated", "Paper", "paper trading started", evidence_ref, "paper"))
            moved.append(h)
    return moved


def check_paper_health(
    ledger: LifecycleLedger,
    spec_hash: str,
    daily_returns: Sequence[float],
    *,
    expected: tuple[float, float] | None,
    evidence_ref: str,
    rule: DegradationRule | None = None,
) -> StrategyState | None:
    """The daily degradation check of one Paper strategy (review finding 11: it was never called). Without an
    expected (mean, sd) of the daily returns nothing is claimed. Slippage and gap ratios are not yet recorded
    per strategy (empty: those rules cannot fire here)."""
    if expected is None:
        return None
    mean, sd = expected
    health = assess(daily_returns, mean, sd, [], [], 0, 0, rule or DegradationRule())
    return apply_health(ledger, spec_hash, health, evidence_ref=evidence_ref)
