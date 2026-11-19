"""Order idempotency and state rules for the live host (Phase 7 step 19; design v2.1 §11). Pure.

- Deterministic client ids per venue: Binance `newClientOrderId` (<= 36 chars, [A-Za-z0-9._:-]), Upbit
  `identifier` (<= 64 chars, permanently unique per account).
- The order state machine mirrors the paper ledger (PENDING_SUBMIT -> ACKED -> ... ) and encodes the
  §11 rule: when the venue state is unknown the ONLY allowed action is QUERY; resubmitting is refused.
- Protective orders: every entry that opens or increases a position is accompanied by a venue-side stop
  (reduce-only STOP) so the kill switch does not depend on this host being alive.
No network, no keys: these functions produce request intents that the live executor would send."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from quant_research.errors import AppError
from quant_research.paper.ledger import TRANSITIONS, OrderState, Side

Venue = Literal["binance", "upbit"]
Action = Literal["submit", "query", "cancel", "none"]
CLIENT_ID_MAX = {"binance": 36, "upbit": 64}
_BINANCE_OK = re.compile(r"^[A-Za-z0-9._:-]{1,36}$")


class OrderRuleError(AppError):
    """An action the live order rules refuse."""


def live_client_id(venue: Venue, account: str, spec_hash: str, symbol: str, decision_ns: int, seq: int) -> str:
    """Deterministic per (account, spec, symbol, decision, seq); the same intent always maps to one id."""
    digest = hashlib.sha256(f"{account}|{spec_hash}|{symbol}|{decision_ns}|{seq}".encode()).hexdigest()
    cid = ("q" + digest)[: CLIENT_ID_MAX[venue]]
    if venue == "binance" and not _BINANCE_OK.match(cid):
        raise OrderRuleError(f"client id {cid!r} violates the Binance format")
    return cid


def next_action(state: OrderState, *, ack_timed_out: bool, want_cancel: bool) -> Action:
    """What the executor may do with an order in `state` (§11: uncertain -> query only, never resubmit)."""
    if state == "PENDING_SUBMIT":
        return "query" if ack_timed_out else "submit"
    if state == "UNCERTAIN":
        return "query"
    if state in ("ACKED", "PARTIALLY_FILLED"):
        return "cancel" if want_cancel else "none"
    return "none"


def check_move(src: OrderState, dst: OrderState) -> None:
    if dst not in TRANSITIONS[src]:
        raise OrderRuleError(f"{src} -> {dst} is not an allowed order transition")


@dataclass(frozen=True, slots=True)
class EntryIntent:
    venue: Venue
    symbol: str
    side: Side
    qty: float
    client_id: str
    reference_price: float


@dataclass(frozen=True, slots=True)
class ProtectiveStop:
    venue: Venue
    symbol: str
    side: Side  # opposite of the entry
    qty: float
    stop_price: float
    reduce_only: bool
    client_id: str


def protective_stop(entry: EntryIntent, *, stop_distance: float) -> ProtectiveStop:
    """The venue-side reduce-only stop that accompanies every position-increasing entry (§11 Kill Switch).
    `stop_distance` is a fraction of the reference price (e.g. 0.05 = 5 %)."""
    if not 0 < stop_distance < 1:
        raise OrderRuleError(f"stop_distance must be in (0, 1), got {stop_distance}")
    if entry.qty <= 0 or entry.reference_price <= 0:
        raise OrderRuleError("entry needs qty and reference price > 0")
    opposite: Side = "sell" if entry.side == "buy" else "buy"
    price = entry.reference_price * (1 - stop_distance if entry.side == "buy" else 1 + stop_distance)
    cid = (entry.client_id[: CLIENT_ID_MAX[entry.venue] - 2] + "-s")[: CLIENT_ID_MAX[entry.venue]]
    return ProtectiveStop(entry.venue, entry.symbol, opposite, entry.qty, round(price, 10), True, cid)


def increases_position(current_qty: float, side: Side, qty: float) -> bool:
    signed = qty if side == "buy" else -qty
    after = current_qty + signed
    return abs(after) > abs(current_qty) + 1e-12
