"""Local clock offset against the exchange (design v2.1 §5.4, ADR-005).

`received_at` stays the raw local receive time; the offset measured here is stored next to it so it
can be corrected later. Rows recorded while the reference is suspect (never measured, stale, or
|offset| / rtt/2 above the threshold) are excluded from `observed` availability.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass
class ClockReference:
    max_abs_offset_ms: float = 250.0
    max_age_ns: int = 300 * 10**9
    offset_ms: float | None = None
    rtt_ms: int | None = None
    measured_ns: int | None = None

    def update(self, samples: Sequence[tuple[float, int]], *, measured_ns: int) -> None:
        """samples: (offset_ms, rtt_ms) pairs from one burst; the lowest-RTT sample is the most precise."""
        if not samples:
            raise ValueError("clock update needs at least one sample")
        offset, rtt = min(samples, key=lambda s: s[1])
        self.offset_ms, self.rtt_ms, self.measured_ns = offset, rtt, measured_ns

    def is_suspect(self, *, now_ns: int) -> bool:
        if self.offset_ms is None or self.rtt_ms is None or self.measured_ns is None:
            return True
        if now_ns - self.measured_ns > self.max_age_ns:
            return True
        return abs(self.offset_ms) > self.max_abs_offset_ms or self.rtt_ms / 2 > self.max_abs_offset_ms
