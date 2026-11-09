"""Bulk vs REST kline comparison (plan step 8 done-criterion)."""

from quant_research.bulk.compare import compare_bars

MIN_NS = 60 * 10**9


def _bulk(t: int, close: float = 1.5, volume: float = 10.0) -> dict[str, object]:
    return {"event_ns": t * MIN_NS, "open": 1.0, "high": 2.0, "low": 0.5, "close": close, "volume": volume}


def _rest(t: int, close: str = "1.50000000", volume: str = "10.000") -> list[object]:
    return [t * 60_000, "1.0", "2.0", "0.5", close, volume, t * 60_000 + 59_999, "0", 1, "0", "0", "0"]


def test_identical_bars_match() -> None:
    c = compare_bars([_bulk(0), _bulk(1)], [_rest(0), _rest(1)], with_volume=True)
    assert c.ok and c.compared == 2


def test_a_different_close_is_reported_with_its_values() -> None:
    c = compare_bars([_bulk(0, close=1.6)], [_rest(0)], with_volume=False)
    assert not c.ok and c.mismatches == [(0, "close", 1.6, 1.5)]


def test_volume_is_only_compared_when_asked() -> None:
    assert compare_bars([_bulk(0, volume=9.0)], [_rest(0)], with_volume=False).ok
    assert not compare_bars([_bulk(0, volume=9.0)], [_rest(0)], with_volume=True).ok


def test_bars_present_on_one_side_only_fail_the_comparison() -> None:
    c = compare_bars([_bulk(0), _bulk(1)], [_rest(0), _rest(2)], with_volume=False)
    assert (c.compared, c.only_bulk, c.only_rest, c.ok) == (1, 1, 1, False)


def test_an_empty_comparison_is_not_a_success() -> None:
    assert not compare_bars([], [], with_volume=False).ok
