"""Phase 6 step 13: hash-chained paper ledger (idempotent client ids, state machine, UNCERTAIN = query only,
positions, reconciliation) and the quote-driven fill model (taker partials, maker trade-through, client ids)."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest

from quant_research.paper.engine import CLIENT_ID_MAX, Quote, client_id, plan_orders, simulate_fills, slippage_bps, vwap
from quant_research.paper.ledger import LedgerError, OrderIntent, PaperLedger


def _intent(cid: str = "p1", side: str = "buy", qty: float = 2.0, limit: float | None = None) -> OrderIntent:
    return OrderIntent(cid, "run", "spec", "binance", "BTCUSDT", side, qty, limit, False, 1_000)  # type: ignore[arg-type]


@pytest.fixture
def ledger(tmp_path: Path) -> Generator[PaperLedger]:
    clock = {"t": 100}

    def now() -> int:
        clock["t"] += 1
        return clock["t"]

    led = PaperLedger.open(tmp_path / "ledger.db", now_ns=now)
    yield led
    led.close()


def test_submission_is_idempotent_and_the_state_machine_is_enforced(ledger: PaperLedger) -> None:
    first = ledger.submit(_intent())
    again = ledger.submit(_intent(qty=99.0))  # same client id: no second order, nothing changes
    assert first == again and again.qty == 2.0 and again.state == "PENDING_SUBMIT"
    assert ledger.ack("p1", "v-1").state == "ACKED"
    with pytest.raises(LedgerError):
        ledger.ack("p1", "v-2")  # ACKED -> ACKED is not a transition
    ledger.fill("p1", 0.5, 100.0, fee=0.01, liquidity="taker", at_ns=5)
    assert ledger.order("p1").state == "PARTIALLY_FILLED"  # type: ignore[union-attr]
    with pytest.raises(LedgerError):
        ledger.fill("p1", 5.0, 100.0, fee=0.0, liquidity="taker", at_ns=6)  # over-fill
    ledger.fill("p1", 1.5, 101.0, fee=0.01, liquidity="taker", at_ns=7)
    assert ledger.order("p1").state == "FILLED"  # type: ignore[union-attr]
    with pytest.raises(LedgerError):
        ledger.cancel("p1", "late")  # terminal
    with pytest.raises(LedgerError):
        ledger.submit(_intent(cid="p0", qty=0.0))


def test_uncertain_orders_are_queried_never_resubmitted(ledger: PaperLedger) -> None:
    ledger.submit(_intent("u1"))
    ledger.mark_uncertain("u1", "ack timeout")
    assert ledger.order("u1").state == "UNCERTAIN"  # type: ignore[union-attr]
    same = ledger.submit(_intent("u1"))  # a retry with the same intent returns the uncertain order, no new order
    assert same.state == "UNCERTAIN"
    assert [o.client_id for o in ledger.open_orders()] == ["u1"]
    ledger.ack("u1", "v-9")  # the query resolved it
    assert ledger.order("u1").state == "ACKED"  # type: ignore[union-attr]
    ledger.submit(_intent("u2"))
    ledger.reject("u2", "filter")
    assert ledger.order("u2").state == "REJECTED"  # type: ignore[union-attr]
    assert ledger.conn.execute("select count(*) from order_").fetchone()[0] == 2


def test_positions_realized_pnl_and_reconciliation(ledger: PaperLedger) -> None:
    ledger.submit(_intent("b1", "buy", 2.0))
    ledger.ack("b1", "v1")
    ledger.fill("b1", 2.0, 100.0, fee=0.2, liquidity="taker", at_ns=1)
    ledger.submit(_intent("s1", "sell", 1.0))
    ledger.ack("s1", "v2")
    ledger.fill("s1", 1.0, 110.0, fee=0.1, liquidity="taker", at_ns=2)
    pos = ledger.position("BTCUSDT")
    assert pos is not None and pos.qty == pytest.approx(1.0) and pos.avg_price == pytest.approx(100.0)
    assert pos.realized_pnl == pytest.approx(10.0) and pos.fees == pytest.approx(0.3)
    assert ledger.reconcile() == []
    ledger.conn.execute("update position set qty = 5 where symbol = 'BTCUSDT'")  # a corrupted projection
    assert ledger.reconcile() and "projection" in ledger.reconcile()[0]


def test_journal_hash_chain_detects_tampering_and_survives_reopen(tmp_path: Path) -> None:
    led = PaperLedger.open(tmp_path / "l.db")
    led.submit(_intent("j1"))
    led.ack("j1", "v")
    led.close()
    led = PaperLedger.open(tmp_path / "l.db")  # restart: same files
    assert led.verify_chain() == (True, "ok") and led.order("j1").state == "ACKED"  # type: ignore[union-attr]
    with pytest.raises(sqlite3.IntegrityError):
        led.conn.execute("delete from journal where seq = 1")
    with pytest.raises(sqlite3.IntegrityError):
        led.conn.execute("update journal set payload_json = '{}' where seq = 1")
    led.conn.execute(
        "insert into journal(kind, ref, payload_json, at_ns, prev_hash, hash) values ('note', 'x', '{}', 1, 'bad', 'h')"
    )
    ok, why = led.verify_chain()
    assert not ok and "broken link" in why
    led.close()


def test_client_ids_are_deterministic_unique_and_short() -> None:
    a = client_id("run", "spec", "BTCUSDT", 1_000, 0)
    assert a == client_id("run", "spec", "BTCUSDT", 1_000, 0) and len(a) == CLIENT_ID_MAX
    assert a != client_id("run", "spec", "BTCUSDT", 1_000, 1) and a != client_id("run", "spec", "ETHUSDT", 1_000, 0)


def test_plan_orders_respects_min_notional_and_marks_reduce_only() -> None:
    orders = plan_orders(
        run_id="r",
        spec_hash="s",
        venue="binance",
        decision_ns=10,
        targets={"BTCUSDT": 1_000.0, "ETHUSDT": 0.0, "SOLUSDT": 3.0},
        positions={"BTCUSDT": 0.02, "ETHUSDT": -1.0},
        prices={"BTCUSDT": 50_000.0, "ETHUSDT": 2_000.0, "SOLUSDT": 100.0},
        min_notional=10.0,
    )
    by = {o.symbol: o for o in orders}
    assert "SOLUSDT" not in by  # 3 USDT < min notional
    assert "BTCUSDT" not in by  # already at the target quantity (0.02 BTC): nothing to trade
    eth = by["ETHUSDT"]
    assert eth.side == "buy" and eth.qty == pytest.approx(1.0) and eth.reduce_only is True  # closing a short
    assert len({o.client_id for o in orders}) == len(orders)


def test_taker_fills_partially_across_quotes_and_maker_needs_a_trade_through() -> None:
    quotes = [
        Quote("BTCUSDT", 1_000, 99.0, 1.0, 101.0, 0.6),
        Quote("BTCUSDT", 2_000, 99.5, 1.0, 101.5, 0.6),
        Quote("BTCUSDT", 3_000, 100.0, 1.0, 102.0, 5.0),
        Quote("ETHUSDT", 3_000, 10.0, 1.0, 11.0, 5.0),
    ]
    taker = simulate_fills(_intent("t", "buy", 1.5), quotes, arrive_ns=1_500, max_wait_ns=10_000)
    assert [(f.price, f.qty) for f in taker] == [(101.5, 0.6), (102.0, 0.9)]  # the first quote is before arrival
    assert vwap(taker) == pytest.approx((101.5 * 0.6 + 102.0 * 0.9) / 1.5)
    assert slippage_bps(taker, 101.0, "buy") is not None and slippage_bps(taker, 101.0, "buy") > 0  # type: ignore[operator]
    assert simulate_fills(_intent("t2", "buy", 1.0), quotes, arrive_ns=1_500, max_wait_ns=100) == []  # waited too short
    maker_touch = simulate_fills(_intent("m", "buy", 1.0, limit=101.5), quotes, arrive_ns=0, max_wait_ns=10_000)
    assert all(f.price == 101.5 and f.liquidity == "maker" for f in maker_touch)
    # ask 101.0 < limit 101.5 at t=1000 trades through: fills at the limit; later asks >= limit do not
    assert [(f.at_ns, f.qty) for f in maker_touch] == [(1_000, 0.6)]
    sell_maker = simulate_fills(_intent("sm", "sell", 1.0, limit=99.8), quotes, arrive_ns=0, max_wait_ns=10_000)
    assert [(f.at_ns, f.price) for f in sell_maker] == [(3_000, 99.8)]  # only bid 100.0 > 99.8 trades through
    assert vwap([]) is None and slippage_bps([], 1.0, "buy") is None
