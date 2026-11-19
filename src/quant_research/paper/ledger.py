"""ledger.db: hash-chained journal of every order event, with order / fill / position projections
(Phase 6 step 13; design v2.1 §4 K, §11 "주문 멱등", "상태가 불확실하면 조회만 하고 재전송 금지").

- Every state change is appended to the journal first (SHA-256 chain), then projected.
- `client_id` is the primary key: submitting the same id twice is a no-op that returns the existing order.
- An order whose venue state is unknown (timeout, lost ack) moves to UNCERTAIN: it may be QUERIED and later
  acked / filled / canceled, but never re-submitted and never replaced by a new order for the same intent.
- `verify_chain` recomputes the chain; `reconcile` compares the position projection with the fills."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

from quant_research.adapters.sqlite import connect, migrate
from quant_research.errors import AppError

GENESIS = "0" * 64
OrderState = Literal["PENDING_SUBMIT", "ACKED", "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED", "UNCERTAIN"]
Side = Literal["buy", "sell"]
TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    "PENDING_SUBMIT": frozenset({"ACKED", "REJECTED", "UNCERTAIN", "CANCELED"}),
    "ACKED": frozenset({"PARTIALLY_FILLED", "FILLED", "CANCELED", "UNCERTAIN"}),
    "PARTIALLY_FILLED": frozenset({"PARTIALLY_FILLED", "FILLED", "CANCELED", "UNCERTAIN"}),
    "UNCERTAIN": frozenset({"ACKED", "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED"}),  # resolved by a query
    "FILLED": frozenset(),
    "CANCELED": frozenset(),
    "REJECTED": frozenset(),
}


class LedgerError(AppError):
    """An order event that the ledger rules refuse."""


@dataclass(frozen=True, slots=True)
class OrderIntent:
    client_id: str
    run_id: str
    spec_hash: str
    venue: str
    symbol: str
    side: Side
    qty: float
    limit_price: float | None
    reduce_only: bool
    decision_ns: int


@dataclass(frozen=True, slots=True)
class OrderRow:
    client_id: str
    symbol: str
    side: Side
    qty: float
    filled_qty: float
    state: OrderState
    limit_price: float | None
    venue_order_id: str | None


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    qty: float
    avg_price: float
    realized_pnl: float
    fees: float


def chain_hash(prev_hash: str, kind: str, ref: str, payload: str, at_ns: int) -> str:
    return hashlib.sha256(f"{prev_hash}|{kind}|{ref}|{payload}|{at_ns}".encode()).hexdigest()


class PaperLedger:
    def __init__(self, conn: sqlite3.Connection, *, now_ns: Callable[[], int] = time.time_ns) -> None:
        self.conn = conn
        self.now_ns = now_ns

    @classmethod
    def open(cls, path: Path, *, now_ns: Callable[[], int] = time.time_ns) -> PaperLedger:
        conn = connect(path)
        migrate(conn, "quant_research.paper")
        return cls(conn, now_ns=now_ns)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Generator[None]:
        self.conn.execute("begin immediate")
        try:
            yield
            self.conn.execute("commit")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("rollback")
            raise

    # ------------------------------------------------------------------ journal
    def _append(self, kind: str, ref: str, payload: Mapping[str, object]) -> int:
        prev = self.conn.execute("select hash from journal order by seq desc limit 1").fetchone()
        prev_hash = str(prev[0]) if prev is not None else GENESIS
        body = json.dumps(dict(payload), sort_keys=True, default=str)
        at = self.now_ns()
        cur = self.conn.execute(
            "insert into journal(kind, ref, payload_json, at_ns, prev_hash, hash) values (?,?,?,?,?,?)",
            (kind, ref, body, at, prev_hash, chain_hash(prev_hash, kind, ref, body, at)),
        )
        return int(cur.lastrowid or 0)

    def verify_chain(self) -> tuple[bool, str]:
        prev = GENESIS
        for seq, kind, ref, payload, at, prev_hash, h in self.conn.execute(
            "select seq, kind, ref, payload_json, at_ns, prev_hash, hash from journal order by seq"
        ):
            if prev_hash != prev:
                return False, f"seq {seq}: broken link"
            if h != chain_hash(prev, str(kind), str(ref), str(payload), int(at)):
                return False, f"seq {seq}: hash mismatch"
            prev = str(h)
        return True, "ok"

    # ------------------------------------------------------------------ orders
    def order(self, client_id: str) -> OrderRow | None:
        row = self.conn.execute(
            "select client_id, symbol, side, qty, filled_qty, state, limit_price, venue_order_id from order_ "
            "where client_id = ?",
            (client_id,),
        ).fetchone()
        if row is None:
            return None
        return OrderRow(
            str(row[0]),
            str(row[1]),
            cast(Side, str(row[2])),
            float(row[3]),
            float(row[4]),
            cast(OrderState, str(row[5])),
            None if row[6] is None else float(row[6]),
            None if row[7] is None else str(row[7]),
        )

    def submit(self, intent: OrderIntent) -> OrderRow:
        """Idempotent: an existing client_id returns the existing order unchanged (never a second order)."""
        existing = self.order(intent.client_id)
        if existing is not None:
            return existing
        if intent.qty <= 0:
            raise LedgerError(f"order {intent.client_id}: qty must be > 0")
        now = self.now_ns()
        with self.transaction():
            self._append("order", intent.client_id, asdict(intent))
            self.conn.execute(
                "insert into order_(client_id, run_id, spec_hash, venue, symbol, side, qty, limit_price, reduce_only, "
                "state, decision_ns, created_ns, updated_ns) values (?,?,?,?,?,?,?,?,?,'PENDING_SUBMIT',?,?,?)",
                (
                    intent.client_id,
                    intent.run_id,
                    intent.spec_hash,
                    intent.venue,
                    intent.symbol,
                    intent.side,
                    intent.qty,
                    intent.limit_price,
                    int(intent.reduce_only),
                    intent.decision_ns,
                    now,
                    now,
                ),
            )
        return cast(OrderRow, self.order(intent.client_id))

    def _move(self, client_id: str, dst: OrderState, kind: str, payload: Mapping[str, object]) -> OrderRow:
        cur = self.order(client_id)
        if cur is None:
            raise LedgerError(f"unknown order {client_id}")
        if dst not in TRANSITIONS[cur.state]:
            raise LedgerError(f"order {client_id}: {cur.state} -> {dst} is not allowed")
        with self.transaction():
            self._append(kind, client_id, {"from": cur.state, "to": dst, **payload})
            self.conn.execute(
                "update order_ set state = ?, updated_ns = ? where client_id = ?", (dst, self.now_ns(), client_id)
            )
        return cast(OrderRow, self.order(client_id))

    def ack(self, client_id: str, venue_order_id: str) -> OrderRow:
        row = self._move(client_id, "ACKED", "ack", {"venue_order_id": venue_order_id})
        self.conn.execute("update order_ set venue_order_id = ? where client_id = ?", (venue_order_id, client_id))
        return row

    def reject(self, client_id: str, why: str) -> OrderRow:
        return self._move(client_id, "REJECTED", "reject", {"why": why})

    def cancel(self, client_id: str, why: str) -> OrderRow:
        return self._move(client_id, "CANCELED", "cancel", {"why": why})

    def mark_uncertain(self, client_id: str, why: str) -> OrderRow:
        """The venue state is unknown: query only from here on (§11); a resubmit is refused by `submit`."""
        return self._move(client_id, "UNCERTAIN", "uncertain", {"why": why})

    def fill(self, client_id: str, qty: float, price: float, *, fee: float, liquidity: str, at_ns: int) -> OrderRow:
        cur = self.order(client_id)
        if cur is None:
            raise LedgerError(f"unknown order {client_id}")
        if qty <= 0 or price <= 0:
            raise LedgerError(f"order {client_id}: fill qty and price must be > 0")
        if cur.filled_qty + qty > cur.qty + 1e-12:
            raise LedgerError(f"order {client_id}: fill {qty} exceeds the remaining {cur.qty - cur.filled_qty}")
        done = cur.filled_qty + qty >= cur.qty - 1e-12
        dst: OrderState = "FILLED" if done else "PARTIALLY_FILLED"
        if dst not in TRANSITIONS[cur.state]:
            raise LedgerError(f"order {client_id}: {cur.state} -> {dst} is not allowed")
        with self.transaction():
            self._append(
                "fill", client_id, {"qty": qty, "price": price, "fee": fee, "liquidity": liquidity, "at_ns": at_ns}
            )
            self.conn.execute(
                "insert into fill(client_id, qty, price, fee, at_ns, liquidity) values (?,?,?,?,?,?)",
                (client_id, qty, price, fee, at_ns, liquidity),
            )
            self.conn.execute(
                "update order_ set state = ?, filled_qty = filled_qty + ?, updated_ns = ? where client_id = ?",
                (dst, qty, self.now_ns(), client_id),
            )
            self._apply_position(cur.symbol, cur.side, qty, price, fee)
        return cast(OrderRow, self.order(client_id))

    # ------------------------------------------------------------------ positions
    def _apply_position(self, symbol: str, side: Side, qty: float, price: float, fee: float) -> None:
        pos = self.position(symbol)
        signed = qty if side == "buy" else -qty
        if pos is None:
            new_qty, avg, realized = signed, price, 0.0
        else:
            same_sign = pos.qty == 0 or (pos.qty > 0) == (signed > 0)
            if same_sign:
                total = pos.qty + signed
                avg = (pos.avg_price * abs(pos.qty) + price * abs(signed)) / abs(total) if total else price
                new_qty, realized = total, 0.0
            else:
                closed = min(abs(pos.qty), abs(signed))
                realized = closed * (price - pos.avg_price) * (1 if pos.qty > 0 else -1)
                new_qty = pos.qty + signed
                avg = pos.avg_price if abs(new_qty) < abs(pos.qty) else price
        now = self.now_ns()
        self.conn.execute(
            "insert into position(symbol, qty, avg_price, realized_pnl, fees, updated_ns) values (?,?,?,?,?,?) "
            "on conflict(symbol) do update set qty = excluded.qty, avg_price = excluded.avg_price, "
            "realized_pnl = position.realized_pnl + ?, fees = position.fees + ?, updated_ns = excluded.updated_ns",
            (symbol, new_qty, avg, realized, fee, now, realized, fee),
        )
        self._append("position", symbol, {"qty": new_qty, "avg_price": avg, "realized": realized, "fee": fee})

    def position(self, symbol: str) -> Position | None:
        row = self.conn.execute(
            "select symbol, qty, avg_price, realized_pnl, fees from position where symbol = ?", (symbol,)
        ).fetchone()
        return (
            None if row is None else Position(str(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]))
        )

    def positions(self) -> dict[str, Position]:
        rows = self.conn.execute("select symbol, qty, avg_price, realized_pnl, fees from position").fetchall()
        return {str(r[0]): Position(str(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows}

    def reconcile(self) -> list[str]:
        """Positions recomputed from the fills must equal the projection (a mismatch means: stop, §11)."""
        rebuilt: dict[str, float] = {}
        for symbol, side, qty in self.conn.execute(
            "select o.symbol, o.side, f.qty from fill f join order_ o using (client_id) order by f.fill_id"
        ):
            rebuilt[str(symbol)] = rebuilt.get(str(symbol), 0.0) + (float(qty) if side == "buy" else -float(qty))
        problems: list[str] = []
        for symbol, pos in self.positions().items():
            if abs(rebuilt.get(symbol, 0.0) - pos.qty) > 1e-9:
                problems.append(f"{symbol}: projection {pos.qty} != fills {rebuilt.get(symbol, 0.0)}")
        for symbol, qty in rebuilt.items():
            if symbol not in self.positions() and abs(qty) > 1e-9:
                problems.append(f"{symbol}: fills {qty} but no position row")
        return problems

    def open_orders(self) -> list[OrderRow]:
        rows = self.conn.execute(
            "select client_id from order_ where state in ('PENDING_SUBMIT','ACKED','PARTIALLY_FILLED','UNCERTAIN') "
            "order by created_ns"
        ).fetchall()
        return [o for o in (self.order(str(r[0])) for r in rows) if o is not None]
