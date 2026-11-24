"""What the extras download does first, what it may skip and how far it is (v2.5 step 2, N02). Pure, except the
small `Checked` state file.

Order: the symbols a research window still waits for come first. Skipping: a symbol whose listed months were all
held at the last look is not listed again for a week. Progress: coverage of the wanted (symbol, month) pairs of
each research window, without the months the source does not publish."""

from __future__ import annotations

import json
import statistics
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

Window = tuple[str, str]  # [first month, end month) as YYYY-MM
# ADR-040 (seal epoch 2), the open window first: its cells are the ones running now
RESEARCH_WINDOWS: tuple[Window, Window] = (("2024-12", "2026-07"), ("2020-01", "2023-01"))
RECHECK_NS = 7 * 86_400 * 10**9  # a republished file of a recent month is found within this


def _inside(month: str, window: Window) -> bool:
    return window[0] <= month < window[1]


def order_symbols(wants: Mapping[str, Collection[str]], windows: Sequence[Window] = RESEARCH_WINDOWS) -> list[str]:
    """Most wanted months in the first window first, then in the next, then by name. The alphabetical order used
    before left every symbol after 'O' without data while cells of the open window waited for them."""

    def key(symbol: str) -> tuple[list[int], str]:
        return [-sum(1 for m in wants[symbol] if _inside(m, w)) for w in windows], symbol

    return sorted(wants, key=key)


def coverage(
    wants: Mapping[str, Collection[str]],
    have: Collection[tuple[str, str]],
    window: Window,
    *,
    unavailable: Collection[tuple[str, str]] = (),
) -> tuple[int, int]:
    """(wanted, held) (symbol, month) pairs of one window; pairs the source does not publish are not wanted."""
    wanted = {(s, m) for s, months in wants.items() for m in months if _inside(m, window)} - set(unavailable)
    return len(wanted), len(wanted & set(have))


def unavailable_months(wanted: Collection[str], *, listed: Collection[str]) -> list[str]:
    """Wanted months the source's listing does not contain (e.g. metrics before 2021-12 for most symbols)."""
    return sorted(set(wanted) - set(listed))


def slowing(new_per_tick: Sequence[int]) -> bool:
    """The last two ticks each brought fewer than half the median of the three before them. `after_exit` only
    asked whether a tick brought anything, so a download that fell from 806 new months a tick to 77 was never
    reported."""
    recent = list(new_per_tick)[-5:]
    if len(recent) < 5:
        return False
    usual = statistics.median(recent[:3])
    return usual > 0 and all(2 * n < usual for n in recent[3:])


@dataclass
class Checked:
    """run/bulk-extras-checked.json: when a (kind, symbol) was last seen complete and which wanted months the
    source did not list then. Kept between ticks so a resume does not ask S3 again for what it just saw."""

    path: Path
    now_ns: Callable[[], int] = time.time_ns
    entries: dict[str, dict[str, dict[str, object]]] = field(
        default_factory=lambda: dict[str, dict[str, dict[str, object]]]()
    )

    @classmethod
    def load(cls, path: Path, *, now_ns: Callable[[], int] = time.time_ns) -> Checked:
        try:
            raw: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            raw = {}
        entries: dict[str, dict[str, dict[str, object]]] = {}
        if isinstance(raw, dict):
            for kind, symbols in cast(dict[object, object], raw).items():
                if not isinstance(symbols, dict):
                    continue
                for symbol, entry in cast(dict[object, object], symbols).items():
                    if isinstance(entry, dict):
                        body = {str(k): v for k, v in cast(dict[object, object], entry).items()}
                        entries.setdefault(str(kind), {})[str(symbol)] = body
        return cls(path, now_ns, entries)

    def fresh(self, kind: str, symbol: str, wanted: Collection[str]) -> bool:
        """Seen complete within the last week for at least these months. What was wanted then is part of the mark:
        a new month, another universe file or a wider --since ask for the symbol again at once."""
        entry = self.entries.get(kind, {}).get(symbol, {})
        at, covered = entry.get("checked_ns"), entry.get("wanted")
        if not isinstance(at, int) or not isinstance(covered, list):
            return False
        if not 0 <= self.now_ns() - at < RECHECK_NS:
            return False
        return set(wanted) <= {str(m) for m in cast(list[object], covered)}

    def mark(self, kind: str, symbol: str, *, wanted: Collection[str], unavailable: Sequence[str]) -> None:
        self.entries.setdefault(kind, {})[symbol] = {
            "checked_ns": self.now_ns(),
            "wanted": sorted(wanted),
            "unavailable": list(unavailable),
        }

    def note_unavailable(self, kind: str, symbol: str, months: Sequence[str]) -> None:
        """Wanted months of a symbol the source lists nothing for. Not a mark: the symbol is asked again next
        time (one request), its months only leave the coverage."""
        self.entries.setdefault(kind, {})[symbol] = {"unavailable": list(months)}

    def unavailable(self, kind: str) -> set[tuple[str, str]]:
        out: set[tuple[str, str]] = set()
        for symbol, entry in self.entries.get(kind, {}).items():
            months = entry.get("unavailable")
            if isinstance(months, list):
                out |= {(symbol, str(m)) for m in cast(list[object], months)}
        return out

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.entries, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
