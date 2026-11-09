"""The only module that reads or writes Parquet and raw segment bytes (design v2.1 §5.4; architecture test).

Layout: <data_root>/lake/<dataset>/<UTC day>/<segment stem>.parquet. Files are immutable and named after
the raw segment they came from, so a new compaction never touches a file a reader may hold open.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
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


def read_segment(path: Path) -> SegmentContent:
    """Records of a raw segment; for a plain .jsonl only the valid prefix (a torn tail is counted)."""
    raw = path.read_bytes()
    data = gzip.decompress(raw) if path.name.endswith(".gz") else raw
    records: list[RawRecord] = []
    pos = 0
    while True:
        nl = data.find(b"\n", pos)
        if nl < 0:
            break
        try:
            obj = cast(dict[str, object], json.loads(data[pos:nl]))
        except (ValueError, UnicodeDecodeError):
            break
        if "recv_ns" in obj:
            records.append(
                RawRecord(
                    recv_ns=int(cast(int, obj["recv_ns"])),
                    offset_ms=cast(float | None, obj.get("offset_ms")),
                    clock_suspect=bool(obj.get("clock_suspect", obj.get("offset_ms") is None)),
                    payload=obj.get("payload"),
                )
            )
        pos = nl + 1
    return SegmentContent(records, hashlib.sha256(raw).hexdigest(), len(data) - pos)


def lake_path(data_root: Path, dataset_id: str, stem: str, day: str) -> Path:
    return data_root / "lake" / dataset_id / day / f"{stem}.parquet"


def write_rows(rows: list[Row], target: Path) -> None:
    """Write once: temporary name, then rename to a name nobody has opened yet."""
    target.parent.mkdir(parents=True, exist_ok=True)
    frame = pl.from_dicts(rows, infer_schema_length=None)
    tmp = target.with_name(target.name + ".tmp")
    frame.write_parquet(tmp, compression="zstd", statistics=True)
    os.replace(tmp, target)


def write_frame(frame: pl.DataFrame, target: Path, *, symbol: str | None = None) -> None:
    """Write a typed frame once (bulk files): temporary name, then rename to a fresh name."""
    target.parent.mkdir(parents=True, exist_ok=True)
    out = frame.with_columns(pl.lit(symbol).alias("symbol")) if symbol is not None else frame
    tmp = target.with_name(target.name + ".tmp")
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


def load_dataset(data_root: Path, dataset_id: str) -> pl.DataFrame:
    """All compacted rows of a dataset. Bitemporal datasets collapse identical repeats to their first
    receipt and keep changed values under the same key as later versions."""
    files = sorted((data_root / "lake" / dataset_id).glob("*/*.parquet"))
    if dataset_id.startswith("binance.um.bulk."):
        files = _latest_bulk_versions(files)
    if not files:
        return pl.DataFrame()
    frame = pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")
    keys = VERSION_KEYS.get(dataset_id)
    if keys is None:
        return frame
    value_cols = [c for c in frame.columns if c not in {*keys, "received_ns", "offset_ms", "clock_suspect"}]
    return (
        frame.sort("received_ns")
        .unique(subset=[*keys, *value_cols], keep="first", maintain_order=True)
        .sort([*keys, "received_ns"])
    )
