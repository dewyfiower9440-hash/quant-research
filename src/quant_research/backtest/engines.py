"""Backtest engines (Phase 2 steps 6-7; design v2.1 module G: vectorised for screening, event for reference).

Shared conventions (both engines):
- equity[t] is marked at decision t's fill prices BEFORE trading; at the last decision everything is
  closed and equity[-1] is the value after that;
- a target weight w (fraction of equity) becomes a quantity q = w * equity[t] / price[t];
- trading costs = rate(notional) * |traded notional| (fee + half spread + impact, `market.trade_rate`);
- funding: every settlement inside interval t pays -q * mark * rate (longs pay positive rates);
- missing data: no orders where `tradable` is False; the position is simply held (§9 "거래 금지").
The event engine keeps quantities and cash per symbol and also handles path-dependent exits (time stop,
ATR stop, liquidation on the interval's mark high / low). The vector engine keeps weights, supports only
stateless rules, and flags (does not model) intervals in which a liquidation would have been possible.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt

from quant_research.backtest.costs import CostModel, funding_payment, liquidation_price
from quant_research.backtest.market import F64, Fidelity, Market, Targets, trade_rate
from quant_research.errors import AppError


class EngineUnsupported(AppError):
    """The vector engine cannot evaluate path-dependent rules; use the event engine."""


@dataclass
class EngineResult:
    equity: F64  # (T,)
    costs: float = 0.0
    funding: float = 0.0
    trades: int = 0
    traded_notional: float = 0.0
    position_bars: int = 0  # symbol-intervals with an open position
    entries: int = 0
    liquidations: int = 0
    stops: int = 0
    liquidation_unmodelled: bool = False
    notes: list[str] = field(default_factory=lambda: list[str]())
    # §9 worst case for missing data: per interval, the adverse range of positions held where no order was
    # possible (interval high-low over the fill price, or GAP_PRIOR when the interval has no data at all)
    gap_loss: F64 = field(default_factory=lambda: np.zeros(0))


GAP_PRIOR = 0.10  # adverse move assumed for a held position over an interval without any data


def _gap_fraction(market: Market, t: int, i: int) -> float:
    hi, lo, p = float(market.high[t, i]), float(market.low[t, i]), float(market.price[t, i])
    if math.isnan(hi) or math.isnan(lo) or not p > 0:
        return GAP_PRIOR
    return (hi - lo) / p


def _rate(cost: CostModel, market: Market, t: int, i: int, notional: float, fidelity: Fidelity) -> float:
    return trade_rate(cost, notional, float(market.sigma_daily[t, i]), float(market.adv[t, i]), fidelity)


def _liquidation_price(entry: float, side: int, weight: float, n_universe: int, cost: CostModel) -> float:
    """Isolated margin of equity / n per position: effective leverage = |weight| * n."""
    return liquidation_price(entry, side, max(abs(weight) * n_universe, 1e-12), cost.maintenance_margin)


def _check_schedule(rebalance_bars: int, min_trade_weight: float) -> None:
    if rebalance_bars < 1:
        raise ValueError(f"rebalance_bars must be >= 1, got {rebalance_bars}")
    if not 0.0 <= min_trade_weight < 1.0:
        raise ValueError(f"no-trade band min_trade_weight must be in [0, 1), got {min_trade_weight}")


def rebalance_schedule(decisions: npt.NDArray[np.int64], rebalance_bars: int) -> npt.NDArray[np.bool_]:
    """Bars on which orders may go out: (decision // bar) % k == 0, anchored to absolute time so selection,
    confirmation, holdout slices and paper rebalance on the same bars (B5 review); bar = the decision spacing."""
    n = len(decisions)
    if rebalance_bars == 1 or n < 2:
        return np.ones(n, dtype=bool)
    bar = int(np.min(np.diff(decisions)))
    if bar <= 0:
        raise ValueError("decisions must be strictly increasing")
    return np.asarray((decisions // bar) % rebalance_bars == 0, dtype=bool)


def run_vector(
    market: Market,
    targets: Targets,
    cost: CostModel,
    *,
    capital: float,
    fidelity: Fidelity,
    n_universe: int,
    stateless: bool,
    rebalance_bars: int = 1,
    min_trade_weight: float = 0.0,
) -> EngineResult:
    """`rebalance_bars` k: orders only on bars 0, k, 2k, ... (positions drift in between); `min_trade_weight`:
    a same-side resize smaller than this weight is skipped (entries, exits and flips always trade)."""
    if not stateless:
        raise EngineUnsupported("vector engine: time stop / ATR stop are path dependent (use the event engine)")
    _check_schedule(rebalance_bars, min_trade_weight)
    T, N = market.price.shape
    schedule = rebalance_schedule(market.decisions, rebalance_bars)
    out = EngineResult(np.zeros(T), gap_loss=np.zeros(T))
    e = capital
    drift = np.zeros(N)  # weights after the previous interval's price move (what we hold now)
    held = np.zeros(N)
    for t in range(T):
        out.equity[t] = e
        last = t == T - 1
        target = targets.weight[t]
        if min_trade_weight > 0:
            small = (np.sign(target) == np.sign(drift)) & (drift != 0) & (np.abs(target - drift) < min_trade_weight)
            target = np.where(small, drift, target)
        may_trade = market.tradable[t] & schedule[t]
        w: F64 = np.zeros(N) if last else np.asarray(np.where(may_trade, target, drift), dtype=np.float64)
        notional = np.abs(w - drift) * e
        step_cost = 0.0
        for i in np.nonzero(notional > 1e-12)[0]:
            step_cost += _rate(cost, market, t, int(i), float(notional[i]), fidelity) * float(notional[i])
            out.trades += 1
            out.traded_notional += float(notional[i])
        out.costs += step_cost
        side = np.sign(w)
        out.entries += int(np.count_nonzero((side != 0) & (side != np.sign(held))))
        held = w
        if last:
            out.equity[t] = e - step_cost
            break
        nxt, now = market.price[t + 1], market.price[t]
        with np.errstate(invalid="ignore", divide="ignore"):
            ret: F64 = np.asarray(np.where(np.isnan(nxt) | np.isnan(now), 0.0, nxt / now - 1.0), dtype=np.float64)
        pnl = float(np.dot(w, ret)) * e
        fund = -float(np.dot(w, market.funding[t])) * e if fidelity == "L1" else 0.0
        out.funding += fund
        out.position_bars += int(np.count_nonzero(w))
        for i in np.nonzero((w != 0) & ~market.tradable[t])[0]:
            out.gap_loss[t] += abs(float(w[i])) * e * _gap_fraction(market, t, int(i))
        if fidelity == "L1" and not out.liquidation_unmodelled:
            for i in np.nonzero(w)[0]:
                side_i = 1 if w[i] > 0 else -1
                liq = _liquidation_price(float(now[i]), side_i, float(w[i]), n_universe, cost)
                lo, hi = float(market.mark_low[t, i]), float(market.mark_high[t, i])
                if (side_i > 0 and lo <= liq) or (side_i < 0 and hi >= liq):
                    out.liquidation_unmodelled = True
                    out.notes.append(f"liquidation possible at t={t} {market.symbols[int(i)]}: use the event engine")
        e_next = e - step_cost + pnl + fund
        drift = w * e * (1.0 + ret) / e_next if e_next > 0 else np.zeros(N)
        e = e_next
    return out


@dataclass
class _Position:
    q: float = 0.0
    side: int = 0
    entry_t: int = -1
    entry_price: float = math.nan
    stop: float = math.nan
    liq: float = math.nan
    blocked: int = 0  # desire value that may not re-enter (after a stop / time stop / liquidation)


def run_event(
    market: Market,
    targets: Targets,
    cost: CostModel,
    *,
    capital: float,
    fidelity: Fidelity,
    n_universe: int,
    time_stop_bars: int | None = None,
    stop_loss_atr: float | None = None,
    rebalance_bars: int = 1,
    min_trade_weight: float = 0.0,
) -> EngineResult:
    """Path-dependent engine; `rebalance_bars` / `min_trade_weight` as in `run_vector`. Stops (time, ATR,
    liquidation) act on every bar, signal changes only on rebalance bars."""
    _check_schedule(rebalance_bars, min_trade_weight)
    T, N = market.price.shape
    schedule = rebalance_schedule(market.decisions, rebalance_bars)
    out = EngineResult(np.zeros(T), gap_loss=np.zeros(T))
    cash = capital
    pos = [_Position() for _ in range(N)]

    def trade(t: int, i: int, new_q: float, fill: float) -> None:
        nonlocal cash
        p = pos[i]
        delta = new_q - p.q
        notional = abs(delta) * fill
        if notional <= 1e-12:
            return
        rate = _rate(cost, market, t, i, notional, fidelity)
        cash -= delta * fill + rate * notional
        out.costs += rate * notional
        out.trades += 1
        out.traded_notional += notional
        p.q = new_q

    def flatten(p: _Position) -> None:
        p.q, p.side, p.entry_t, p.entry_price, p.stop, p.liq = 0.0, 0, -1, math.nan, math.nan, math.nan

    for t in range(T):
        price = market.price[t]
        equity = cash + sum(p.q * float(price[i]) for i, p in enumerate(pos) if p.q != 0)
        out.equity[t] = equity
        if t == T - 1:
            for i, p in enumerate(pos):
                if p.q != 0:
                    trade(t, i, 0.0, float(price[i]))
                    flatten(p)
            out.equity[t] = cash
            break
        for i, p in enumerate(pos):
            if not market.tradable[t, i]:
                continue  # no orders on missing data; the position is held
            desire = int(targets.desire[t, i])
            if p.blocked and desire == p.blocked:
                desire = 0
            elif desire != p.blocked:
                p.blocked = 0
            fill = float(price[i])
            # the time stop acts on every bar; between rebalances the signal may have changed already (review)
            stop_due = time_stop_bars is not None and p.side != 0 and t - p.entry_t >= time_stop_bars
            if stop_due and (desire == p.side or not schedule[t]):
                p.blocked = p.side
                trade(t, i, 0.0, fill)
                flatten(p)
                out.stops += 1
                continue
            if not schedule[t]:
                continue  # between rebalances only stops act; the position drifts
            atr = float(market.atr[t, i])
            if desire != p.side and desire != 0 and stop_loss_atr is not None and math.isnan(atr):
                desire = 0  # an ATR stop cannot be placed without an ATR estimate: no new position
            w = float(targets.weight[t, i]) if desire != 0 else 0.0
            target_q = w * equity / fill
            if desire != p.side:
                trade(t, i, target_q, fill)
                if desire == 0:
                    flatten(p)
                    continue
                out.entries += 1
                p.side, p.entry_t, p.entry_price = desire, t, fill
                p.liq = _liquidation_price(fill, desire, w, n_universe, cost)
                p.stop = fill - desire * stop_loss_atr * atr if stop_loss_atr is not None else math.nan
            elif desire != 0 and abs(w - p.q * fill / equity) >= min_trade_weight:
                trade(t, i, target_q, fill)  # same side: rebalance to the target weight (outside the band)
        # interval t: funding settlements, then liquidation and stop checks on the interval range
        for i, p in enumerate(pos):
            if p.q == 0:
                continue
            out.position_bars += 1
            if not market.tradable[t, i]:
                out.gap_loss[t] += abs(p.q) * float(price[i]) * _gap_fraction(market, t, i)
            if fidelity == "L1":
                for rate, mark in market.funding_events.get((t, i), []):
                    flow = funding_payment(p.q, mark, rate)
                    cash += flow
                    out.funding += flow
            lo, hi = float(market.low[t, i]), float(market.high[t, i])
            mlo, mhi = float(market.mark_low[t, i]), float(market.mark_high[t, i])
            if fidelity == "L1" and ((p.side > 0 and mlo <= p.liq) or (p.side < 0 and mhi >= p.liq)):
                notional = abs(p.q) * p.liq
                cash += p.q * p.liq - cost.liquidation_fee * notional
                out.costs += cost.liquidation_fee * notional
                out.liquidations += 1
                out.trades += 1
                p.blocked = p.side
                flatten(p)
                continue
            if not math.isnan(p.stop) and ((p.side > 0 and lo <= p.stop) or (p.side < 0 and hi >= p.stop)):
                trade(t, i, 0.0, p.stop)
                out.stops += 1
                p.blocked = p.side
                flatten(p)
    return out
