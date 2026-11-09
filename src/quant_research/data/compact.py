"""Compaction: closed raw segments -> immutable Parquet, recorded in the catalog (plan step 7).

Eligible: segments of sources that have a normalizer (snapshots stay raw), whose UTC hour ended at
least a minute ago, not yet in `compacted_segment`. A segment whose normalizer fails is reported and
retried on the next run (never marked done).

Memory (review #1): segments are streamed line by line and normalized in batches of BATCH_ROWS rows,
each batch turned into a columnar frame at once. Failures are isolated per segment (review #2).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl
from polars.exceptions import PolarsError

from quant_research.capture.compress import hour_end_ns
from quant_research.catalog.store import Catalog
from quant_research.data.lake import SegmentStream, lake_path, rows_frame, segment_stem, write_frames
from quant_research.normalize.rows import NORMALIZERS, Row, normalize

BATCH_ROWS = 100_000  # row dicts alive at once during normalization


@dataclass
class CompactResult:
    compacted: int = 0
    already_done: int = 0
    rows: int = 0
    skipped_bytes: int = 0
    vanished: int = 0  # listed, then replaced by its .gz before it was read
    failed: list[tuple[str, str]] = field(default_factory=lambda: list[tuple[str, str]]())


def _eligible(raw_root: Path, now_ns: int) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for dataset_id in sorted(NORMALIZERS):
        for path in sorted((raw_root / dataset_id).glob("*/*.jsonl*")):
            if path.name.endswith(".tmp"):
                continue
            end = hour_end_ns(path.name.removesuffix(".gz"))
            if end is not None and end + 60 * 10**9 <= now_ns:
                out.append((dataset_id, path))
    return out


def compact_once(data_root: Path) -> CompactResult:
    """Open the catalog, compact, close: safe to call from a worker thread (SQLite objects stay local)."""
    catalog = Catalog.open(data_root / "meta" / "catalog.db")
    try:
        return compact_closed_segments(data_root, catalog, now_ns=time.time_ns())
    finally:
        catalog.close()


def main() -> None:
    from quant_research.config import load_settings

    result = compact_once(load_settings().data_root)
    print(  # CLI output
        f"compacted={result.compacted} already_done={result.already_done} rows={result.rows} "
        f"skipped_bytes={result.skipped_bytes} failed={len(result.failed)}"
    )
    for path, why in result.failed:
        print(f"FAILED {path}: {why}")


def _normalized_frames(dataset_id: str, stream: SegmentStream, batch_rows: int) -> tuple[list[pl.DataFrame], int]:
    """Normalize in batches of about `batch_rows`; only one batch of row dicts is alive at a time."""
    frames: list[pl.DataFrame] = []
    batch: list[Row] = []
    total = 0
    for rec in stream:
        batch.extend(normalize(dataset_id, rec))
        if len(batch) >= batch_rows:
            frames.append(rows_frame(batch))
            total += len(batch)
            batch = []
    if batch:
        frames.append(rows_frame(batch))
        total += len(batch)
    return frames, total


def compact_closed_segments(
    data_root: Path, catalog: Catalog, *, now_ns: int, batch_rows: int = BATCH_ROWS
) -> CompactResult:
    result = CompactResult()
    done = {str(r[0]) for r in catalog.conn.execute("select stem from compacted_segment")}
    for dataset_id, path in _eligible(data_root / "raw", now_ns):
        stem = segment_stem(path)
        if stem in done:
            result.already_done += 1
            continue
        try:
            stream = SegmentStream(path)
            frames, n_rows = _normalized_frames(dataset_id, stream, batch_rows)
            target = lake_path(data_root, dataset_id, stem, path.parent.name) if n_rows else None
            if target is not None:
                write_frames(frames, target)
            del frames
            catalog.conn.execute(
                "insert into compacted_segment(stem, dataset_id, segment_sha256, records, rows_written, "
                "skipped_bytes, lake_path, compacted_ns) values (?,?,?,?,?,?,?,?)",
                (
                    stem,
                    dataset_id,
                    stream.sha256,
                    stream.records,
                    n_rows,
                    stream.skipped_bytes,
                    str(target) if target else None,
                    time.time_ns(),
                ),
            )
        except FileNotFoundError:
            # compress replaced the .jsonl by .jsonl.gz after listing; the .gz is compacted next run
            result.vanished += 1
            continue
        except (
            KeyError,
            ValueError,
            TypeError,
            AttributeError,
            EOFError,
            OSError,
            MemoryError,
            sqlite3.Error,
            PolarsError,
        ) as e:
            # one bad segment must not block every later segment and source (review #2); retried next run
            result.failed.append((str(path), f"{type(e).__name__}: {e}"))
            continue
        done.add(stem)
        result.compacted += 1
        result.rows += n_rows
        result.skipped_bytes += stream.skipped_bytes
    return result


if __name__ == "__main__":
    main()
