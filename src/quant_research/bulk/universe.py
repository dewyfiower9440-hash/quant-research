"""Point-in-time universe (plan step 8; design v2.1 §5.3, §5.5).

Membership for month m is the top-K by quote volume in month m-1, among every symbol that traded in
m-1, delisted ones included. Downloading the union of all members over time avoids survivorship bias;
`universe_membership` in the catalog keeps when each symbol was a member.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def is_usdt_perpetual(symbol: str) -> bool:
    return symbol.endswith("USDT") and "_" not in symbol


def pit_top_k(volumes: Mapping[str, Mapping[int, float]], *, k: int, months: Sequence[int]) -> dict[int, list[str]]:
    """{month: members}. Ties break by symbol name so the result is deterministic."""
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    ordered = sorted(months)
    out: dict[int, list[str]] = {}
    for i, month in enumerate(ordered):
        if i == 0:
            out[month] = []
            continue
        prev = ordered[i - 1]
        ranked = sorted(((sym, v[prev]) for sym, v in volumes.items() if prev in v), key=lambda sv: (-sv[1], sv[0]))
        out[month] = [sym for sym, _ in ranked[:k]]
    return out


def membership_spans(
    top: Mapping[int, Sequence[str]], *, months: Sequence[int]
) -> dict[str, list[tuple[int, int | None]]]:
    """Consecutive membership months merged into [from, to) spans; the open span ends with None."""
    ordered = sorted(months)
    spans: dict[str, list[tuple[int, int | None]]] = {}
    current: dict[str, int] = {}
    for month in ordered:
        members = set(top.get(month, []))
        for sym in sorted(set(current) - members):
            spans.setdefault(sym, []).append((current.pop(sym), month))
        for sym in sorted(members - set(current)):
            current[sym] = month
    for sym, start in current.items():
        spans.setdefault(sym, []).append((start, None))
    return dict(sorted(spans.items()))
