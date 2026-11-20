"""Phase 2 steps 6-7: reference cases (hand-computed, both engines) and the engine cross-check.

Design v2.1 §13 Phase 2 done-criteria: reference cases 100 %, engines agree."""

import math
from dataclasses import replace

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from quant_research.backtest.costs import CostModel
from quant_research.backtest.engines import EngineUnsupported, run_event, run_vector
from quant_research.backtest.market import Market, Targets

NAN = math.nan
C0 = 10_000.0
FEE, HALF = 0.0005, 0.0003
R = FEE + HALF  # trade cost rate without impact (no volume data -> impact not modelled)
COST = CostModel(fee=FEE, half_spread=HALF)


def market(
    prices: list[list[float]],
    *,
    tradable: list[list[bool]] | None = None,
    low: list[list[float]] | None = None,
    high: list[list[float]] | None = None,
    mark_low: list[list[float]] | None = None,
    atr: float = NAN,
    events: dict[tuple[int, int], list[tuple[float, float]]] | None = None,
) -> Market:
    p = np.array(prices, dtype=np.float64)
    shape = p.shape
    ev = events or {}
    fund = np.zeros(shape)
    for (t, i), items in ev.items():
        fund[t, i] = sum(rate * mark / p[t, i] for rate, mark in items)
    return Market(
        decisions=np.arange(shape[0], dtype=np.int64),
        symbols=[f"S{i}" for i in range(shape[1])],
        price=p,
        tradable=np.ones(shape, dtype=bool) if tradable is None else np.array(tradable),
        high=p * 1.001 if high is None else np.array(high, dtype=np.float64),
        low=p * 0.999 if low is None else np.array(low, dtype=np.float64),
        mark_high=p * 1.001,
        mark_low=p * 0.999 if mark_low is None else np.array(mark_low, dtype=np.float64),
        atr=np.full(shape, atr),
        funding=fund,
        funding_events=ev,
        sigma_daily=np.full(shape, NAN),
        adv=np.full(shape, NAN),
        funding_known=bool(ev),
    )


def targets(weights: list[list[float]]) -> Targets:
    w = np.array(weights, dtype=np.float64)
    return Targets(weight=w, desire=np.sign(w).astype(np.int8))


def both(m: Market, t: Targets, cost: CostModel = COST, fidelity: str = "L1") -> tuple[np.ndarray, np.ndarray]:
    fid = "L1" if fidelity == "L1" else "L0"
    ev = run_event(m, t, cost, capital=C0, fidelity=fid, n_universe=1)
    vec = run_vector(m, t, cost, capital=C0, fidelity=fid, n_universe=1, stateless=True)
    assert not vec.liquidation_unmodelled and ev.liquidations == 0
    return ev.equity, vec.equity


def test_reference_long_with_fees_spread_and_rebalancing() -> None:
    prices = [[100.0], [110.0], [99.0], [99.0]]
    m, t = market(prices), targets([[1.0], [1.0], [1.0], [1.0]])
    # hand computation (quantity view)
    q0 = C0 / 100.0
    cash = -q0 * 100.0 - R * q0 * 100.0 + C0  # buy 100 units: -8 cash
    e1 = cash + q0 * 110.0
    q1 = e1 / 110.0
    cash += -(q1 - q0) * 110.0 - R * abs(q1 - q0) * 110.0
    e2 = cash + q1 * 99.0
    q2 = e2 / 99.0
    cash += -(q2 - q1) * 99.0 - R * abs(q2 - q1) * 99.0
    e3 = cash + q2 * 99.0
    final = cash + q2 * 99.0 - R * q2 * 99.0  # closed at the last decision
    expected = [C0, e1, e2, final]
    assert e1 == pytest.approx(10_992.0) and e3 == pytest.approx(e2 - R * abs(q2 - q1) * 99.0)
    ev, vec = both(m, t)
    assert ev.tolist() == pytest.approx(expected, rel=1e-12)
    assert vec.tolist() == pytest.approx(expected, rel=1e-12)


