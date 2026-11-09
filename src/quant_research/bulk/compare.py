"""Bulk vs REST kline comparison (plan step 8 done-criterion "대조 샘플이 REST와 일치"). Pure."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

FIELDS = ("open", "high", "low", "close")
REST_INDEX = {"open": 1, "high": 2, "low": 3, "close": 4, "volume": 5}


@dataclass
class Comparison:
    compared: int = 0
    only_bulk: int = 0
    only_rest: int = 0
    mismatches: list[tuple[int, str, float, float]] = field(
        default_factory=lambda: list[tuple[int, str, float, float]]()
    )

    @property
    def ok(self) -> bool:
        return self.compared > 0 and not self.mismatches and not self.only_bulk and not self.only_rest


def compare_bars(
    bulk: Sequence[Mapping[str, object]],
    rest: Sequence[Sequence[object]],
    *,
    with_volume: bool,
    rel_tol: float = 1e-12,
) -> Comparison:
    """bulk rows carry event_ns (open time, ns) and float fields; REST rows are Binance kline arrays (open
    time ms, then string prices). Values must agree to `rel_tol` (both are the same decimal strings)."""
    fields = (*FIELDS, "volume") if with_volume else FIELDS
    by_open = {int(str(r["event_ns"])) // 1_000_000: r for r in bulk}
    rest_open = {int(str(r[0])): r for r in rest}
    out = Comparison(only_bulk=len(by_open.keys() - rest_open.keys()), only_rest=len(rest_open.keys() - by_open.keys()))
    for t in sorted(by_open.keys() & rest_open.keys()):
        out.compared += 1
        for name in fields:
            a = float(str(by_open[t][name]))
            b = float(str(rest_open[t][REST_INDEX[name]]))
            if abs(a - b) > rel_tol * max(abs(a), abs(b), 1e-300):
                out.mismatches.append((t, name, a, b))
    return out
