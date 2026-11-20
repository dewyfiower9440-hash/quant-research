"""Tier T7 (v2.2 B4, design delta D9): free, key-less external sources (docs/measurements/b4-t7-sources.md).

- Alternative.me Crypto Fear & Greed (daily since 2018-02, one call): dataset `alternative.fng_1d`.
- Coin Metrics Community asset metrics (daily, CC BY-NC 4.0, paged): dataset `coinmetrics.asset_1d`.
- Deribit DVOL volatility index (hourly since 2021-03-24, paged backwards): dataset `deribit.dvol_1h`.

Publication times of the daily values are not documented, so the catalog registers conservative lags
(catalog/bootstrap.py `EXTERNAL_DATASETS`). The fetched history is written as one lake file per (symbol, month)
like the bulk data; a month is written again (a newer version, the old file stays as history) only when its
content changed, and every written file is recorded in `bulk_file` (url = the request, last_modified = the
fetch time). Every request has an explicit timeout (the caller's client) and paging is bounded."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
import polars as pl

from quant_research.catalog.store import Catalog
from quant_research.data.lake import bulk_files, read_lake_file, write_frame

FNG_DATASET = "alternative.fng_1d"
COINMETRICS_DATASET = "coinmetrics.asset_1d"
DVOL_DATASET = "deribit.dvol_1h"
FNG_URL = "https://api.alternative.me/fng/?limit=0&format=json"
COINMETRICS_URL = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
DVOL_URL = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
# Exchange flows (FlowIn/OutEx*) arrive as provisional "flash" values revised later: a backfill fetched today would
# hold the revised vintage as if it was known at D+2, so they are not fetched (B4 review).
COINMETRICS_METRICS = ("PriceUSD", "CapMrktCurUSD", "CapMVRVCur", "AdrActCnt", "TxCnt")
MAX_PAGES = 1_000  # paging bound: far above the real history (DVOL ~ 50 pages, Coin Metrics ~ 1 per asset)
HOUR_MS = 3_600_000


class ExternalSourceError(ValueError):
    """A source answered with something that is not the documented shape."""


def _obj(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ExternalSourceError(f"{what}: expected an object, got {type(value).__name__}")
    return cast(dict[str, object], value)


def _list(value: object, what: str) -> list[object]:
    if not isinstance(value, list):
        raise ExternalSourceError(f"{what}: expected a list, got {type(value).__name__}")
    return cast(list[object], value)


def parse_fng(body: object) -> pl.DataFrame:
    """{data: [{value, value_classification, timestamp (unix s, 00:00 UTC)}...]} -> (event_ns, value, label)."""
    rows = _list(_obj(body, "fng").get("data"), "fng.data")
    out: list[tuple[int, float, str]] = []
    for item in rows:
        row = _obj(item, "fng.data[]")
        try:
            out.append((int(str(row["timestamp"])) * 10**9, float(str(row["value"])), str(row["value_classification"])))
        except (KeyError, ValueError) as e:
            raise ExternalSourceError(f"fng row {row!r} is not {{timestamp, value, value_classification}}") from e
    frame = pl.DataFrame(out, schema={"event_ns": pl.Int64, "value": pl.Float64, "label": pl.String}, orient="row")
    return frame.unique("event_ns", keep="last").sort("event_ns")


def parse_coinmetrics(body: object) -> tuple[pl.DataFrame, str | None]:
    """One page {data: [{asset, time (ISO, 00:00Z), <metric>: "text"}...], next_page_token?} -> rows, token."""
    page = _obj(body, "coinmetrics")
    rows: list[dict[str, object]] = []
    for item in _list(page.get("data"), "coinmetrics.data"):
        row = _obj(item, "coinmetrics.data[]")
        try:
            stamp = datetime.fromisoformat(str(row["time"]).replace("Z", "+00:00"))
            out: dict[str, object] = {"symbol": str(row["asset"]).upper(), "event_ns": int(stamp.timestamp()) * 10**9}
            for metric in COINMETRICS_METRICS:
                value = row.get(metric)
                out[metric] = None if value is None else float(str(value))
        except (KeyError, ValueError) as e:
            raise ExternalSourceError(f"coinmetrics row {row!r} has no valid asset / time / metric") from e
        rows.append(out)
    schema = {"symbol": pl.String, "event_ns": pl.Int64, **dict.fromkeys(COINMETRICS_METRICS, pl.Float64)}
    token = page.get("next_page_token")
    return pl.DataFrame(rows, schema=schema), (str(token) if token else None)


def parse_dvol(body: object) -> tuple[pl.DataFrame, int | None]:
    """{result: {data: [[ts_ms, open, high, low, close]...], continuation}} -> rows, next end_timestamp."""
    result = _obj(_obj(body, "dvol").get("result"), "dvol.result")
    rows: list[tuple[int, float, float, float, float]] = []
    for item in _list(result.get("data"), "dvol.result.data"):
        candle = _list(item, "dvol candle")
        if len(candle) != 5:
            raise ExternalSourceError(f"dvol candle has {len(candle)} fields, expected 5")
        try:
            ts, o, h, lo, c = (float(str(v)) for v in candle)
        except ValueError as e:
            raise ExternalSourceError(f"dvol candle {candle!r} is not numeric") from e
        rows.append((int(ts) * 10**6, o, h, lo, c))
    schema = {"event_ns": pl.Int64, "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64}
    cont = result.get("continuation")
    return pl.DataFrame(rows, schema=schema, orient="row"), (int(str(cont)) if cont not in (None, "") else None)


def _get(client: httpx.Client, url: str, params: Mapping[str, str | int] | None = None) -> object:
    response = client.get(url, params=params)
    response.raise_for_status()
    return cast(object, response.json())


def fetch_fng(client: httpx.Client) -> pl.DataFrame:
    return parse_fng(_get(client, FNG_URL))


def fetch_coinmetrics(client: httpx.Client, assets: Sequence[str], *, start: str) -> pl.DataFrame:
    """All daily rows of `assets` from `start` (YYYY-MM-DD), following `next_page_token` (bounded)."""
    params: dict[str, str | int] = {
        "assets": ",".join(a.lower() for a in assets),
        "metrics": ",".join(COINMETRICS_METRICS),
        "frequency": "1d",
        "start_time": start,
        "page_size": 10_000,
    }
    parts: list[pl.DataFrame] = []
    for _ in range(MAX_PAGES):
        frame, token = parse_coinmetrics(_get(client, COINMETRICS_URL, params))
        parts.append(frame)
        if token is None:
            return pl.concat(parts).unique(["symbol", "event_ns"], keep="last").sort(["symbol", "event_ns"])
        params["next_page_token"] = token
    raise ExternalSourceError(f"coinmetrics paging exceeded {MAX_PAGES} pages")


def fetch_dvol(client: httpx.Client, currency: str, *, start_ms: int, end_ms: int) -> pl.DataFrame:
    """Hourly DVOL candles in [start_ms, end_ms], paging backwards with `continuation` (bounded)."""
    parts: list[pl.DataFrame] = []
    end = end_ms
    for _ in range(MAX_PAGES):
        params: dict[str, str | int] = {
            "currency": currency.upper(),
            "start_timestamp": start_ms,
            "end_timestamp": end,
            "resolution": "3600",
        }
        frame, cont = parse_dvol(_get(client, DVOL_URL, params))
        parts.append(frame)
        if cont is None or cont <= start_ms or frame.is_empty():
            out = pl.concat(parts).unique("event_ns", keep="first").sort("event_ns")
            # a candle still open at end_ms (end = now) carries a partial close: only closed hours are kept
            closed = (pl.col("event_ns") + HOUR_MS * 10**6) <= end_ms * 10**6
            return out.filter((pl.col("event_ns") >= start_ms * 10**6) & closed)
        end = cont
    raise ExternalSourceError(f"dvol paging exceeded {MAX_PAGES} pages")


def _month() -> pl.Expr:
    return pl.from_epoch(pl.col("event_ns"), time_unit="ns").dt.strftime("%Y-%m")


def _digest(frame: pl.DataFrame) -> str:
    return hashlib.sha256(frame.sort("event_ns").write_csv().encode("utf-8")).hexdigest()


def write_monthly(
    frame: pl.DataFrame,
    *,
    data_root: Path,
    catalog: Catalog,
    dataset_id: str,
    symbol: str,
    source_url: str,
    fetched_ns: int | None = None,
) -> int:
    """Merge the months of `frame` over the newest stored version of each month (new values win per `event_ns`,
    rows the fetch did not cover are kept) and write the months whose merged content changed; the number written.
    Version names carry the fetch time in nanoseconds and an existing name is refused, so no file is replaced."""
    now = fetched_ns if fetched_ns is not None else time.time_ns()
    stamp = datetime.fromtimestamp(now // 10**9, UTC).strftime("%Y%m%dT%H%M%S") + f"{now % 10**9:09d}"
    body = frame.drop("symbol", strict=False)
    written = 0
    for (month,), part in body.with_columns(_month().alias("_m")).group_by("_m", maintain_order=True):
        rows = part.drop("_m").sort("event_ns")
        latest = bulk_files(data_root, dataset_id, [symbol], [str(month)])
        if latest:
            previous = read_lake_file(latest[-1]).drop("symbol", strict=False)
            rows = pl.concat([previous, rows], how="diagonal_relaxed").unique("event_ns", keep="last").sort("event_ns")
            if _digest(previous) == _digest(rows):
                continue  # nothing new since the last fetch
        target = data_root / "lake" / dataset_id / str(month) / f"{symbol}-{month}-{stamp}.parquet"
        if target.exists():
            raise ExternalSourceError(f"lake version {target.name} exists already; lake files are never replaced")
        write_frame(rows, target, symbol=symbol)
        try:
            with catalog.transaction():
                catalog.conn.execute(
                    "insert into bulk_file(url, last_modified, dataset_id, symbol, month, sha256, zip_bytes, "
                    "rows_written, lake_path, fetched_ns) values (?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"{source_url}#{symbol}/{month}",
                        stamp,
                        dataset_id,
                        symbol,
                        str(month),
                        _digest(rows),
                        0,
                        rows.height,
                        str(target),
                        now,
                    ),
                )
        except sqlite3.IntegrityError as e:
            target.unlink(missing_ok=True)  # never leave a file the catalog does not name
            raise ExternalSourceError(f"version {stamp} of {symbol} {month} is recorded already") from e
        written += 1
    return written
