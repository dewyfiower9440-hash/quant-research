"""Plan step 11 (7-day validation) arithmetic. Pure: interval union/intersection and coverage."""

from __future__ import annotations

from collections.abc import Iterable

Span = tuple[int, int]


def merge_spans(spans: Iterable[tuple[int | None, int]], lo: int, hi: int) -> list[Span]:
    """Union of [start, end) spans clipped to [lo, hi); a span with unknown start (None) is dropped
    (it only marks the very first start of a process)."""
    clipped = sorted((max(s, lo), min(e, hi)) for s, e in spans if s is not None and min(e, hi) > max(s, lo))
    out: list[Span] = []
    for s, e in clipped:
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def intersect(a: list[Span], b: list[Span]) -> list[Span]:
    """Intersection of two merged span lists (silences seen by both 1 Hz sources = host-wide)."""
    out: list[Span] = []
    i = j = 0
    while i < len(a) and j < len(b):
        s, e = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if s < e:
            out.append((s, e))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def covered_fraction(lo: int, hi: int, missing: list[Span]) -> float:
    if hi <= lo:
        raise ValueError(f"empty validation window [{lo}, {hi})")
    return 1.0 - sum(e - s for s, e in missing) / (hi - lo)
