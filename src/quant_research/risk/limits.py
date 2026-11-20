"""Pre-trade and portfolio limits (design v2.1 §11 "한도": gross and per-symbol exposure, leverage, daily /
weekly / cumulative loss, order rate, order notional). Pure: no clock, no I/O.

`check_order` returns every violated rule (an empty list means the order may go out); `kill_switch`
returns the reason when the loss limits or data / clock health demand that NO new order goes out and the
open ones are cancelled (§11 "장애 시 안전 정지")."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class Limits:
    max_gross_notional: float
    max_symbol_notional: float
    max_leverage: float
    max_daily_loss: float  # positive numbers: the loss that trips the limit
    max_weekly_loss: float
    max_total_loss: float
    max_orders_per_minute: int
    max_order_notional: float

    def __post_init__(self) -> None:
        values = (
            self.max_gross_notional,
            self.max_symbol_notional,
            self.max_leverage,
            self.max_daily_loss,
            self.max_weekly_loss,
            self.max_total_loss,
            self.max_order_notional,
        )
        if any(v <= 0 for v in values) or self.max_orders_per_minute <= 0:
            raise ValueError(f"every limit must be > 0: {self}")


@dataclass(frozen=True, slots=True)
class Exposure:
    equity: float
    gross_notional: float  # sum of |position notional|
    per_symbol: Mapping[str, float]  # symbol -> signed position notional
    daily_pnl: float
    weekly_pnl: float
    total_pnl: float
    orders_last_minute: int
    data_stale: bool = False  # §11: missing data, clock offset, heartbeat loss stop new orders
    clock_offset_ms: float = 0.0
    max_clock_offset_ms: float = 250.0


def kill_switch(e: Exposure, limits: Limits) -> str | None:
    """Reason to stop ALL new orders (and cancel open ones); None when trading may continue."""
    if e.daily_pnl <= -limits.max_daily_loss:
        return f"daily loss {e.daily_pnl:.2f} reached the limit {limits.max_daily_loss:.2f}"
    if e.weekly_pnl <= -limits.max_weekly_loss:
        return f"weekly loss {e.weekly_pnl:.2f} reached the limit {limits.max_weekly_loss:.2f}"
    if e.total_pnl <= -limits.max_total_loss:
        return f"cumulative loss {e.total_pnl:.2f} reached the limit {limits.max_total_loss:.2f}"
    if e.data_stale:
        return "market data or heartbeat is stale"
    if abs(e.clock_offset_ms) > e.max_clock_offset_ms:
        return f"clock offset {e.clock_offset_ms:.0f} ms exceeds {e.max_clock_offset_ms:.0f} ms"
    if e.equity <= 0:
        return "equity is not positive"
    return None


def check_order(symbol: str, signed_notional: float, e: Exposure, limits: Limits) -> list[str]:
    """Violations of one prospective order (signed notional: + buy, - sell). Empty list = allowed."""
    problems: list[str] = []
    stop = kill_switch(e, limits)
    if stop is not None:
        problems.append(f"kill switch: {stop}")
    notional = abs(signed_notional)
    if notional > limits.max_order_notional:
        problems.append(f"order notional {notional:.2f} > max {limits.max_order_notional:.2f}")
    current = e.per_symbol.get(symbol, 0.0)
    after_symbol = current + signed_notional
    reduces = abs(after_symbol) < abs(current)
    if not reduces:
        if abs(after_symbol) > limits.max_symbol_notional:
            problems.append(f"{symbol} notional after {abs(after_symbol):.2f} > max {limits.max_symbol_notional:.2f}")
        gross_after = e.gross_notional - abs(current) + abs(after_symbol)
        if gross_after > limits.max_gross_notional:
            problems.append(f"gross notional after {gross_after:.2f} > max {limits.max_gross_notional:.2f}")
        if e.equity > 0 and gross_after / e.equity > limits.max_leverage:
            problems.append(f"leverage after {gross_after / e.equity:.2f} > max {limits.max_leverage:.2f}")
    if e.orders_last_minute >= limits.max_orders_per_minute:
        problems.append(f"{e.orders_last_minute} orders in the last minute >= max {limits.max_orders_per_minute}")
    return problems


def approve_in_order(orders: Sequence[tuple[str, float]], e: Exposure, limits: Limits) -> list[list[str]]:
    """Violations per order (symbol, signed notional), each checked against the exposure AFTER the approved
    orders before it (review finding 5: checking every order of a tick against the starting exposure let
    8 x 4,000 pass a 20,000 gross limit together, and the order-rate limit never saw the tick's own orders).
    Refused orders change nothing."""
    out: list[list[str]] = []
    cur = e
    for symbol, notional in orders:
        problems = check_order(symbol, notional, cur, limits)
        out.append(problems)
        if not problems:
            per = dict(cur.per_symbol)
            before = per.get(symbol, 0.0)
            per[symbol] = before + notional
            gross = cur.gross_notional - abs(before) + abs(per[symbol])
            cur = replace(cur, per_symbol=per, gross_notional=gross, orders_last_minute=cur.orders_last_minute + 1)
    return out
