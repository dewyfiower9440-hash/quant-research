"""Tier T5 (v2.2 B4, design delta D9): Binance USDⓈ-M aggTrades -> 1m taker-flow aggregates.

A monthly aggTrades file of a large symbol is gigabytes of CSV, so the zip is streamed to disk (sha256 checked
against `.CHECKSUM` while it is written), its CSV member is read in chunks of `CHUNK_BYTES`, every chunk is
aggregated to minutes and the partial minutes are summed at the end; the zip is deleted afterwards (only the
aggregates are kept, in one immutable lake file per symbol-month, recorded in `bulk_file`).

Per minute (`event_ns` = minute start of `transact_time`): taker buy / sell base quantity and quote notional
(`is_buyer_maker` true means the taker sold), the number of aggregate trades, and the quote notional of
aggregate trades of at least `LARGE_QUOTE` per side. A minute without trades has no row (no trade is not
missing data). Stamps are milliseconds; files that switched to microseconds are detected by magnitude."""

from __future__ import annotations

import hashlib
import io
import os
import time
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx
import polars as pl

from quant_research.bulk.download import BulkTarget, DownloadResult, always_allowed
from quant_research.catalog.store import Catalog
from quant_research.data.lake import write_frame

AGG_FLOW_DATASET = "binance.um.bulk.agg_flow_1m"
LARGE_QUOTE = 100_000.0  # quote notional (USDT) of one aggregate trade counted as large
CHUNK_BYTES = 64 * 1024 * 1024
MINUTE_NS = 60 * 10**9
_MICROSECONDS_FROM = 10**14  # ms stamps are ~1.7e12; us stamps ~1.7e15
_COLUMNS = ("agg_trade_id", "price", "quantity", "first_trade_id", "last_trade_id", "transact_time", "is_buyer_maker")
_HEADER = b"agg_trade_id"


def parse_agg_chunk(raw: bytes) -> pl.DataFrame:
    """Complete CSV lines -> (ts_ns, price, quantity, taker_buy)."""
    if raw.startswith(_HEADER):
        raw = raw.split(b"\n", 1)[1] if b"\n" in raw else b""
    if not raw.strip():
        return pl.DataFrame(
            schema={"ts_ns": pl.Int64, "price": pl.Float64, "quantity": pl.Float64, "taker_buy": pl.Boolean}
        )
    frame = pl.read_csv(io.BytesIO(raw), has_header=False, infer_schema=False)
    if frame.width != len(_COLUMNS):
        raise ValueError(f"aggTrades rows have {frame.width} columns, expected {len(_COLUMNS)}")
    frame = frame.rename(dict(zip(frame.columns, _COLUMNS, strict=True)))
    frame = frame.with_columns(pl.all().str.strip_chars())  # CRLF files leave a carriage return on the last field
    frame = frame.filter(~pl.all_horizontal(pl.all().is_null() | (pl.all() == "")))  # blank lines
    needed = ("price", "quantity", "transact_time", "is_buyer_maker")
    if frame.select(pl.any_horizontal(pl.col(c).is_null() | (pl.col(c) == "") for c in needed).any()).item():
        raise ValueError("aggTrades row with a missing price, quantity, time or side")
    stamp = pl.col("transact_time").cast(pl.Int64)
    # per row: a file (or a chunk) may switch from milliseconds to microseconds
    ts_ns = pl.when(stamp >= _MICROSECONDS_FROM).then(stamp * 1_000).otherwise(stamp * 1_000_000)
    return frame.select(
        ts_ns.alias("ts_ns"),
        pl.col("price").cast(pl.Float64),
        pl.col("quantity").cast(pl.Float64),
        (pl.col("is_buyer_maker").str.to_lowercase() != "true").alias("taker_buy"),
    )


