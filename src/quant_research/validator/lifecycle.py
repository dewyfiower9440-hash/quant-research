"""Strategy lifecycle and pre-registered degradation rules (Phase 5 step 11; design v2.1 §9-10). Pure core +
a thin recorder on validator.db.

States: Candidate -> Validated -> Paper -> Approved -> Active -> Degraded -> Suspended -> Retired, with the
return path Degraded/Suspended -> Validated only through the validator again (and counted as a trial by the
caller). Degradation rules are pre-registered here so the paper / live monitors cannot tune them later:
  - one-sided CUSUM of realised vs expected daily pnl (standardised), alarm at `cusum_h`;
  - realised slippage / modelled slippage ratio above `slippage_ratio_max` over a window;
  - share of decision intervals with data gaps above `gap_ratio_max`."""

from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from typing import Literal, cast

import numpy as np
from numpy.typing import NDArray

from quant_research.errors import AppError
from quant_research.validator.store import rule_hash

F64 = NDArray[np.float64]
StrategyState = Literal["Candidate", "Validated", "Paper", "Approved", "Active", "Degraded", "Suspended", "Retired"]
STATES: tuple[StrategyState, ...] = (
    "Candidate",
    "Validated",
    "Paper",
    "Approved",
    "Active",
    "Degraded",
    "Suspended",
    "Retired",
)
# (from, to): who may sign it. The validator alone promotes into Validated; the user alone approves for live.
TRANSITIONS: dict[tuple[StrategyState | None, StrategyState], frozenset[str]] = {
    (None, "Candidate"): frozenset({"research"}),
    ("Candidate", "Validated"): frozenset({"validator"}),
    ("Candidate", "Retired"): frozenset({"validator", "user"}),
    ("Validated", "Paper"): frozenset({"paper"}),
    ("Validated", "Retired"): frozenset({"user"}),
    ("Paper", "Approved"): frozenset({"user"}),
    ("Paper", "Degraded"): frozenset({"paper"}),
    ("Paper", "Retired"): frozenset({"user"}),
    ("Approved", "Active"): frozenset({"user"}),
    ("Approved", "Retired"): frozenset({"user"}),
    ("Active", "Degraded"): frozenset({"risk", "paper"}),
    ("Active", "Suspended"): frozenset({"risk", "user"}),
    ("Active", "Retired"): frozenset({"user"}),
    ("Degraded", "Suspended"): frozenset({"risk", "user"}),
    ("Degraded", "Validated"): frozenset({"validator"}),  # back only through the validator (a new trial)
    ("Degraded", "Retired"): frozenset({"user"}),
    ("Suspended", "Validated"): frozenset({"validator"}),
    ("Suspended", "Retired"): frozenset({"user"}),
}


class IllegalLifecycleTransition(AppError):
    """A lifecycle move not in the pre-registered table, or signed by the wrong party."""


def check_transition(src: StrategyState | None, dst: StrategyState, signer: str) -> None:
    allowed = TRANSITIONS.get((src, dst))
    if allowed is None:
        raise IllegalLifecycleTransition(f"{src} -> {dst} is not a lifecycle transition")
    role = signer.split(":", 1)[0]
    if role not in allowed:
        raise IllegalLifecycleTransition(f"{src} -> {dst} must be signed by {sorted(allowed)}, not {signer!r}")


# ------------------------------------------------------------------------------------------- degradation rules
@dataclass(frozen=True, slots=True)
class DegradationRule:
    """Pre-registered (ADR): changing any value is a new registration, never a tuning of a live strategy."""

    cusum_k: float = 0.5  # allowance in standardised units (half a sigma)
    cusum_h: float = 5.0  # alarm threshold of the one-sided lower CUSUM
    slippage_ratio_max: float = 2.0  # realised / modelled slippage over the window
    gap_ratio_max: float = 0.10  # share of decision intervals without data
    window_days: int = 30

    def __post_init__(self) -> None:
        if self.cusum_k < 0 or self.cusum_h <= 0 or self.slippage_ratio_max <= 0 or not 0 < self.gap_ratio_max < 1:
            raise ValueError(f"invalid degradation rule {self}")
        if self.window_days < 1:
            raise ValueError("window_days must be >= 1")

    def hash(self) -> str:
        return rule_hash(cast(dict[str, object], asdict(self)))


