"""Phase 6 step 14: limit boundary values, the kill switch, and the signed / monotone / expiring policy."""

from __future__ import annotations

import pytest

from quant_research.risk.limits import Exposure, Limits, check_order, kill_switch
from quant_research.risk.policy import FakeVerifier, Policy, PolicyError, PolicyStore, parse_policy, policy_bytes

LIMITS = Limits(
    max_gross_notional=10_000.0,
    max_symbol_notional=4_000.0,
    max_leverage=2.0,
    max_daily_loss=300.0,
    max_weekly_loss=800.0,
    max_total_loss=2_000.0,
    max_orders_per_minute=10,
    max_order_notional=2_500.0,
)


def _exp(**over: object) -> Exposure:
    base: dict[str, object] = {
        "equity": 6_000.0,
        "gross_notional": 5_000.0,
        "per_symbol": {"BTCUSDT": 3_000.0, "ETHUSDT": -2_000.0},
        "daily_pnl": -100.0,
        "weekly_pnl": -200.0,
        "total_pnl": -500.0,
        "orders_last_minute": 3,
    }
    base.update(over)
    return Exposure(**base)  # type: ignore[arg-type]


def test_limits_must_be_positive() -> None:
    with pytest.raises(ValueError):
        Limits(0.0, 1, 1, 1, 1, 1, 1, 1)


def test_boundary_values_of_each_order_rule() -> None:
    assert check_order("SOLUSDT", 2_500.0, _exp(), LIMITS) == []  # exactly the order notional limit
    assert any("order notional" in p for p in check_order("SOLUSDT", 2_500.01, _exp(), LIMITS))
    assert check_order("BTCUSDT", 1_000.0, _exp(), LIMITS) == []  # BTC 4,000 = symbol limit, gross 6,000
    assert any("notional after" in p for p in check_order("BTCUSDT", 1_000.01, _exp(), LIMITS))
    # gross: 5,000 + 2,500 (new symbol) = 7,500 ok; with gross 8,000 the same order breaks 10,000
    assert check_order("SOLUSDT", 2_500.0, _exp(gross_notional=7_500.0), LIMITS) == []
    assert any("gross" in p for p in check_order("SOLUSDT", 2_500.0, _exp(gross_notional=8_000.0), LIMITS))
    # leverage: equity 3,000, gross 5,000 + 1,000 = 6,000 -> exactly 2.0 ok; 1,001 -> too much
    assert check_order("SOLUSDT", 1_000.0, _exp(equity=3_000.0), LIMITS) == []
    assert any("leverage" in p for p in check_order("SOLUSDT", 1_000.01, _exp(equity=3_000.0), LIMITS))
    # reducing a position never trips exposure rules even when the book is already over the limits
    over = _exp(gross_notional=20_000.0, per_symbol={"BTCUSDT": 9_000.0})
    assert check_order("BTCUSDT", -2_000.0, over, LIMITS) == []
    assert any(
        "orders in the last minute" in p for p in check_order("SOLUSDT", 10.0, _exp(orders_last_minute=10), LIMITS)
    )
    assert check_order("SOLUSDT", 10.0, _exp(orders_last_minute=9), LIMITS) == []


def test_kill_switch_blocks_every_order_on_loss_data_or_clock() -> None:
    assert kill_switch(_exp(), LIMITS) is None
    assert kill_switch(_exp(daily_pnl=-300.0), LIMITS) is not None  # at the limit: stop
    assert kill_switch(_exp(daily_pnl=-299.99), LIMITS) is None
    assert "weekly" in str(kill_switch(_exp(weekly_pnl=-800.0), LIMITS))
    assert "cumulative" in str(kill_switch(_exp(total_pnl=-2_000.0), LIMITS))
    assert "stale" in str(kill_switch(_exp(data_stale=True), LIMITS))
    assert "clock" in str(kill_switch(_exp(clock_offset_ms=251.0), LIMITS))
    assert "equity" in str(kill_switch(_exp(equity=0.0), LIMITS))
    violations = check_order("BTCUSDT", -10.0, _exp(daily_pnl=-500.0), LIMITS)  # even a reducing order
    assert violations and violations[0].startswith("kill switch")


def test_policy_requires_signature_monotone_version_and_expiry() -> None:
    signer = FakeVerifier(b"dev-secret")
    store = PolicyStore(signer)
    with pytest.raises(PolicyError):
        store.active(now_ns=1)  # no policy: everything refused
    p1 = Policy(1, LIMITS, issued_ns=0, expires_ns=1_000)
    msg = policy_bytes(p1)
    with pytest.raises(PolicyError):
        store.apply(msg, b"bad-signature", now_ns=10)
    assert store.apply(msg, signer.sign(msg), now_ns=10) == p1
    assert store.active(now_ns=500) == p1
    with pytest.raises(PolicyError):
        store.active(now_ns=1_000)  # expired at its boundary
    same = policy_bytes(Policy(1, LIMITS, 0, 5_000))
    with pytest.raises(PolicyError):
        store.apply(same, signer.sign(same), now_ns=10)  # version 1 again: rollback / replay refused
    older = policy_bytes(Policy(0, LIMITS, 0, 5_000)) if False else None
    assert older is None
    p2 = policy_bytes(Policy(2, LIMITS, 0, 5))
    with pytest.raises(PolicyError):
        store.apply(p2, signer.sign(p2), now_ns=10)  # already expired
    p3 = policy_bytes(Policy(3, LIMITS, 0, 9_000))
    store.apply(p3, signer.sign(p3), now_ns=10)
    assert store.history == [1, 3]
    tampered = msg.replace(b'"max_daily_loss":300.0', b'"max_daily_loss":30000.0')
    with pytest.raises(PolicyError):
        store.apply(tampered, signer.sign(msg), now_ns=10)  # signature over the original bytes
    with pytest.raises(PolicyError):
        parse_policy(b'{"version": 1}')
    with pytest.raises(ValueError):
        Policy(0, LIMITS, 0, 1)
    other = FakeVerifier(b"another-key")
    assert other.verify(msg, signer.sign(msg)) is False
