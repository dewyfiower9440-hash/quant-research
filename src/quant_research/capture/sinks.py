"""One SegmentWriter per source, sharing version, git SHA and the clock reference."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from quant_research.capture.clockref import ClockReference
from quant_research.capture.recovery import Gap, quarantine_tail, record_gap
from quant_research.capture.segment import SegmentWriter


@dataclass
class Sinks:
    raw_root: Path
    collector_version: str
    git_sha: str
    clock: ClockReference
    now_ns: Callable[[], int] = time.time_ns
    fsync: Callable[[int], None] = os.fsync
    gaps: list[Gap] = field(default_factory=lambda: list[Gap]())
    _writers: dict[str, SegmentWriter] = field(default_factory=lambda: dict[str, SegmentWriter](), repr=False)

    def write(self, source: str, payload: object) -> None:
        w = self._writers.get(source)
        if w is None:
            quarantine_tail(self.raw_root, source, self.fsync)
            w = SegmentWriter(
                root=self.raw_root,
                source=source,
                collector_version=self.collector_version,
                git_sha=self.git_sha,
                now_ns=self.now_ns,
                fsync=self.fsync,
            )
            self._writers[source] = w
        # The offset is always stored (it is most needed when large); suspicion is a separate flag.
        w.write(payload, offset_ms=self.clock.offset_ms, clock_suspect=self.clock.is_suspect(now_ns=self.now_ns()))

    def record_gap(self, gap: Gap) -> None:
        record_gap(self.raw_root, gap, self.fsync)
        self.gaps.append(gap)

    def sync_due(self) -> None:
        for w in self._writers.values():
            w.sync_if_due()

    def close(self) -> None:
        for w in self._writers.values():
            w.close()