def test_reference_funding_longs_pay_and_shorts_receive_at_settlement() -> None:
    events = {(0, 0): [(0.001, 100.0)]}  # one settlement in interval 0 at mark 100
    for side in (1.0, -1.0):
        m, t = market([[100.0], [100.0]], events=events), targets([[side], [side]])
        q = side * C0 / 100.0
        flow = -q * 100.0 * 0.001  # long pays 10, short receives 10
        cost_in, cost_out = R * C0, R * abs(q) * 100.0
        expected_final = C0 - cost_in + flow - cost_out
        ev, vec = both(m, t)
        assert ev[-1] == pytest.approx(expected_final, rel=1e-12)
        assert vec[-1] == pytest.approx(expected_final, rel=1e-12)
        assert (ev[-1] < C0 - cost_in - cost_out) == (side > 0)


def test_reference_no_orders_on_missing_data_the_position_is_held() -> None:
    m = market([[100.0], [120.0], [120.0]], tradable=[[True], [False], [True]])
    t = targets([[1.0], [0.0], [0.0]])  # the exit signal at t=1 cannot be executed: data missing
    ev, vec = both(m, t)
    q = C0 / 100.0
    e1 = C0 - R * C0 + q * 20.0  # held through t=1 instead of exiting at 120
    final = e1 - R * q * 120.0  # exit at t=2
    assert ev.tolist() == pytest.approx([C0, e1, final], rel=1e-12)
    assert vec.tolist() == pytest.approx([C0, e1, final], rel=1e-12)
    worst = run_event(m, t, COST, capital=C0, fidelity="L1", n_universe=1).gap_loss
    assert worst.tolist() == pytest.approx([0.0, q * 120.0 * 0.002, 0.0])  # held 100 units at 120, range 0.2%


def test_reference_l0_has_no_costs_or_funding() -> None:
    m = market([[100.0], [110.0]], events={(0, 0): [(0.01, 100.0)]})
    ev, vec = both(m, targets([[1.0], [1.0]]), fidelity="L0")
    assert ev.tolist() == pytest.approx([C0, C0 * 1.1]) and vec.tolist() == pytest.approx([C0, C0 * 1.1])


def test_reference_time_stop_exits_and_blocks_reentry_until_the_signal_resets() -> None:
    prices = [[100.0]] * 7
    weights = [[1.0], [1.0], [1.0], [1.0], [0.0], [1.0], [1.0]]
    res = run_event(market(prices), targets(weights), COST, capital=C0, fidelity="L1", n_universe=1, time_stop_bars=2)
    assert res.stops == 1 and res.entries == 2  # exit at t=2 (held 2 bars), blocked at t=3, re-enter at t=5
    # entry (t0), rebalance to the target weight of the cost-reduced equity (t1), time-stop exit (t2),
    # re-entry (t5), final close (t6)
    assert res.trades == 5
    e1 = C0 - R * C0
    assert res.equity[1] == pytest.approx(e1) and res.equity[2] == pytest.approx(e1 - R * abs(e1 / 100 - 100) * 100)
    with pytest.raises(EngineUnsupported):
        run_vector(market(prices), targets(weights), COST, capital=C0, fidelity="L1", n_universe=1, stateless=False)


def test_reference_atr_stop_fills_at_the_stop_price() -> None:
    m = market([[100.0], [100.0], [100.0]], low=[[96.0], [99.0], [99.0]], atr=2.0)
    res = run_event(m, targets([[1.0], [1.0], [1.0]]), COST, capital=C0, fidelity="L1", n_universe=1, stop_loss_atr=1.5)
    q = C0 / 100.0
    after_stop = C0 - R * C0 + q * (97.0 - 100.0) - R * q * 97.0  # stop = 100 - 1.5 * 2 = 97
    assert res.stops == 1
    assert res.equity[1] == pytest.approx(after_stop, rel=1e-12)  # blocked at t=1 (same desire): stays flat
    no_atr = run_event(
        market([[100.0], [100.0]]),
        targets([[1.0], [1.0]]),
        COST,
        capital=C0,
        fidelity="L1",
        n_universe=1,
        stop_loss_atr=1.5,
    )
    assert no_atr.entries == 0 and no_atr.equity[-1] == C0  # cannot place the stop: no position


def test_reference_liquidation_on_the_interval_mark_low() -> None:
    cost = CostModel(fee=FEE, half_spread=HALF, maintenance_margin=0.01, liquidation_fee=0.005)
    m = market([[100.0], [60.0], [60.0]], mark_low=[[50.0], [59.0], [59.0]])
    res = run_event(m, targets([[2.0], [2.0], [2.0]]), cost, capital=C0, fidelity="L1", n_universe=1)
    q = 2.0 * C0 / 100.0
    liq = 100.0 * (1 - (1 / 2.0 - 0.01))  # 51
    after = C0 - R * 2 * C0 + q * (liq - 100.0) - 0.005 * q * liq
    assert res.liquidations == 1 and res.equity[1] == pytest.approx(after, rel=1e-12)
    vec = run_vector(m, targets([[2.0], [2.0], [2.0]]), cost, capital=C0, fidelity="L1", n_universe=1, stateless=True)
    assert vec.liquidation_unmodelled  # the vector engine says so instead of guessing


