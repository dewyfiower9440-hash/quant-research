"""T1 extra bulk data from data.binance.vision (design v2.1 §5.5 T1; phase-1 completion step 4).

- fundingRate (monthly): calc_time ms, interval hours, rate -> event data
- metrics (daily, 5m rows: OI, top-trader / global / taker ratios; history the 30-day REST API lacks).
  `create_time` is UTC text; whether it is the interval start or end is unknown (Phase 0), so the dataset
  is `unknown_interval` (read as start + 5 min + lag). Exact duplicate rows inside a file are dropped.
- bookDepth (daily, ±1..5 % depth and notional per ~30 s snapshot)
- spot klines (monthly, same format as futures klines; µs timestamps since 2025)

Daily files of one symbol-month are combined into ONE immutable lake file (hundreds of thousands of tiny
Parquet files would be unusable). Every source file is checksum-verified and recorded in `bulk_file`;
when a month gains or regenerates a file, the whole month is written again as a newer version
(`<symbol>-<month>-<newest LastModified>.parquet`) and the previous file stays as history.
"""

from __future__ import annotations

import hashlib
import io
import time
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx
import polars as pl

from quant_research.bulk.download import BulkTarget, DownloadResult, always_allowed, month_bounds_ns
from quant_research.bulk.klines import missing_minutes
from quant_research.catalog.store import Catalog
from quant_research.data.lake import write_frame

FIVE_MIN_NS = 300 * 10**9
Parser = Callable[[str], pl.DataFrame]


def _body(text: str) -> str:
    return text.split("\n", 1)[1] if text[:1].isalpha() else text


def parse_funding_csv(text: str) -> pl.DataFrame:
    raw = pl.read_csv(
        io.StringIO(_body(text)),
        has_header=False,
        schema=pl.Schema({"calc_time": pl.Int64, "funding_interval_hours": pl.Int64, "last_funding_rate": pl.Float64}),
    )
    return raw.select(
        (pl.col("calc_time") * 1_000_000).alias("event_ns"), "funding_interval_hours", "last_funding_rate"
    )


_METRIC_VALUES = (
    "sum_open_interest",
    "sum_open_interest_value",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
)


def _utc_text_ns(column: str) -> pl.Expr:
    return pl.col(column).str.strptime(pl.Datetime("ns", "UTC"), "%Y-%m-%d %H:%M:%S").dt.epoch("ns").alias("event_ns")


def parse_metrics_csv(text: str) -> pl.DataFrame:
    schema = {"create_time": pl.String, "symbol": pl.String, **dict.fromkeys(_METRIC_VALUES, pl.Float64)}
    raw = pl.read_csv(io.StringIO(_body(text)), has_header=False, schema=pl.Schema(schema))
    return raw.select(_utc_text_ns("create_time"), *_METRIC_VALUES).unique(maintain_order=True).sort("event_ns")


def parse_book_depth_csv(text: str) -> pl.DataFrame:
    # percentage is "-5" in 2023 files and "5.00" in 2026 files (pilot 2026-11-09): read as float
    schema = pl.Schema({"timestamp": pl.String, "percentage": pl.Float64, "depth": pl.Float64, "notional": pl.Float64})
    raw = pl.read_csv(io.StringIO(_body(text)), has_header=False, schema=schema)
    return raw.select(_utc_text_ns("timestamp"), "percentage", "depth", "notional").sort(
        ["event_ns", "percentage"], maintain_order=True
    )


def fetch_verified_csv(client: httpx.Client, url: str) -> tuple[str, str, int]:
    """(csv text, sha256, zip bytes) of one bulk zip whose `.CHECKSUM` matched; the zip stays in memory."""
    blob = client.get(url)
    blob.raise_for_status()
    check = client.get(url + ".CHECKSUM")
    check.raise_for_status()
    expected = check.text.split()[0].lower()
    digest = hashlib.sha256(blob.content).hexdigest()
    if digest != expected:
        raise ValueError(f"checksum mismatch: {digest} != {expected}")
    with zipfile.ZipFile(io.BytesIO(blob.content)) as z:
        return z.read(z.namelist()[0]).decode("utf-8"), digest, len(blob.content)


def download_month_group(
    group: list[BulkTarget],
    *,
    data_root: Path,
    catalog: Catalog,
    client: httpx.Client,
    parse: Parser,
    grid_ns: int | None,
    pause_s: float = 0.05,
    heavy_allowed: Callable[[], tuple[bool, str]] = always_allowed,
) -> DownloadResult:
    """All source files of one (dataset, symbol, month) -> one lake file. All-or-nothing: a failing file
    fails the month (nothing recorded), so a rerun retries it."""
    result = DownloadResult()
    if not group:
        return result
    first = group[0]
    have = {
        (str(r[0]), str(r[1]))
        for r in catalog.conn.execute(
            "select url, last_modified from bulk_file where dataset_id = ? and symbol = ? and month = ?",
            (first.dataset_id, first.symbol, first.month),
        )
    }
    if all((t.url, t.last_modified) in have for t in group):
        result.already_have = len(group)
        return result
    allowed, why = heavy_allowed()
    if not allowed:
        result.paused_reason = why
        return result
    frames: list[pl.DataFrame] = []
    fetched: list[tuple[BulkTarget, str, int, int]] = []
    for t in sorted(group, key=lambda x: x.url):
        try:
            text, digest, size = fetch_verified_csv(client, t.url)
            frame = parse(text)
        except (httpx.HTTPError, ValueError, zipfile.BadZipFile, pl.exceptions.PolarsError) as e:
            result.failed.append((t.url, f"{type(e).__name__}: {e}"))
            return result
        frames.append(frame)
        fetched.append((t, digest, size, frame.height))
        if pause_s:
            time.sleep(pause_s)
    month = pl.concat(frames, how="vertical_relaxed").unique(maintain_order=True).sort("event_ns")
    if month.height == 0:
        result.failed.append((first.url, "ValueError: no rows in any file of the month"))
        return result
    version = max(t.last_modified for t in group).replace(":", "").replace("-", "").replace(".", "")
    target = data_root / "lake" / first.dataset_id / first.month / f"{first.symbol}-{first.month}-{version}.parquet"
    write_frame(month, target, symbol=first.symbol)
    holes: list[tuple[int, int]] = []
    if grid_ns is not None:
        start_ns, end_ns = month_bounds_ns(first.month)
        stamps = month["event_ns"].unique().to_list()
        holes = missing_minutes(stamps, month_start_ns=start_ns, month_end_ns=end_ns, step_ns=grid_ns)
    now = time.time_ns()
    with catalog.transaction():
        for lo, hi in holes:
            catalog.conn.execute(
                "insert or ignore into gap(source, start_ns, end_ns, cause, detail, recorded_ns) values (?,?,?,?,?,?)",
                (first.dataset_id, lo, hi, "unknown", f"{first.symbol} missing rows in bulk {first.kind}", now),
            )
        for t, digest, size, rows in fetched:
            if (t.url, t.last_modified) in have:
                continue
            catalog.conn.execute(
                "insert into bulk_file(url, last_modified, dataset_id, symbol, month, sha256, zip_bytes, "
                "rows_written, lake_path, fetched_ns) values (?,?,?,?,?,?,?,?,?,?)",
                (t.url, t.last_modified, t.dataset_id, t.symbol, t.month, digest, size, rows, str(target), now),
            )
            result.downloaded += 1
            result.zip_bytes += size
    result.already_have = len(group) - result.downloaded
    result.rows = month.height
    result.gaps = len(holes)
    return result
