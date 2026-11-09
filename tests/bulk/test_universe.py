"""Step 8: point-in-time top-K universe from monthly quote volume (survivorship-free, v2.1 §5.5)."""

import pytest

from quant_research.bulk.universe import is_usdt_perpetual, membership_spans, pit_top_k

JAN, FEB, MAR, APR = 1, 2, 3, 4


def test_month_m_is_ranked_by_month_m_minus_1_only() -> None:
    vol = {
        "A": {JAN: 100.0, FEB: 1.0, MAR: 1.0},
        "B": {JAN: 50.0, FEB: 200.0, MAR: 1.0},
        "C": {FEB: 150.0, MAR: 300.0},
    }
    top = pit_top_k(vol, k=2, months=[JAN, FEB, MAR, APR])
    assert top[JAN] == []  # nothing known before the first month
    assert top[FEB] == ["A", "B"]  # January volumes
    assert top[MAR] == ["B", "C"]  # February volumes
    assert top[APR] == ["C", "A"]  # March: C 300, then A/B tie at 1 -> name order


def test_delisted_symbols_stay_in_history_where_they_qualified() -> None:
    vol = {"LUNA": {JAN: 999.0}, "BTC": {JAN: 10.0, FEB: 10.0}}
    top = pit_top_k(vol, k=1, months=[JAN, FEB, MAR])
    assert top[FEB] == ["LUNA"] and top[MAR] == ["BTC"]
    spans = membership_spans(top, months=[JAN, FEB, MAR])
    assert spans == {"LUNA": [(FEB, MAR)], "BTC": [(MAR, None)]}


def test_membership_spans_merge_consecutive_months_and_split_gaps() -> None:
    top = {1: ["X"], 2: ["X"], 3: [], 4: ["X"]}
    assert membership_spans(top, months=[1, 2, 3, 4]) == {"X": [(1, 3), (4, None)]}


def test_k_must_be_positive() -> None:
    with pytest.raises(ValueError, match="k"):
        pit_top_k({}, k=0, months=[1])


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [("BTCUSDT", True), ("1000PEPEUSDT", True), ("BTCUSDT_260327", False), ("BTCUSDC", False), ("ETHBTC", False)],
)
def test_universe_is_usdt_perpetuals_only(symbol: str, expected: bool) -> None:
    assert is_usdt_perpetual(symbol) is expected