@st.composite
def scenarios(draw: st.DrawFn) -> tuple[Market, Targets]:
    t_len = draw(st.integers(2, 40))
    n = draw(st.integers(1, 4))
    steps = draw(st.lists(st.lists(st.floats(-0.1, 0.1), min_size=n, max_size=n), min_size=t_len, max_size=t_len))
    prices = [[100.0 * (1 + 0.0) for _ in range(n)]]
    for row in steps[1:]:
        prices.append([p * (1 + r) for p, r in zip(prices[-1], row, strict=True)])
    weights = draw(st.lists(st.lists(st.floats(-0.5, 0.5), min_size=n, max_size=n), min_size=t_len, max_size=t_len))
    weights = [[0.0 if abs(w) < 0.05 else w for w in row] for row in weights]
    tradable = draw(st.lists(st.lists(st.booleans(), min_size=n, max_size=n), min_size=t_len, max_size=t_len))
    events: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for t in range(t_len - 1):
        for i in range(n):
            if draw(st.booleans()):
                events[(t, i)] = [(draw(st.floats(-0.003, 0.003)), prices[t][i] * draw(st.floats(0.98, 1.02)))]
    m = market(prices, tradable=tradable, events=events)
    return m, Targets(weight=np.array(weights), desire=np.sign(np.array(weights)).astype(np.int8))


@settings(max_examples=300, deadline=None)
@given(scenarios())
def test_engines_agree_on_every_stateless_scenario(case: tuple[Market, Targets]) -> None:
    m, t = case
    ev = run_event(m, t, COST, capital=C0, fidelity="L1", n_universe=1)
    vec = run_vector(m, t, COST, capital=C0, fidelity="L1", n_universe=1, stateless=True)
    assert ev.liquidations == 0 and not vec.liquidation_unmodelled  # |w| <= 0.5: no liquidation possible
    np.testing.assert_allclose(ev.equity, vec.equity, rtol=1e-9, atol=1e-9)
    assert ev.costs == pytest.approx(vec.costs, rel=1e-9, abs=1e-9)
    assert ev.funding == pytest.approx(vec.funding, rel=1e-9, abs=1e-9)
    np.testing.assert_allclose(ev.gap_loss, vec.gap_loss, rtol=1e-9, atol=1e-9)


def test_reference_rebalance_every_k_bars_holds_the_drifted_position_in_between() -> None:
    """v2.2 B5 (D5): with rebalance_bars = 2 orders go out on bars 0 and 2 only; bar 1 holds what bar 0 bought
    (no drift-back trade) and its target change is ignored."""
    prices = [[100.0], [110.0], [99.0], [99.0]]
    m, t = market(prices), targets([[1.0], [0.5], [1.0], [1.0]])
    q0 = C0 / 100.0
    cash = C0 - q0 * 100.0 - R * q0 * 100.0
    e1 = cash + q0 * 110.0  # bar 1: no trade, the target 0.5 is ignored
    e2 = cash + q0 * 99.0
    q2 = e2 / 99.0  # bar 2: rebalance to weight 1
    cash += -(q2 - q0) * 99.0 - R * abs(q2 - q0) * 99.0
    final = cash + q2 * 99.0 - R * q2 * 99.0
    for engine in ("event", "vector"):
        if engine == "event":
            res = run_event(m, t, COST, capital=C0, fidelity="L1", n_universe=1, rebalance_bars=2)
        else:
            res = run_vector(m, t, COST, capital=C0, fidelity="L1", n_universe=1, stateless=True, rebalance_bars=2)
        assert res.equity.tolist() == pytest.approx([C0, e1, e2, final], rel=1e-12)
        assert res.trades == 3  # buy, rebalance, close


