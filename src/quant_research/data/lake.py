"""The only module that reads or writes Parquet and raw segment bytes (design v2.1 §5.4; architecture test).

Layout: <data_root>/lake/<dataset>/<UTC day>/<segment stem>.parquet. Files are immutable and named after
the raw segment they came from, so a new compaction never touches a file a reader may hold open.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import polars as pl

from quant_research.normalize.rows import VERSION_KEYS, RawRecord, Row


@dataclass(frozen=True, slots=True)
class SegmentContent:
    records: list[RawRecord]
    sha256: str
    skipped_bytes: int


def segment_stem(path: Path) -> str:
    name = path.name
    return name[: -len(".jsonl.gz")] if name.endswith(".jsonl.gz") else name[: -len(".jsonl")]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SegmentStream:
    """Records of a raw segment, read line by line in constant memory (review #1).

    Only the valid prefix is yielded: the first line that is not newline-terminated or not valid JSON
    ends the records, and it plus everything after it is counted in `skipped_bytes` (a torn tail).
    `records` and `skipped_bytes` are final once iteration is exhausted; `sha256` is of the file bytes.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.sha256 = _file_sha256(path)
        self.records = 0
        self.skipped_bytes = 0

    def __iter__(self) -> Iterator[RawRecord]:
        gz = self.path.name.endswith(".gz")
        with gzip.open(self.path, "rb") if gz else self.path.open("rb") as fh:
            torn = False
            for line in fh:
                if not torn and line.endswith(b"\n"):
                    try:
                        obj = cast(dict[str, object], json.loads(line))
                    except (ValueError, UnicodeDecodeError):
                        torn = True
                    else:
                        if "recv_ns" in obj:
                            self.records += 1
                            yield RawRecord(
                                recv_ns=int(cast(int, obj["recv_ns"])),
                                offset_ms=cast(float | None, obj.get("offset_ms")),
                                clock_suspect=bool(obj.get("clock_suspect", obj.get("offset_ms") is None)),
                                payload=obj.get("payload"),
                            )
                        continue
                torn = True
                self.skipped_bytes += len(line)


def read_segment(path: Path) -> SegmentContent:
    """All records of a raw segment at once (small segments, tests); compaction uses SegmentStream."""
    stream = SegmentStream(path)
    records = list(stream)
    return SegmentContent(records, stream.sha256, stream.skipped_bytes)


def lake_path(data_root: Path, dataset_id: str, stem: str, day: str) -> Path:
    return data_root / "lake" / dataset_id / day / f"{stem}.parquet"


def rows_frame(rows: list[Row]) -> pl.DataFrame:
    """One batch of normalized rows as a columnar frame (the dicts can be dropped right after)."""
    return pl.from_dicts(rows, infer_schema_length=None)


def write_rows(rows: list[Row], target: Path) -> None:
    """Write once: temporary name, then rename to a name nobody has opened yet."""
    write_frames([rows_frame(rows)], target)


def write_frames(frames: list[pl.DataFrame], target: Path) -> None:
    """Concatenate batch frames (a column that was all-null in one batch takes its type from the others)
    and write once: temporary name, then rename to a name nobody has opened yet."""
    target.parent.mkdir(parents=True, exist_ok=True)
    frame = frames[0] if len(frames) == 1 else pl.concat(frames, how="diagonal_relaxed")
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")  # unique per writer process
    frame.write_parquet(tmp, compression="zstd", statistics=True)
    os.replace(tmp, target)


def write_frame(frame: pl.DataFrame, target: Path, *, symbol: str | None = None) -> None:
    """Write a typed frame once (bulk files): temporary name, then rename to a fresh name."""
    target.parent.mkdir(parents=True, exist_ok=True)
    out = frame.with_columns(pl.lit(symbol).alias("symbol")) if symbol is not None else frame
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")  # unique per writer process
    out.write_parquet(tmp, compression="zstd", statistics=True)
    os.replace(tmp, target)


def _latest_bulk_versions(files: list[Path]) -> list[Path]:
    """Bulk files are named <symbol>-<YYYY-MM>-<lastmodified>.parquet; older versions stay on disk as
    history (regenerated upstream files), the reader uses the newest per symbol and month."""
    latest: dict[str, Path] = {}
    for f in files:
        key, _, version = f.stem.rpartition("-")
        if key not in latest or version > latest[key].stem.rpartition("-")[2]:
            latest[key] = f
    return sorted(latest.values())


def bulk_files(data_root: Path, dataset_id: str, symbols: Iterable[str], months: Iterable[str]) -> list[Path]:
    """Newest version of each requested (symbol, month) bulk lake file that exists; nothing else is read."""
    wanted: list[Path] = []
    for month in sorted(set(months)):
        folder = data_root / "lake" / dataset_id / month
        for symbol in sorted(set(symbols)):
            wanted += sorted(folder.glob(f"{symbol}-{month}-*.parquet"))
    return _latest_bulk_versions(wanted)


def load_bulk(data_root: Path, dataset_id: str, symbols: Iterable[str], months: Iterable[str]) -> BulkRead:
    """Selected bulk files (research panels): rows plus the identity of what was read (path, size) so a
    backtest records exactly which file versions it used."""
    files = bulk_files(data_root, dataset_id, symbols, months)
    parts = [(f.relative_to(data_root).as_posix(), f.stat().st_size) for f in files]
    if not files:
        return BulkRead(pl.DataFrame(), parts)
    return BulkRead(pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed"), parts)


@dataclass(frozen=True, slots=True)
class BulkRead:
    frame: pl.DataFrame
    files: list[tuple[str, int]]


def read_lake_rows(path: Path, columns: Sequence[str], predicate: pl.Expr) -> pl.DataFrame:
    """Only the rows of one lake file that match `predicate` (pushed down to the Parquet scan, so a short
    window of a large file is read without materialising the whole file)."""
    return pl.scan_parquet(path).select(list(columns)).filter(predicate).collect()


def read_lake_file(path: Path, columns: Sequence[str] | None = None) -> pl.DataFrame:
    """One immutable lake file as written, for audits (bulk vs REST validation) and the paper feed (one
    file at a time, only the needed columns). Research code reads through the point-in-time API instead."""
    return pl.read_parquet(path, columns=list(columns) if columns is not None else None)


def load_dataset(data_root: Path, dataset_id: str) -> pl.DataFrame:
    """All compacted rows of a dataset. Bitemporal datasets collapse identical repeats to their first
    receipt and keep changed values under the same key as later versions."""
    files = sorted((data_root / "lake" / dataset_id).glob("*/*.parquet"))
    if dataset_id.startswith(("binance.um.bulk.", "binance.spot.bulk.")):
        files = _latest_bulk_versions(files)
    if not files:
        return pl.DataFrame()
    frame = pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")
    keys = VERSION_KEYS.get(dataset_id)
    if keys is None:
        return frame
    # live captures carry received_ns; REST backfills carry fetched_ns (they were not observed live)
    order = "received_ns" if "received_ns" in frame.columns else "fetched_ns"
    value_cols = [
        c for c in frame.columns if c not in {*keys, "received_ns", "fetched_ns", "offset_ms", "clock_suspect"}
    ]
    return frame.sort(order).unique(subset=[*keys, *value_cols], keep="first", maintain_order=True).sort([*keys, order])
