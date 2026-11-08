"""Startup recovery and gap records for raw segments (design v2.1 §3.1).

- A torn tail (partial line, NUL padding after a crash) is copied to `_quarantine/` together with the
  valid prefix length; the original segment is never modified and readers use only `valid_bytes`.
- Gaps are explicit records in `_gaps/<name>.jsonl`: process restarts (from the previous run's last
  heartbeat), WebSocket reconnects, and API history windows that were exceeded.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
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
    start_ns: int | None  # None: start unknown (no previous heartbeat)
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


def quarantine_tail(root: Path, source: str, fsync: Callable[[int], None] = os.fsync) -> SegmentState | None:
    """Inspect the previous segment of `source` (written by an earlier run of the same process, guaranteed
    by the instance lock) and copy a torn tail aside, recording the valid prefix length."""
    seg = latest_segment(root, source)
    if seg is None:
        return None
    state = inspect_segment(seg)
    if state.tail:
        q = root / "_quarantine" / f"{seg.name}.tail"
        if not q.exists():
            q.parent.mkdir(parents=True, exist_ok=True)
            q.write_bytes(state.tail)
            append_durable(
                root / "_quarantine" / "index.jsonl",
                {"segment": str(seg), "valid_bytes": state.valid_bytes, "tail_bytes": len(state.tail)},
                fsync,
            )
    return state


def record_gap(root: Path, gap: Gap, fsync: Callable[[int], None] = os.fsync) -> None:
    append_durable(root / "_gaps" / f"{gap.source}.jsonl", cast(dict[str, object], asdict(gap)), fsync)