def test_reference_a_same_side_resize_below_the_band_is_skipped() -> None:
    """v2.2 B5: min_trade_weight 0.2: the drift from 1.0 after +10 % is tiny next to the band, so bar 1 does not
    trade; a flip or an exit always trades."""
    prices = [[100.0], [110.0], [99.0], [99.0]]
    m, t = market(prices), targets([[1.0], [0.9], [-1.0], [-1.0]])  # the last bar closes everything
    for engine in ("event", "vector"):
        if engine == "event":
            res = run_event(m, t, COST, capital=C0, fidelity="L1", n_universe=1, min_trade_weight=0.2)
        else:
            res = run_vector(m, t, COST, capital=C0, fidelity="L1", n_universe=1, stateless=True, min_trade_weight=0.2)
        assert res.trades == 3  # buy, (no resize at 0.9), flip to short, close
        q0 = C0 / 100.0
        assert res.equity[1] == pytest.approx(C0 - R * q0 * 100.0 + q0 * 10.0, rel=1e-12)


@settings(max_examples=300, deadline=None)
@given(scenarios(), st.integers(1, 5), st.sampled_from([0.0, 0.05, 0.2]))
def test_engines_agree_with_a_rebalance_schedule_and_a_no_trade_band(
    case: tuple[Market, Targets], every: int, band: float
) -> None:
    m, t = case
    ev = run_event(m, t, COST, capital=C0, fidelity="L1", n_universe=1, rebalance_bars=every, min_trade_weight=band)
    vec = run_vector(
        m, t, COST, capital=C0, fidelity="L1", n_universe=1, stateless=True, rebalance_bars=every, min_trade_weight=band
    )
    np.testing.assert_allclose(ev.equity, vec.equity, rtol=1e-9, atol=1e-9)
    assert ev.costs == pytest.approx(vec.costs, rel=1e-9, abs=1e-9)
    assert ev.trades == vec.trades


def test_invalid_rebalance_settings_are_refused() -> None:
    m, t = market([[100.0], [101.0]]), targets([[1.0], [1.0]])
    with pytest.raises(ValueError, match="rebalance"):
        run_vector(m, t, COST, capital=C0, fidelity="L1", n_universe=1, stateless=True, rebalance_bars=0)
    with pytest.raises(ValueError, match="band"):
        run_event(m, t, COST, capital=C0, fidelity="L1", n_universe=1, min_trade_weight=-0.1)


def test_a_time_stop_fires_between_rebalances_even_when_the_signal_has_changed() -> None:
    """B5 review 2: the time stop required desire == side; with k > 1 a signal that turned off on a
    non-rebalance bar skipped the stop and the position was held until the next rebalance."""
    prices = [[100.0]] * 7
    m = market(prices)
    w = [[1.0], [1.0], [0.0], [0.0], [0.0], [0.0], [0.0]]  # the signal turns off at bar 2
    res = run_event(m, targets(w), COST, capital=C0, fidelity="L1", n_universe=1, time_stop_bars=2, rebalance_bars=4)
    assert res.stops == 1 and res.position_bars == 2  # stopped at bar 2, not held to the bar-4 rebalance
    unchanged = run_event(
        m, targets([[1.0]] * 7), COST, capital=C0, fidelity="L1", n_universe=1, time_stop_bars=2, rebalance_bars=1
    )
    assert unchanged.stops >= 1 and unchanged.position_bars == 2  # k = 1 behaves as before


def test_the_rebalance_phase_is_anchored_to_absolute_time_not_to_the_first_row() -> None:
    """B5 review 3: `t % k` tied the schedule to the first row (warm-up included), so selection, confirmation,
    holdout slices and paper rebalanced on different bars. The phase is now (decision // bar) % k."""
    prices = [[100.0 + t] for t in range(8)]
    base = market(prices)
    shifted = replace(base, decisions=base.decisions + 1)  # the same bars, the panel starts one bar later
    w = targets([[1.0], [0.5], [1.0], [0.5], [1.0], [0.5], [1.0], [0.5]])
    a = run_vector(base, w, COST, capital=C0, fidelity="L1", n_universe=1, stateless=True, rebalance_bars=2)
    b = run_vector(shifted, w, COST, capital=C0, fidelity="L1", n_universe=1, stateless=True, rebalance_bars=2)
    assert a.trades != b.trades or not np.allclose(a.equity, b.equity)  # rows 0, 2, .. vs 1, 3, .. rebalance
    ev = run_event(shifted, w, COST, capital=C0, fidelity="L1", n_universe=1, rebalance_bars=2)
    np.testing.assert_allclose(ev.equity, b.equity, rtol=1e-12)  # both engines use the same anchor