def aggregate_minutes(trades: pl.DataFrame) -> pl.DataFrame:
    quote = pl.col("price") * pl.col("quantity")
    buy, sell = pl.col("taker_buy"), ~pl.col("taker_buy")
    large = quote >= LARGE_QUOTE
    return (
        trades.group_by((pl.col("ts_ns") // MINUTE_NS * MINUTE_NS).alias("event_ns"))
        .agg(
            pl.col("quantity").filter(buy).sum().alias("buy_qty"),
            pl.col("quantity").filter(sell).sum().alias("sell_qty"),
            quote.filter(buy).sum().alias("buy_quote"),
            quote.filter(sell).sum().alias("sell_quote"),
            quote.filter(buy & large).sum().alias("large_buy_quote"),
            quote.filter(sell & large).sum().alias("large_sell_quote"),
            pl.len().cast(pl.Int64).alias("trades"),
        )
        .sort("event_ns")
    )


def flow_from_zip(path: Path, *, chunk_bytes: int = CHUNK_BYTES) -> pl.DataFrame:
    """Minutes of the zip's single CSV member, read `chunk_bytes` at a time (whole lines per chunk)."""
    parts: list[pl.DataFrame] = []
    with zipfile.ZipFile(path) as z:
        if not z.namelist():
            raise ValueError(f"empty zip {path.name}")
        name = z.namelist()[0]
    with zipfile.ZipFile(path) as z, z.open(name) as member:
        rest = b""
        while block := member.read(chunk_bytes):
            data = rest + block
            cut = data.rfind(b"\n")
            if cut < 0:
                rest = data
                continue
            rest = data[cut + 1 :]
            parts.append(aggregate_minutes(parse_agg_chunk(data[: cut + 1])))
        if rest.strip():
            parts.append(aggregate_minutes(parse_agg_chunk(rest)))
    if not parts:
        return aggregate_minutes(parse_agg_chunk(b""))
    combined = pl.concat(parts, how="vertical")
    return combined.group_by("event_ns").agg(pl.exclude("event_ns").sum()).sort("event_ns")


def fetch_verified_to_file(client: httpx.Client, url: str, target: Path) -> tuple[str, int]:
    """Stream one bulk zip to `target`; (sha256, bytes). A checksum mismatch removes the partial file and raises."""
    check = client.get(url + ".CHECKSUM")
    check.raise_for_status()
    fields = check.text.split()
    if not fields:
        raise ValueError(f"empty checksum file for {url}")
    expected = fields[0].lower()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex}.part")
    digest, size = hashlib.sha256(), 0
    try:
        with client.stream("GET", url) as response, tmp.open("wb") as out:
            response.raise_for_status()
            for chunk in response.iter_bytes(1 << 20):
                digest.update(chunk)
                out.write(chunk)
                size += len(chunk)
        if digest.hexdigest() != expected:
            raise ValueError(f"checksum mismatch for {url}: {digest.hexdigest()} != {expected}")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return digest.hexdigest(), size


def download_flow_month(
    target: BulkTarget,
    *,
    data_root: Path,
    catalog: Catalog,
    client: httpx.Client,
    work_dir: Path,
    heavy_allowed: Callable[[], tuple[bool, str]] = always_allowed,
    chunk_bytes: int = CHUNK_BYTES,
) -> DownloadResult:
    """One monthly aggTrades file -> one aggregated lake file. Already recorded (url, LastModified) = no-op;
    a failure records nothing, so a rerun retries the month."""
    result = DownloadResult()
    seen = catalog.conn.execute(
        "select 1 from bulk_file where url = ? and last_modified = ?", (target.url, target.last_modified)
    ).fetchone()
    if seen is not None:
        result.already_have = 1
        return result
    allowed, why = heavy_allowed()
    if not allowed:
        result.paused_reason = why
        return result
    if work_dir.exists():  # a killed run leaves its partial download; one folder per lock (pilot vs bulk)
        for stale in work_dir.glob("*.part"):
            stale.unlink(missing_ok=True)
    zip_path = work_dir / target.url.rsplit("/", 1)[-1]
    try:
        digest, size = fetch_verified_to_file(client, target.url, zip_path)
        minutes = flow_from_zip(zip_path, chunk_bytes=chunk_bytes)
    except (httpx.HTTPError, ValueError, zipfile.BadZipFile, pl.exceptions.PolarsError) as e:
        result.failed.append((target.url, f"{type(e).__name__}: {e}"))
        return result
    finally:
        zip_path.unlink(missing_ok=True)  # raw trades are not kept (D9-T5)
    if minutes.height == 0:
        result.failed.append((target.url, "ValueError: no trades in the month"))
        return result
    version = target.last_modified.replace(":", "").replace("-", "").replace(".", "")
    lake = data_root / "lake" / target.dataset_id / target.month / f"{target.symbol}-{target.month}-{version}.parquet"
    write_frame(minutes, lake, symbol=target.symbol)
    with catalog.transaction():
        catalog.conn.execute(
            "insert into bulk_file(url, last_modified, dataset_id, symbol, month, sha256, zip_bytes, "
            "rows_written, lake_path, fetched_ns) values (?,?,?,?,?,?,?,?,?,?)",
            (
                target.url,
                target.last_modified,
                target.dataset_id,
                target.symbol,
                target.month,
                digest,
                size,
                minutes.height,
                str(lake),
                time.time_ns(),
            ),
        )
    result.downloaded, result.zip_bytes, result.rows = 1, size, minutes.height
    return result
