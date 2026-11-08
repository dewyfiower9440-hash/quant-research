"""Pure helpers for Phase 0 environment measurements (no I/O)."""

from collections.abc import Callable, Iterable
from typing import Literal

EpochUnit = Literal["s", "ms", "us", "ns"]

_UNIT_BY_DIGITS: dict[int, EpochUnit] = {10: "s", 13: "ms", 16: "us", 19: "ns"}


def clock_offset_ms(*, local_send_ms: int, server_ms: int, local_recv_ms: int) -> tuple[float, int]:
    """(offset, rtt) of a server clock against the local clock; offset = server - local midpoint.

    The error of the offset is bounded by rtt / 2 (asymmetric network paths).
    """
    rtt = local_recv_ms - local_send_ms
    if rtt < 0:
        raise ValueError(f"receive before send: send={local_send_ms} recv={local_recv_ms}")
    return server_ms - (local_send_ms + local_recv_ms) / 2, rtt


def message_rate(count: int, seconds: float) -> float:
    """Messages per second over a measurement window."""
    if seconds <= 0:
        raise ValueError(f"measurement window must be positive, got {seconds}")
    return count / seconds


def detect_epoch_unit(value: int) -> EpochUnit:
    """Unit of a Unix epoch timestamp from its digit count (Binance spot bulk switched ms -> us in 2025)."""
    unit = _UNIT_BY_DIGITS.get(len(str(value))) if value > 0 else None
    if unit is None:
        raise ValueError(f"cannot infer epoch unit for {value}")
    return unit


def earliest_with_data(has_data: Callable[[int], bool], *, lo: int, hi: int) -> int | None:
    """Smallest x in [lo, hi] with has_data(x), assuming has_data is monotone (False...False True...True).

    Returns None when even hi has no data. Uses O(log(hi - lo)) probes, so each probe may be a
    rate-limited network request.
    """
    if lo > hi:
        raise ValueError(f"lo ({lo}) must not exceed hi ({hi})")
    if not has_data(hi):
        return None
    while lo < hi:
        mid = (lo + hi) // 2
        if has_data(mid):
            hi = mid
        else:
            lo = mid + 1
    return lo


def aligned_to_interval(timestamps_ms: Iterable[int], interval_ms: int) -> bool:
    """True when every timestamp is an exact multiple of the interval (grid-aligned)."""
    if interval_ms <= 0:
        raise ValueError(f"interval must be positive, got {interval_ms}")
    return all(t % interval_ms == 0 for t in timestamps_ms)
