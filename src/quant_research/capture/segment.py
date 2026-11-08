"""Append-only raw JSONL segments (design v2.1 §3.1).

Each process writes its own files: one segment per source per UTC hour, named with the creation
time in ns so a restarted process never appends to an earlier (possibly torn) file. Every line is
flushed; fsync runs every N lines or T seconds and on rotation/close, bounding loss on power cuts
or BSODs to that window. The first line is a header with the collector version and git SHA.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

FORMAT_VERSION = 1


def hour_key(t_ns: int) -> str:
    return datetime.fromtimestamp(t_ns / 1e9, tz=UTC).strftime("%Y%m%dT%H")


@dataclass
class SegmentWriter:
    root: Path
    source: str
    collector_version: str
    git_sha: str
    now_ns: Callable[[], int]
    fsync: Callable[[int], None] = os.fsync
    fsync_every_lines: int = 200
    fsync_every_ns: int = 2_000_000_000
    path: Path | None = field(default=None, init=False)
    _fh: BinaryIO | None = field(default=None, init=False, repr=False)
    _hour: str | None = field(default=None, init=False, repr=False)
    _unsynced: int = field(default=0, init=False, repr=False)
    _last_sync_ns: int = field(default=0, init=False, repr=False)

    def write(self, payload: object, *, offset_ms: float | None) -> None:
        t = self.now_ns()
        hour = hour_key(t)
        if self._fh is None or hour != self._hour:
            self._open(t, hour)
        line = {"recv_ns": t, "offset_ms": offset_ms, "payload": payload}
        self._emit(line, t)

    def close(self) -> None:
        if self._fh is not None:
            self._sync(self.now_ns())
            self._fh.close()
            self._fh = None

    def _open(self, t: int, hour: str) -> None:
        self.close()
        day = hour[:4] + "-" + hour[4:6] + "-" + hour[6:8]
        directory = self.root / self.source / day
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.source}_{hour}_{t}.jsonl"
        self._fh = path.open("xb")  # exclusive: never reuse an existing file
        self.path, self._hour = path, hour
        header = {
            "header": {
                "format": FORMAT_VERSION,
                "source": self.source,
                "collector_version": self.collector_version,
                "git_sha": self.git_sha,
                "created_ns": t,
                "pid": os.getpid(),
            }
        }
        self._emit(header, t)
        self._sync(t)

    def _emit(self, obj: Mapping[str, object], t: int) -> None:
        if self._fh is None:
            raise RuntimeError(f"segment for {self.source} is not open")
        self._fh.write((json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))
        self._fh.flush()
        self._unsynced += 1
        if self._unsynced >= self.fsync_every_lines or t - self._last_sync_ns >= self.fsync_every_ns:
            self._sync(t)

    def _sync(self, t: int) -> None:
        if self._fh is None or self._unsynced == 0:
            return
        self._fh.flush()
        self.fsync(self._fh.fileno())
        self._unsynced = 0
        self._last_sync_ns = t
