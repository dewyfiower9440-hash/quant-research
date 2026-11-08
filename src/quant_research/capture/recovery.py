"""Startup recovery for raw segments (design v2.1 §3.1).

A torn tail (partial line, NUL padding after a crash) is copied to `_quarantine/` and the original
segment is left untouched; readers use only `valid_bytes`. The span from the last durable record to
the restart is registered as a gap in `_gaps/<source>.jsonl` (imported into the catalog later).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast


@dataclass(frozen=True, slots=True)
class SegmentState:
    path: Path
    valid_bytes: int
    records: int
    last_recv_ns: int | None
    tail: bytes


@dataclass(frozen=True, slots=True)
class Gap:
    source: str
    start_ns: int
    end_ns: int
    cause: str


def inspect_segment(path: Path) -> SegmentState:
    """Valid prefix = complete, parseable lines up to the first broken one."""
    data = path.read_bytes()
    pos, records, last_recv = 0, 0, None
    while True:
        nl = data.find(b"\n", pos)
        if nl < 0:
            break
        try:
            obj: object = json.loads(data[pos:nl])
        except (ValueError, UnicodeDecodeError):
            break
        if not isinstance(obj, dict):
            break
        recv = cast(dict[str, object], obj).get("recv_ns")
        if isinstance(recv, int):
            records += 1
            last_recv = recv
        pos = nl + 1
    return SegmentState(path, pos, records, last_recv, data[pos:])


def latest_segment(root: Path, source: str) -> Path | None:
    files = sorted((root / source).glob("*/*.jsonl"), key=lambda p: p.name)
    return files[-1] if files else None


def append_durable(path: Path, obj: dict[str, object], fsync: Callable[[int], None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as fh:
        fh.write((json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8"))
        fh.flush()
        fsync(fh.fileno())


def recover_source(root: Path, source: str, *, now_ns: int, fsync: Callable[[int], None] = os.fsync) -> Gap | None:
    seg = latest_segment(root, source)
    if seg is None:
        return None
    state = inspect_segment(seg)
    if state.tail:
        q = root / "_quarantine" / f"{seg.name}.tail"
        if not q.exists():
            q.parent.mkdir(parents=True, exist_ok=True)
            q.write_bytes(state.tail)
    if state.last_recv_ns is None:
        return None
    gap = Gap(source, state.last_recv_ns, now_ns, "restart")
    append_durable(
        root / "_gaps" / f"{source}.jsonl",
        {"source": gap.source, "start_ns": gap.start_ns, "end_ns": gap.end_ns, "cause": gap.cause},
        fsync,
    )
    return gap
