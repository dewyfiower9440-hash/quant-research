"""Phase 2 step 5: cost model formulas (hand-computed cases) and invariants."""

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from quant_research.backtest.costs import (
    FEES,
    CostModel,
    abdi_ranaldo,
    corwin_schultz,
    funding_payment,
    liquidation_price,
    measured_half_spread,
)


def test_fee_priors_are_the_conservative_defaults() -> None:
    assert FEES[("binance", "usdm_futures")].taker == 0.0005
    assert FEES[("binance", "spot")].taker == 0.001
    assert FEES[("upbit", "spot")].taker == 0.0005


def test_trade_cost_is_fee_plus_half_spread_plus_square_root_impact_times_stress() -> None:
    m = CostModel(fee=0.0005, half_spread=0.0002, impact_y=0.5, impact_k=2.0)
    # impact = 0.5 * 2 * 0.04 * sqrt(1e4 / 1e8) = 0.0004
    assert m.rate(10_000, 0.04, 1e8) == pytest.approx(0.0005 + 0.0002 + 0.0004)
    assert m.stressed(3).rate(10_000, 0.04, 1e8) == pytest.approx(3 * 0.0011)
    assert m.rate(10_000, None, None) == pytest.approx(0.0007)  # no volume data: impact not modelled


@given(st.floats(1, 1e7), st.floats(1, 3))
def test_cost_rate_never_falls_when_size_or_stress_grows(notional: float, stress: float) -> None:
    m = CostModel()
    assert m.rate(notional * 2, 0.03, 1e8) >= m.rate(notional, 0.03, 1e8)
    assert m.stressed(stress).rate(notional, 0.03, 1e8) >= m.rate(notional, 0.03, 1e8)


def test_corwin_schultz_hand_case_and_zero_floor() -> None:
    h1, l1, h2, l2 = 101.0, 99.0, 101.5, 99.5
    beta = math.log(h1 / l1) ** 2 + math.log(h2 / l2) ** 2
    gamma = math.log(101.5 / 99.0) ** 2
    k = 3 - 2 * math.sqrt(2)
    alpha = (math.sqrt(2 * beta) - math.sqrt(beta)) / k - math.sqrt(gamma / k)
    expected = max(0.0, 2 * (math.exp(alpha) - 1) / (1 + math.exp(alpha)))
    assert corwin_schultz(h1, l1, h2, l2) == pytest.approx(expected)
    assert corwin_schultz(110.0, 90.0, 130.0, 120.0) == 0.0  # large overnight move: negative estimate floored


def test_abdi_ranaldo_recovers_a_bid_ask_bounce() -> None:
    # mid fixed at 100, closes alternate on bid / ask of a 1% spread, ranges symmetric around the mid
    closes = [99.5, 100.5] * 50
    highs, lows = [100.5] * 100, [99.5] * 100
    # eta is constant, so every term is d^2 with d = ln(100.5 / 99.5) / 2 and S = 2d = ln(100.5 / 99.5) ~ 1%
    assert abdi_ranaldo(closes, highs, lows) == pytest.approx(math.log(100.5 / 99.5))
    trending = abdi_ranaldo([100.5] * 10, [100.5] * 10, [99.5] * 10)  # always at the ask
    assert trending == pytest.approx(2 * math.sqrt((math.log(100.5) - (math.log(100.5) + math.log(99.5)) / 2) ** 2))
    with pytest.raises(ValueError, match=">= 2 bars"):
        abdi_ranaldo([1.0], [1.0], [1.0])


def test_measured_half_spread_is_the_median_of_valid_quotes() -> None:
    assert measured_half_spread([99.0, 99.9, 50.0], [101.0, 100.1, 40.0]) == pytest.approx(
        (0.01 + 0.001) / 2
    )  # crossed quote (ask < bid) ignored; median of 1% and 0.1% half spreads
    with pytest.raises(ValueError, match="no valid"):
        measured_half_spread([2.0], [1.0])


def test_funding_sign_longs_pay_positive_rates_shorts_receive() -> None:
    assert funding_payment(2.0, 100.0, 0.0001) == pytest.approx(-0.02)
    assert funding_payment(-2.0, 100.0, 0.0001) == pytest.approx(0.02)
    assert funding_payment(2.0, 100.0, -0.0001) == pytest.approx(0.02)


def test_liquidation_price_for_isolated_long_and_short() -> None:
    assert liquidation_price(100.0, 1, 2.0, 0.01) == pytest.approx(51.0)  # 50% move minus 1% maintenance
    assert liquidation_price(100.0, -1, 2.0, 0.01) == pytest.approx(149.0)
    with pytest.raises(ValueError, match="side"):
        liquidation_price(100.0, 0, 2.0, 0.01)
