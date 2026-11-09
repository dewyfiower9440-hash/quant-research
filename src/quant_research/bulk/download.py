"""Bulk download of monthly 1m klines from data.binance.vision (plan step 8).

Per file: download zip + .CHECKSUM, verify sha256 in memory (the zip is never written to disk), parse,
QA against the 1-minute grid (missing bars become catalog gaps), write one immutable Parquet file to the
lake, record (url, last_modified) in `bulk_file`. A regenerated upstream file (new LastModified) is
stored as a new version. Thermal gate is checked before every file; pause_s throttles disk writes.
"""

from __future__ import annotations

import hashlib
import io
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from quant_research.bulk.klines import missing_minutes, parse_kline_csv
from quant_research.catalog.store import Catalog
from quant_research.data.lake import write_frame


@dataclass(frozen=True, slots=True)
class BulkTarget:
    dataset_id: str
    kind: str  # klines | markPriceKlines | indexPriceKlines | premiumIndexKlines
    symbol: str
    month: str  # YYYY-MM
    url: str
    size: int
    last_modified: str


@dataclass
class DownloadResult:
    downloaded: int = 0
    already_have: int = 0
    rows: int = 0
    zip_bytes: int = 0
    gaps: int = 0
    failed: list[tuple[str, str]] = field(default_factory=lambda: list[tuple[str, str]]())
    paused_reason: str | None = None


def _month_bounds_ns(month: str) -> tuple[int, int]:
    y, m = (int(x) for x in month.split("-"))
    start = datetime(y, m, 1, tzinfo=UTC)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=UTC)
    return int(start.timestamp()) * 10**9, int(end.timestamp()) * 10**9


def _always_allowed() -> tuple[bool, str]:
    return True, "no thermal gate"


def download_targets(
    targets: list[BulkTarget],
    *,
    data_root: Path,
    catalog: Catalog,
    client: httpx.Client,
    pause_s: float = 0.2,
    heavy_allowed: Callable[[], tuple[bool, str]] = _always_allowed,
) -> DownloadResult:
    result = DownloadResult()
    have = {(str(r[0]), str(r[1])) for r in catalog.conn.execute("select url, last_modified from bulk_file")}
    for t in targets:
        if (t.url, t.last_modified) in have:
            result.already_have += 1
            continue
        allowed, why = heavy_allowed()
        if not allowed:
            result.paused_reason = why
            return result
        try:
            blob = client.get(t.url)
            blob.raise_for_status()
            check = client.get(t.url + ".CHECKSUM")
            check.raise_for_status()
            expected = check.text.split()[0].lower()
            digest = hashlib.sha256(blob.content).hexdigest()
            if digest != expected:
                raise ValueError(f"checksum mismatch: {digest} != {expected}")
            with zipfile.ZipFile(io.BytesIO(blob.content)) as z:
                frame = parse_kline_csv(z.read(z.namelist()[0]).decode("utf-8"))
        except (httpx.HTTPError, ValueError, zipfile.BadZipFile) as e:
            result.failed.append((t.url, f"{type(e).__name__}: {e}"))
            continue
        version = t.last_modified.replace(":", "").replace("-", "").replace(".", "")
        target = data_root / "lake" / t.dataset_id / t.month / f"{t.symbol}-{t.month}-{version}.parquet"
        write_frame(frame.with_columns(), target, symbol=t.symbol)
        start_ns, end_ns = _month_bounds_ns(t.month)
        holes = (
            missing_minutes(frame["event_ns"].to_list(), month_start_ns=start_ns, month_end_ns=end_ns)
            if frame.height
            else [(start_ns, end_ns)]
        )
        now = time.time_ns()
        for lo, hi in holes:
            catalog.conn.execute(
                "insert or ignore into gap(source, start_ns, end_ns, cause, detail, recorded_ns) values (?,?,?,?,?,?)",
                (t.dataset_id, lo, hi, "unknown", f"{t.symbol} missing 1m bars in bulk file", now),
            )
        catalog.conn.execute(
            "insert into bulk_file(url, last_modified, dataset_id, symbol, month, sha256, zip_bytes, rows_written, "
            "lake_path, fetched_ns) values (?,?,?,?,?,?,?,?,?,?)",
            (
                t.url,
                t.last_modified,
                t.dataset_id,
                t.symbol,
                t.month,
                digest,
                len(blob.content),
                frame.height,
                str(target),
                now,
            ),
        )
        have.add((t.url, t.last_modified))
        result.downloaded += 1
        result.rows += frame.height
        result.zip_bytes += len(blob.content)
        result.gaps += len(holes)
        if pause_s:
            time.sleep(pause_s)
    return result