def cusum_lower(
    realised: F64, expected_mean: float, expected_sd: float, rule: DegradationRule
) -> tuple[F64, int | None]:
    """One-sided lower CUSUM of standardised shortfall; returns the path and the first alarm index (or None)."""
    if expected_sd <= 0:
        raise ValueError("expected_sd must be > 0")
    z = (np.asarray(realised, dtype=np.float64) - expected_mean) / expected_sd
    s = np.zeros(z.shape[0])
    run = 0.0
    alarm: int | None = None
    for i, v in enumerate(z):
        run = max(0.0, run - float(v) - rule.cusum_k)  # accumulates when realised < expected - k sigma
        s[i] = run
        if alarm is None and run > rule.cusum_h:
            alarm = i
    return s, alarm


def slippage_degraded(
    realised_bps: Sequence[float], modelled_bps: Sequence[float], rule: DegradationRule
) -> tuple[bool, float]:
    r, m = (
        float(np.sum(np.asarray(realised_bps, dtype=np.float64))),
        float(np.sum(np.asarray(modelled_bps, dtype=np.float64))),
    )
    if m <= 0:
        return False, math.nan
    ratio = r / m
    return ratio > rule.slippage_ratio_max, ratio


def gap_degraded(intervals_total: int, intervals_with_gap: int, rule: DegradationRule) -> tuple[bool, float]:
    if intervals_total <= 0:
        return False, math.nan
    ratio = intervals_with_gap / intervals_total
    return ratio > rule.gap_ratio_max, ratio


# ------------------------------------------------------------------------------------------- recorder (validator.db)
@dataclass(frozen=True, slots=True)
class Transition:
    spec_hash: str
    from_state: StrategyState | None
    to_state: StrategyState
    reason: str
    evidence_ref: str
    signer: str


class LifecycleLedger:
    """State table + append-only transition log in validator.db (one writer: the validator / paper process)."""

    def __init__(self, conn: sqlite3.Connection, *, now_ns: Callable[[], int] = time.time_ns) -> None:
        self.conn = conn
        self.now_ns = now_ns

    def state(self, spec_hash: str) -> StrategyState | None:
        row = self.conn.execute("select state from strategy_state where spec_hash = ?", (spec_hash,)).fetchone()
        return None if row is None else cast(StrategyState, str(row[0]))

    def move(self, t: Transition) -> int:
        current = self.state(t.spec_hash)
        if current != t.from_state:
            raise IllegalLifecycleTransition(f"{t.spec_hash}: current state is {current}, not {t.from_state}")
        check_transition(t.from_state, t.to_state, t.signer)
        if not t.evidence_ref:
            raise IllegalLifecycleTransition("a transition needs an evidence reference")
        now = self.now_ns()
        self.conn.execute("begin immediate")
        try:
            cur = self.conn.execute(
                "insert into strategy_transition(spec_hash, from_state, to_state, reason, evidence_ref, signer, at_ns) "
                "values (?,?,?,?,?,?,?)",
                (t.spec_hash, t.from_state, t.to_state, t.reason, t.evidence_ref, t.signer, now),
            )
            self.conn.execute(
                "insert into strategy_state(spec_hash, state, updated_ns) values (?,?,?) "
                "on conflict(spec_hash) do update set state = excluded.state, updated_ns = excluded.updated_ns",
                (t.spec_hash, t.to_state, now),
            )
            self.conn.execute("commit")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("rollback")
            raise
        return int(cur.lastrowid or 0)

    def history(self, spec_hash: str) -> list[tuple[StrategyState | None, StrategyState, str, str]]:
        rows = self.conn.execute(
            "select from_state, to_state, reason, signer from strategy_transition where spec_hash = ? "
            "order by transition_id",
            (spec_hash,),
        ).fetchall()
        return [
            (
                None if r[0] is None else cast(StrategyState, str(r[0])),
                cast(StrategyState, str(r[1])),
                str(r[2]),
                str(r[3]),
            )
            for r in rows
        ]
