"""Phase 7 step 19-20: live client ids per venue, the uncertain -> query-only rule, protective stops,
dead-man requests with Upbit's 2 s limit, safe-stop reasons and the testnet request formats."""

from __future__ import annotations

import pytest

from quant_research.live.deadman import HeartbeatState, HostHealth, deadman_requests, safe_stop_reasons
from quant_research.live.orders import (
    EntryIntent,
    OrderRuleError,
    check_move,
    increases_position,
    live_client_id,
    next_action,
    protective_stop,
)
from quant_research.live.testnet import (
    binance_um_countdown_cancel_all,
    binance_um_test_order,
    upbit_cancel_all,
    upbit_order_test,
)


def test_client_ids_fit_each_venue_and_are_deterministic() -> None:
    b = live_client_id("binance", "acct", "spec", "BTCUSDT", 1, 0)
    u = live_client_id("upbit", "acct", "spec", "KRW-BTC", 1, 0)
    assert len(b) == 36 and len(u) == 64 and b == live_client_id("binance", "acct", "spec", "BTCUSDT", 1, 0)
    assert b != live_client_id("binance", "acct", "spec", "BTCUSDT", 1, 1)


def test_uncertain_orders_are_only_queried_and_transitions_are_checked() -> None:
    assert next_action("PENDING_SUBMIT", ack_timed_out=False, want_cancel=False) == "submit"
    assert next_action("PENDING_SUBMIT", ack_timed_out=True, want_cancel=False) == "query"
    assert next_action("UNCERTAIN", ack_timed_out=True, want_cancel=True) == "query"  # never resubmit
    assert next_action("ACKED", ack_timed_out=False, want_cancel=True) == "cancel"
    assert next_action("FILLED", ack_timed_out=False, want_cancel=True) == "none"
    check_move("UNCERTAIN", "ACKED")
    with pytest.raises(OrderRuleError):
        check_move("FILLED", "ACKED")


def test_every_position_increasing_entry_gets_a_reduce_only_stop() -> None:
    entry = EntryIntent("binance", "BTCUSDT", "buy", 0.5, live_client_id("binance", "a", "s", "BTCUSDT", 1, 0), 100.0)
    stop = protective_stop(entry, stop_distance=0.05)
    assert stop.side == "sell" and stop.reduce_only and stop.stop_price == pytest.approx(95.0) and stop.qty == 0.5
    assert len(stop.client_id) <= 36 and stop.client_id.endswith("-s")
    short = protective_stop(EntryIntent("upbit", "KRW-BTC", "sell", 1.0, "c" * 64, 100.0), stop_distance=0.1)
    assert short.side == "buy" and short.stop_price == pytest.approx(110.0) and len(short.client_id) == 64
    with pytest.raises(OrderRuleError):
        protective_stop(entry, stop_distance=1.5)
    assert increases_position(0.0, "buy", 1.0) and increases_position(-1.0, "sell", 0.5)
    assert not increases_position(1.0, "sell", 0.5) and not increases_position(1.0, "sell", 1.0)


def test_deadman_requests_and_upbit_rate_limit() -> None:
    alive = HeartbeatState(last_beat_ns=100, now_ns=150, timeout_ns=100)
    stale = HeartbeatState(last_beat_ns=0, now_ns=500, timeout_ns=100)
    binance_alive = deadman_requests("binance", alive, last_cancel_all_ns=None)
    assert [r.kind for r in binance_alive] == ["arm_countdown"] and binance_alive[0].unverified_endpoint
    assert [r.kind for r in deadman_requests("binance", stale, last_cancel_all_ns=None)] == [
        "arm_countdown",
        "cancel_all",
    ]
    assert deadman_requests("upbit", alive, last_cancel_all_ns=None) == []
    assert [r.kind for r in deadman_requests("upbit", stale, last_cancel_all_ns=None)] == ["cancel_all"]
    assert deadman_requests("upbit", stale, last_cancel_all_ns=500 - 10**9) == []  # cancelled 1 s ago: wait
    assert deadman_requests("upbit", stale, last_cancel_all_ns=500 - 3 * 10**9) != []


def test_safe_stop_reasons_cover_every_section_11_condition() -> None:
    ok = HostHealth(False, 10.0, False, 0.0, False)
    assert safe_stop_reasons(ok) == []
    bad = HostHealth(True, 300.0, True, 0.5, True)
    reasons = safe_stop_reasons(bad)
    assert len(reasons) == 5 and reasons[-1].endswith("kill")


def test_request_formats_are_shaped_and_marked_unverified() -> None:
    r = binance_um_test_order("BTCUSDT", "buy", 0.001, "cid", reduce_only=True)
    assert r.method == "POST" and r.path == "/fapi/v1/order" and r.base_url.startswith("https://testnet.")
    assert r.params["quantity"] == "0.001" and r.params["side"] == "BUY" and r.params["reduceOnly"] == "true"
    assert r.needs_signature and r.unverified
    with pytest.raises(ValueError):
        binance_um_test_order("BTCUSDT", "buy", 0.0, "cid")
    c = binance_um_countdown_cancel_all("BTCUSDT", 60_000)
    assert c.params["countdownTime"] == "60000"
    u = upbit_order_test("KRW-BTC", "buy", volume=None, price=5_000.0, identifier="id-1")
    assert u.path == "/v1/orders/test" and u.params["ord_type"] == "price" and u.params["side"] == "bid"
    limit = upbit_order_test("KRW-BTC", "sell", volume=0.1, price=50_000_000.0, identifier="x")
    assert limit.params["ord_type"] == "limit" and limit.params["side"] == "ask"
    with pytest.raises(ValueError):
        upbit_order_test("KRW-BTC", "buy", volume=None, price=1.0, identifier="y" * 65)
    assert upbit_cancel_all().method == "DELETE"
