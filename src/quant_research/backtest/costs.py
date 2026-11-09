"""Cost and fill model priors (Phase 2 step 5; design v2.1 §9). Pure functions.

Defaults are conservative priors, not calibrated values (ADR-021):
- fees: Binance USD-M VIP0 taker 0.05% / maker 0.02% (Binance FAQ example; official table 미확인),
  Binance spot 0.1%, Upbit KRW 0.05% (design §9);
- spread: measured from captured bookTicker where available (7-day validation pending, OPS-007), otherwise
  Corwin-Schultz or Abdi-Ranaldo estimates from bars (not calibrated: pre-2024 bookTicker bulk deferred);
- impact: square-root law Y * sigma_daily * sqrt(Q / ADV) with Y = 0.7 (literature 0.5..1, 미확인) and a
  stress factor k (cannot be calibrated without meta-order data, §9);
- liquidation: isolated margin, maintenance margin rate and liquidation fee priors (real tiers need keys,
  Phase 6).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    taker: float
    maker: float


FEES: dict[tuple[str, str], FeeSchedule] = {
    ("binance", "usdm_futures"): FeeSchedule(taker=0.0005, maker=0.0002),
    ("binance", "spot"): FeeSchedule(taker=0.001, maker=0.001),
    ("upbit", "spot"): FeeSchedule(taker=0.0005, maker=0.0005),
}


@dataclass(frozen=True, slots=True)
class CostModel:
    fee: float = 0.0005
    half_spread: float = 0.0003  # 3 bps prior when no estimate is supplied
    impact_y: float = 0.7
    impact_k: float = 1.0
    stress: float = 1.0  # x1, x2, x3 stress runs (§9-5)
    maintenance_margin: float = 0.01  # conservative prior (BTC tier 1 is lower; small caps higher)
    liquidation_fee: float = 0.005  # prior, fraction of notional

    def rate(self, notional: float, sigma_daily: float | None, adv: float | None) -> float:
        """Cost of a trade as a fraction of its notional (fee + half spread + impact), times the stress."""
        impact = 0.0
        if sigma_daily is not None and adv is not None and adv > 0 and notional > 0:
            impact = self.impact_y * self.impact_k * sigma_daily * math.sqrt(notional / adv)
        return self.stress * (self.fee + self.half_spread + impact)

    def stressed(self, factor: float) -> CostModel:
        return CostModel(
            self.fee,
            self.half_spread,
            self.impact_y,
            self.impact_k,
            self.stress * factor,
            self.maintenance_margin,
            self.liquidation_fee,
        )


def corwin_schultz(high1: float, low1: float, high2: float, low2: float) -> float:
    """Full relative spread from two consecutive bars' high / low (Corwin & Schultz 2012); negatives -> 0."""
    beta = math.log(high1 / low1) ** 2 + math.log(high2 / low2) ** 2
    gamma = math.log(max(high1, high2) / min(low1, low2)) ** 2
    denom = 3 - 2 * math.sqrt(2)
    alpha = (math.sqrt(2 * beta) - math.sqrt(beta)) / denom - math.sqrt(gamma / denom)
    spread = 2 * (math.exp(alpha) - 1) / (1 + math.exp(alpha))
    return max(0.0, spread)


def abdi_ranaldo(closes: Sequence[float], highs: Sequence[float], lows: Sequence[float]) -> float:
    """Full relative spread from close and high-low midpoints (Abdi & Ranaldo 2017), log prices;
    S^2 = 4 * mean((c_t - eta_t)(c_t - eta_{t+1})), negative means -> 0."""
    if not len(closes) == len(highs) == len(lows) or len(closes) < 2:
        raise ValueError("abdi_ranaldo needs >= 2 bars of equal-length close/high/low")
    c = [math.log(x) for x in closes]
    eta = [(math.log(h) + math.log(lo)) / 2 for h, lo in zip(highs, lows, strict=True)]
    terms = [(c[t] - eta[t]) * (c[t] - eta[t + 1]) for t in range(len(c) - 1)]
    s2 = 4 * sum(terms) / len(terms)
    return math.sqrt(s2) if s2 > 0 else 0.0


def measured_half_spread(bids: Sequence[float], asks: Sequence[float]) -> float:
    """Median relative half spread from quote samples (captured bookTicker)."""
    rel = sorted((a - b) / ((a + b) / 2) / 2 for b, a in zip(bids, asks, strict=True) if a >= b > 0)
    if not rel:
        raise ValueError("no valid bid/ask pairs")
    mid = len(rel) // 2
    return rel[mid] if len(rel) % 2 else (rel[mid - 1] + rel[mid]) / 2


def funding_payment(quantity: float, mark: float, rate: float) -> float:
    """Cash flow at a settlement: longs (quantity > 0) pay a positive rate, shorts receive it (§9)."""
    return -quantity * mark * rate


def liquidation_price(entry: float, side: int, leverage: float, maintenance_margin: float) -> float:
    """Isolated margin: the position is liquidated when the loss eats the margin down to maintenance."""
    if side not in (1, -1) or leverage <= 0:
        raise ValueError(f"side must be +1/-1 and leverage > 0, got {side}, {leverage}")
    return entry * (1 - side * (1 / leverage - maintenance_margin))
