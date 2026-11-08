"""Append-only raw JSONL segments (design v2.1 §3.1).

Each process writes its own files: one segment per source per UTC hour, named with the creation
time in ns so a restarted process never appends to an earlier (possibly torn) file. Every line is
flushed; fsync runs every N lines or T seconds (checked on each write and by `sync_if_due`, which the
collector calls every few seconds so sparse sources are durable too) and on rotation/close.
A failed write abandons the file so later records never follow a broken line.
The first line is a header with the collector version and git SHA.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

FORMAT_VERSION = 2  # v2: offset_ms always recorded, separate clock_suspect flag


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

    def write(self, payload: object, *, offset_ms: float | None, clock_suspect: bool = False) -> None:
        t = self.now_ns()
        hour = hour_key(t)
        try:
            if self._fh is None or hour != self._hour:
                self._open(t, hour)
            self._emit({"recv_ns": t, "offset_ms": offset_ms, "clock_suspect": clock_suspect, "payload": payload}, t)
        except OSError:
            self._abandon()
            raise

    def sync_if_due(self) -> None:
        """Time-based fsync for sources that write rarely (daily snapshots, last page of a burst)."""
        t = self.now_ns()
        if self._fh is not None and self._unsynced and t - self._last_sync_ns >= self.fsync_every_ns:
            try:
                self._sync(t)
            except OSError:
                self._abandon()
                raise

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._sync(self.now_ns())
            finally:
                self._fh.close()
                self._fh = None

    def _abandon(self) -> None:
        """Drop the handle after an I/O error; the next write starts a new file (never append after a torn line)."""
        fh, self._fh, self._unsynced = self._fh, None, 0
        if fh is not None:
            try:
                fh.close()
            except OSError:
                return  # the handle is unusable either way; the original write error is re-raised by the caller

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
