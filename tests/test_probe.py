"""Phase 0 measurement helpers (pure)."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from quant_research.domain.probe import (
    aligned_to_interval,
    clock_offset_ms,
    detect_epoch_unit,
    earliest_with_data,
    message_rate,
)


def test_clock_offset_symmetric_rtt_gives_server_minus_midpoint() -> None:
    offset, rtt = clock_offset_ms(local_send_ms=1_000, server_ms=1_120, local_recv_ms=1_200)
    assert rtt == 200
    assert offset == 20  # midpoint 1_100


def test_clock_offset_negative_rtt_is_rejected() -> None:
    with pytest.raises(ValueError, match="receive before send"):
        clock_offset_ms(local_send_ms=2_000, server_ms=1_500, local_recv_ms=1_999)


def test_message_rate_counts_per_second() -> None:
    assert message_rate(300, 30.0) == 10.0


@pytest.mark.parametrize("seconds", [0.0, -1.0])
def test_message_rate_non_positive_window_is_rejected(seconds: float) -> None:
    with pytest.raises(ValueError, match="window"):
        message_rate(1, seconds)


@pytest.mark.parametrize(
    ("value", "unit"),
    [
        (1_700_000_000, "s"),
        (1_700_000_000_000, "ms"),
        (1_700_000_000_000_000, "us"),
        (1_700_000_000_000_000_000, "ns"),
    ],
)
def test_detect_epoch_unit_by_digit_count(value: int, unit: str) -> None:
    assert detect_epoch_unit(value) == unit


@pytest.mark.parametrize("value", [0, 12345, 10**14, -1_700_000_000_000])
def test_detect_epoch_unit_ambiguous_values_are_rejected(value: int) -> None:
    with pytest.raises(ValueError, match="epoch"):
        detect_epoch_unit(value)


@given(st.integers(min_value=0, max_value=5_000), st.integers(min_value=0, max_value=5_000))
def test_earliest_with_data_finds_first_true_of_monotone_predicate(first: int, hi: int) -> None:
    calls: list[int] = []

    def has_data(day: int) -> bool:
        calls.append(day)
        return day >= first

    result = earliest_with_data(has_data, lo=0, hi=hi)
    if first > hi:
        assert result is None
    else:
        assert result == first
    assert len(calls) <= 2 + hi.bit_length() + 1  # logarithmic number of probes


def test_earliest_with_data_rejects_inverted_range() -> None:
    with pytest.raises(ValueError, match="lo"):
        earliest_with_data(lambda _: True, lo=5, hi=1)


def test_aligned_to_interval_true_only_when_every_stamp_is_a_multiple() -> None:
    five_min = 300_000
    assert aligned_to_interval([0, five_min, 2 * five_min], five_min)
    assert not aligned_to_interval([0, five_min + 1], five_min)
    assert aligned_to_interval([], five_min)


def test_aligned_to_interval_rejects_non_positive_interval() -> None:
    with pytest.raises(ValueError, match="interval"):
        aligned_to_interval([0], 0)
