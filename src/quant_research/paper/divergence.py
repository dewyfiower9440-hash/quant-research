"""Backtest-vs-paper divergence decomposition (Phase 6 step 15; design v2.1 §9-8). Pure.

For every decision the backtest assumed a target weight and a fill at a reference price (the next bar open);
paper trading produced an actual target (possibly different because of data timing), an arrival price
(after latency), fills at the quotes (spread, partial fills). The pnl gap per decision splits into:
  signal   = (w_paper - w_backtest) * r_ref            weight difference at the reference price
  latency  = w_paper * (p_ref - p_arrival) / p_ref       price moved between decision and arrival
  spread   = -|w_paper| * (vwap - p_arrival) / p_arrival * sign   execution cost versus the arrival mid
  partial  = (filled_fraction - 1) * w_paper * r_ref     exposure not taken because the order did not fill
  residual = total - sum(components)
Signs are from the long/short perspective (sign = +1 buy, -1 sell).

Only pre-seal summaries may reach the research side (`pre_seal_summary`): rows at or after the seal
boundary are dropped before anything is handed back (§9 "모의매매 결과는 봉인 경계 이전 데이터로만")."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    decision_ns: int
    symbol: str
    w_backtest: float  # target weight the backtest would hold
    w_paper: float  # target weight the paper engine computed
    p_ref: float  # backtest fill reference (next bar open)
    p_arrival: float  # mid at order arrival (decision + latency)
    vwap: float | None  # paper fill price (None: nothing filled)
    filled_fraction: float  # 0..1 of the intended change that filled
    r_ref: float  # realised return of the interval at the reference price
    backtest_pnl: float
    paper_pnl: float


@dataclass(frozen=True, slots=True)
class Divergence:
    signal: float = 0.0
    latency: float = 0.0
    spread: float = 0.0
    partial: float = 0.0
    residual: float = 0.0
    total: float = 0.0
    n: int = 0
    by_symbol: dict[str, float] = field(default_factory=lambda: dict[str, float]())

    def shares(self) -> dict[str, float]:
        denom = abs(self.signal) + abs(self.latency) + abs(self.spread) + abs(self.partial) + abs(self.residual)
        if denom == 0:
            return {k: 0.0 for k in ("signal", "latency", "spread", "partial", "residual")}
        return {
            "signal": abs(self.signal) / denom,
            "latency": abs(self.latency) / denom,
            "spread": abs(self.spread) / denom,
            "partial": abs(self.partial) / denom,
            "residual": abs(self.residual) / denom,
        }


def decompose(records: Sequence[DecisionRecord]) -> Divergence:
    sig = lat = spr = par = tot = 0.0
    by_symbol: dict[str, float] = {}
    for r in records:
        if r.p_ref <= 0 or r.p_arrival <= 0:
            raise ValueError(f"{r.symbol}@{r.decision_ns}: prices must be > 0")
        sign = 1.0 if r.w_paper >= 0 else -1.0
        sig += (r.w_paper - r.w_backtest) * r.r_ref
        lat += r.w_paper * (r.p_ref - r.p_arrival) / r.p_ref
        if r.vwap is not None and r.filled_fraction > 0:
            spr += -abs(r.w_paper) * r.filled_fraction * sign * (r.vwap - r.p_arrival) / r.p_arrival
        par += (r.filled_fraction - 1.0) * r.w_paper * r.r_ref
        gap = r.paper_pnl - r.backtest_pnl
        tot += gap
        by_symbol[r.symbol] = by_symbol.get(r.symbol, 0.0) + gap
    residual = tot - (sig + lat + spr + par)
    return Divergence(sig, lat, spr, par, residual, tot, len(records), by_symbol)


def pre_seal_summary(records: Sequence[DecisionRecord], seal_boundary_ns: int) -> dict[str, object]:
    """What research is allowed to see: aggregates over decisions strictly before the seal boundary."""
    kept = [r for r in records if r.decision_ns < seal_boundary_ns]
    d = decompose(kept)
    return {
        "decisions": d.n,
        "dropped_post_seal": len(records) - len(kept),
        "total_gap": d.total,
        "shares": d.shares(),
        "fill_rate": (sum(r.filled_fraction for r in kept) / len(kept)) if kept else None,
    }
