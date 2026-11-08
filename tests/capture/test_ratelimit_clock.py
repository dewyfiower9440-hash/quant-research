"""Exchange rate-limit decisions and the clock reference (pure logic)."""

import pytest

from quant_research.capture.clockref import ClockReference
from quant_research.capture.ratelimit import (
    BannedError,
    GeoBlockedError,
    binance_delay_s,
    classify_status,
    parse_upbit_remaining,
    upbit_delay_s,
)


@pytest.mark.parametrize(
    ("status", "headers", "expected"),
    [
        (200, {}, ("ok", 0.0)),
        (429, {"Retry-After": "7"}, ("retry", 7.0)),
        (429, {}, ("retry", 60.0)),
        (500, {}, ("retry", 1.0)),
        (503, {}, ("retry", 1.0)),
        (404, {}, ("fail", 0.0)),
    ],
)
def test_classify_status_maps_http_outcomes(status: int, headers: dict[str, str], expected: tuple[str, float]) -> None:
    assert classify_status(status, headers, venue="binance", default_retry_s=60.0) == expected


def test_classify_status_418_is_an_ip_ban_that_stops_collection() -> None:
    with pytest.raises(BannedError, match="418"):
        classify_status(418, {"Retry-After": "120"}, venue="binance", default_retry_s=60.0)


def test_classify_status_451_is_a_geo_block() -> None:
    with pytest.raises(GeoBlockedError, match="451"):
        classify_status(451, {}, venue="binance", default_retry_s=60.0)


def test_classify_status_upbit_418_is_a_temporary_block_to_wait_out_not_a_ban() -> None:
    """Review #2: Upbit 418 must not stop the process (design §5.2: honour the stated block time)."""
    assert classify_status(418, {"Retry-After": "30"}, venue="upbit", default_retry_s=1.0) == ("retry", 30.0)
    assert classify_status(418, {}, venue="upbit", default_retry_s=1.0) == ("retry", 60.0)


def test_classify_status_upbit_451_is_also_a_geo_block() -> None:
    with pytest.raises(GeoBlockedError, match="upbit"):
        classify_status(451, {}, venue="upbit", default_retry_s=1.0)


def test_binance_delay_waits_for_next_minute_when_weight_near_limit() -> None:
    assert binance_delay_s(used_weight=100, limit=2400, now_s=1_000.0) == 0.0
    # 95 % used at second 30 of the minute -> wait for the window to roll (+0.5 s margin)
    assert binance_delay_s(used_weight=2280, limit=2400, now_s=60 * 100 + 30.0) == pytest.approx(30.5)


def test_binance_delay_without_header_does_not_wait() -> None:
    assert binance_delay_s(used_weight=None, limit=2400, now_s=5.0) == 0.0


def test_parse_upbit_remaining_reads_group_and_seconds_only() -> None:
    assert parse_upbit_remaining("group=candles; min=1795; sec=9") == ("candles", 9)
    assert parse_upbit_remaining("group=orderbook; sec=0") == ("orderbook", 0)
    assert parse_upbit_remaining(None) == (None, None)
    assert parse_upbit_remaining("garbage") == (None, None)


def test_upbit_delay_waits_to_next_second_when_budget_exhausted() -> None:
    assert upbit_delay_s(remaining_sec=5, now_s=10.25) == 0.0
    assert upbit_delay_s(remaining_sec=0, now_s=10.25) == pytest.approx(0.80)
    assert upbit_delay_s(remaining_sec=None, now_s=10.25) == 0.0


def test_clock_reference_keeps_lowest_rtt_sample_and_flags_large_or_stale_offsets() -> None:
    ref = ClockReference(max_abs_offset_ms=250.0, max_age_ns=300 * 10**9)
    assert ref.is_suspect(now_ns=0)  # never measured
    ref.update([(-16.0, 42), (-30.0, 90)], measured_ns=10**9)
    assert ref.offset_ms == -16.0
    assert ref.rtt_ms == 42
    assert not ref.is_suspect(now_ns=10**9 + 60 * 10**9)
    assert ref.is_suspect(now_ns=10**9 + 301 * 10**9)  # stale
    ref.update([(400.0, 40)], measured_ns=2 * 10**9)
    assert ref.is_suspect(now_ns=2 * 10**9)  # too large


def test_clock_reference_rejects_empty_sample_sets() -> None:
    with pytest.raises(ValueError, match="sample"):
        ClockReference().update([], measured_ns=1)
