"""Paper fill model (Phase 6 step 13; design v2.1 §9 table "부분 체결", §11). Pure.

Fills are simulated against the collected 1 s top-of-book samples (bookTicker / Upbit L1):
- a taker order fills at the first quote at or after decision + latency, at the opposite side, up to the
  displayed quantity; the remainder keeps filling on later quotes (partial fills) until `max_wait_ns`;
- a limit (maker) order fills only when a later quote trades THROUGH the limit (ask < limit for a buy,
  bid > limit for a sell): queue position is unknown, so touching the price is not a fill;
- client ids are deterministic (run, spec, symbol, decision, sequence) and fit Binance's 36 characters.
Target-position planning turns target notionals into order intents honouring the minimum notional."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from quant_research.paper.ledger import OrderIntent, Side

CLIENT_ID_MAX = 36  # Binance clientOrderId; Upbit identifier allows 64


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    at_ns: int
    bid: float
    bid_qty: float
    ask: float
    ask_qty: float


@dataclass(frozen=True, slots=True)
class Fill:
    client_id: str
    qty: float
    price: float
    at_ns: int
    liquidity: str  # taker | maker


def client_id(run_id: str, spec_hash: str, symbol: str, decision_ns: int, seq: int) -> str:
    """Deterministic, collision-resistant, <= 36 chars: 'p' + 35 hex chars of a SHA-256."""
    body = f"{run_id}|{spec_hash}|{symbol}|{decision_ns}|{seq}"
    return "p" + hashlib.sha256(body.encode("utf-8")).hexdigest()[: CLIENT_ID_MAX - 1]


def plan_orders(
    *,
    run_id: str,
    spec_hash: str,
    venue: str,
    decision_ns: int,
    targets: Mapping[str, float],  # symbol -> target signed notional
    positions: Mapping[str, float],  # symbol -> current signed quantity
    prices: Mapping[str, float],  # symbol -> reference price for sizing
    min_notional: float,
    qty_step: float = 1e-6,
) -> list[OrderIntent]:
    """Orders that move each symbol from its current quantity to the target notional (taker, reduce-only when
    the trade only shrinks the position). Changes below the minimum notional are skipped."""
    out: list[OrderIntent] = []
    seq = 0
    for symbol in sorted(set(targets) | set(positions)):
        price = prices.get(symbol)
        if price is None or price <= 0:
            continue
        target_qty = targets.get(symbol, 0.0) / price
        delta = target_qty - positions.get(symbol, 0.0)
        qty = math.floor(abs(delta) / qty_step) * qty_step
        if qty * price < min_notional or qty <= 0:
            continue
        side: Side = "buy" if delta > 0 else "sell"
        cur = positions.get(symbol, 0.0)
        reduce_only = cur != 0 and (cur > 0) != (delta > 0) and abs(delta) <= abs(cur) + 1e-12
        out.append(
            OrderIntent(
                client_id(run_id, spec_hash, symbol, decision_ns, seq),
                run_id,
                spec_hash,
                venue,
                symbol,
                side,
                round(qty, 10),
                None,
                reduce_only,
                decision_ns,
            )
        )
        seq += 1
    return out


def simulate_fills(
    intent: OrderIntent,
    quotes: Sequence[Quote],
    *,
    arrive_ns: int,
    max_wait_ns: int,
) -> list[Fill]:
    """Fills for one order from a time-ordered quote stream of its symbol (empty when nothing fills)."""
    remaining = intent.qty
    fills: list[Fill] = []
    for q in quotes:
        if q.symbol != intent.symbol or q.at_ns < arrive_ns:
            continue
        if q.at_ns > arrive_ns + max_wait_ns or remaining <= 1e-12:
            break
        if intent.limit_price is None:  # taker: opposite side, displayed quantity
            price, avail = (q.ask, q.ask_qty) if intent.side == "buy" else (q.bid, q.bid_qty)
            liquidity = "taker"
        else:  # maker: only a quote that traded through the limit fills (queue position unknown)
            through = q.ask < intent.limit_price if intent.side == "buy" else q.bid > intent.limit_price
            if not through:
                continue
            price, avail = intent.limit_price, (q.ask_qty if intent.side == "buy" else q.bid_qty)
            liquidity = "maker"
        if avail <= 0 or price <= 0:
            continue
        qty = min(remaining, avail)
        fills.append(Fill(intent.client_id, qty, price, q.at_ns, liquidity))
        remaining -= qty
    return fills


def vwap(fills: Sequence[Fill]) -> float | None:
    qty = sum(f.qty for f in fills)
    return sum(f.qty * f.price for f in fills) / qty if qty > 0 else None


def slippage_bps(fills: Sequence[Fill], reference: float, side: Side) -> float | None:
    """Signed execution cost versus a reference price in basis points (positive = worse than reference)."""
    v = vwap(fills)
    if v is None or reference <= 0:
        return None
    sign = 1.0 if side == "buy" else -1.0
    return sign * (v - reference) / reference * 1e4
