"""One SegmentWriter per source, sharing version, git SHA and the clock reference."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from quant_research.capture.clockref import ClockReference
from quant_research.capture.recovery import Gap, recover_source
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
            gap = recover_source(self.raw_root, source, now_ns=self.now_ns(), fsync=self.fsync)
            if gap is not None:
                self.gaps.append(gap)
            w = SegmentWriter(
                root=self.raw_root,
                source=source,
                collector_version=self.collector_version,
                git_sha=self.git_sha,
                now_ns=self.now_ns,
                fsync=self.fsync,
            )
            self._writers[source] = w
        suspect = self.clock.is_suspect(now_ns=self.now_ns())
        w.write(payload, offset_ms=None if suspect else self.clock.offset_ms)

    def close(self) -> None:
        for w in self._writers.values():
            w.close()
