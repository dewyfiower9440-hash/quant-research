"""Compress closed raw segments (ADR-006; design v2.1: T0 raw is kept permanently, compressed).

A segment is eligible once its UTC hour has ended plus a grace period (writers close idle files at the
hour boundary, see SegmentWriter.sync_if_due). The .gz is written to a temporary name, fsynced,
decompressed and compared byte-for-byte with the original, then renamed; only after that is the original
removed. A file still locked by another process (Windows) is kept and retried on the next run.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

_NAME = re.compile(r"_(\d{8}T\d{2})_\d+\.jsonl$")
HOUR_NS = 3_600 * 10**9
_CHUNK = 4 * 1024 * 1024


@dataclass
class CompressResult:
    compressed: list[Path] = field(default_factory=lambda: list[Path]())
    skipped: list[tuple[Path, str]] = field(default_factory=lambda: list[tuple[Path, str]]())


def hour_end_ns(name: str) -> int | None:
    m = _NAME.search(name)
    if not m:
        return None
    start = datetime.strptime(m.group(1), "%Y%m%dT%H").replace(tzinfo=UTC)
    return int(start.timestamp()) * 10**9 + HOUR_NS


def _default_unlink(p: Path) -> None:
    p.unlink()


def compress_closed_segments(
    raw_root: Path,
    *,
    now_ns: int,
    min_age_s: float = 600,
    level: int = 6,
    unlink: Callable[[Path], None] = _default_unlink,
    fsync: Callable[[int], None] = os.fsync,
) -> CompressResult:
    if not 1 <= level <= 9:
        raise ValueError(f"gzip level must be 1..9, got {level}")
    result = CompressResult()
    for seg in sorted(raw_root.glob("*/*/*.jsonl")):
        if seg.parts[-3].startswith("_"):
            continue  # _gaps, _quarantine: small append-only logs, kept as text
        end = hour_end_ns(seg.name)
        if end is None or now_ns - end < min_age_s * 1e9:
            continue
        gz = seg.with_suffix(".jsonl.gz")
        tmp = seg.with_suffix(".jsonl.gz.tmp")
        source_digest = hashlib.sha256()
        # Streamed in chunks: a first-run trade backfill segment can exceed 1 GB (collector RAM budget 300 MB).
        with seg.open("rb") as src, tmp.open("wb") as raw_out:
            with gzip.GzipFile(fileobj=raw_out, mode="wb", compresslevel=level) as out:
                while chunk := src.read(_CHUNK):
                    source_digest.update(chunk)
                    out.write(chunk)
            raw_out.flush()
            fsync(raw_out.fileno())
        check = hashlib.sha256()
        with gzip.open(tmp, "rb") as back:
            while chunk := back.read(_CHUNK):
                check.update(chunk)
        if check.digest() != source_digest.digest():
            tmp.unlink()
            result.skipped.append((seg, "verification failed"))
            continue
        os.replace(tmp, gz)  # our own temp file; nobody reads .tmp
        try:
            unlink(seg)
        except PermissionError as e:
            result.skipped.append((seg, f"original still in use: {e}"))
            continue
        result.compressed.append(seg)
    return result
