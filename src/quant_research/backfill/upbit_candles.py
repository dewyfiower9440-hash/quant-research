"""Upbit 1-minute candle backfill (plan step 9; design docs/plans/2026-11-09-step9-upbit-candles.md).

Verified 2026-11-09: `GET /v1/candles/minutes/1?market&to&count<=200` returns, newest first, up to 200
candles that EXIST strictly before `to` (a sparse market's page can span days); before listing it returns
[]. Paging backwards with `to` = oldest open of the previous page therefore sees every candle, and a
minute between two returned candles is definitively `no_trade` (recorded as a gap of that cause, merged
into spans), not missing data.

Pages are buffered into chunks; each chunk is one immutable Parquet file plus its catalog rows
(`backfill_chunk` + no_trade gaps) in a single transaction. The oldest chunk's start is the resume cursor;
an empty page marks the market done (`backfill_done`). Candles carry `fetched_ns`, never `received_ns`:
historical candles were not observed live, so availability is modelled (open + 1 min + lag).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import polars as pl

from quant_research.bulk.klines import missing_minutes
from quant_research.capture.http import Fetcher
from quant_research.catalog.store import Catalog
from quant_research.data.lake import write_frame
from quant_research.errors import ExternalServiceError

DATASET = "upbit.candles_1m"
URL = "https://api.upbit.com/v1/candles/minutes/1"  # no trailing slash (Upbit rejects it from 2026-11-16)
PAGE = 200
MINUTE_NS = 60 * 10**9

SCHEMA = pl.Schema(
    {
        "market": pl.String,
        "event_ns": pl.Int64,  # candle open (candle_date_time_utc)
        "last_trade_ns": pl.Int64,  # `timestamp`: last trade inside the minute
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "value": pl.Float64,  # candle_acc_trade_price (KRW)
        "volume": pl.Float64,  # candle_acc_trade_volume
        "fetched_ns": pl.Int64,
    }
)


def open_ns(candle: dict[str, Any]) -> int:
    """`candle_date_time_utc` (no zone suffix, UTC) -> ns."""
    stamp = datetime.fromisoformat(str(candle["candle_date_time_utc"])).replace(tzinfo=UTC)
    return int(stamp.timestamp()) * 10**9


def to_param(ns: int) -> str:
    return datetime.fromtimestamp(ns // 10**9, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_candles(rows: list[dict[str, Any]], *, fetched_ns: int) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "market": [str(r["market"]) for r in rows],
            "event_ns": [open_ns(r) for r in rows],
            "last_trade_ns": [int(r["timestamp"]) * 1_000_000 for r in rows],
            "open": [float(r["opening_price"]) for r in rows],
            "high": [float(r["high_price"]) for r in rows],
            "low": [float(r["low_price"]) for r in rows],
            "close": [float(r["trade_price"]) for r in rows],
            "value": [float(r["candle_acc_trade_price"]) for r in rows],
            "volume": [float(r["candle_acc_trade_volume"]) for r in rows],
            "fetched_ns": [fetched_ns] * len(rows),
        },
        schema=SCHEMA,
    ).sort("event_ns")


def oldest_open(frame: pl.DataFrame) -> int:
    return min(int(v) for v in frame["event_ns"].to_list())


def no_trade_spans(opens_ns: list[int], *, lo_ns: int, hi_ns: int) -> list[tuple[int, int]]:
    """Minutes in [lo, hi) without a candle; lo is the oldest candle (before it the market may not exist)."""
    return missing_minutes(opens_ns, month_start_ns=lo_ns, month_end_ns=hi_ns)


@dataclass
class MarketResult:
    market: str
    done: bool = False
    chunks: int = 0
    rows: int = 0
    requests: int = 0
    no_trade_spans: int = 0
    stopped_reason: str | None = None


@dataclass
class CandleStore:
    """Writes chunks (Parquet + catalog rows atomically) and answers the resume questions."""

    data_root: Path
    catalog: Catalog
    run_end_ns: int
    now_ns: Callable[[], int] = time.time_ns
    written: list[Path] = field(default_factory=lambda: list[Path]())

    def is_done(self, market: str) -> bool:
        row = self.catalog.conn.execute(
            "select 1 from backfill_done where dataset_id=? and market=? and run_end_ns=?",
            (DATASET, market, self.run_end_ns),
        ).fetchone()
        return row is not None

    def resume_to(self, market: str) -> int:
        row = self.catalog.conn.execute(
            "select min(start_ns) from backfill_chunk where dataset_id=? and market=? and run_end_ns=?",
            (DATASET, market, self.run_end_ns),
        ).fetchone()
        return self.run_end_ns if row is None or row[0] is None else int(row[0])

    def first_open(self, market: str) -> int | None:
        return None if (to := self.resume_to(market)) == self.run_end_ns else to

    def write_chunk(self, market: str, frame: pl.DataFrame, *, end_ns: int) -> int:
        """Store candles covering [oldest open, end_ns); returns the number of no_trade spans recorded."""
        opens = frame["event_ns"].to_list()
        start = int(min(opens))
        spans = no_trade_spans(opens, lo_ns=start, hi_ns=end_ns)
        target = self.data_root / "lake" / DATASET / market / f"{market}-{self.run_end_ns}-{start}.parquet"
        write_frame(frame, target)
        self.written.append(target)
        now = self.now_ns()
        with self.catalog.transaction():
            self.catalog.conn.execute(
                "insert into backfill_chunk(dataset_id, market, run_end_ns, start_ns, end_ns, rows_written, lake_path, "
                "fetched_ns) values (?,?,?,?,?,?,?,?)",
                (DATASET, market, self.run_end_ns, start, end_ns, frame.height, str(target), now),
            )
            self.catalog.conn.executemany(
                "insert or ignore into gap(source, start_ns, end_ns, cause, detail, recorded_ns) values (?,?,?,?,?,?)",
                [(DATASET, lo, hi, "no_trade", market, now) for lo, hi in spans],
            )
        return len(spans)

    def mark_done(self, market: str) -> None:
        with self.catalog.transaction():
            self.catalog.conn.execute(
                "insert or ignore into backfill_done(dataset_id, market, run_end_ns, first_ns, finished_ns) "
                "values (?,?,?,?,?)",
                (DATASET, market, self.run_end_ns, self.first_open(market), self.now_ns()),
            )


async def backfill_market(
    f: Fetcher,
    store: CandleStore,
    market: str,
    *,
    pages_per_chunk: int = 50,
    should_stop: Callable[[], str | None] = lambda: None,
    throttle: Callable[[], Awaitable[None]] | None = None,
) -> MarketResult:
    """Page backwards from the resume cursor until the market's history is exhausted or a stop is asked.
    Buffered pages are always flushed before returning, so the cursor never skips candles."""
    result = MarketResult(market)
    if store.is_done(market):
        result.done = True
        return result
    to = store.resume_to(market)
    chunk_end = to
    buffer: list[pl.DataFrame] = []
    pages = 0

    def flush() -> None:
        nonlocal buffer, chunk_end
        if not buffer:
            return
        frame = pl.concat(buffer)
        result.no_trade_spans += store.write_chunk(market, frame, end_ns=chunk_end)
        result.chunks += 1
        result.rows += frame.height
        chunk_end = oldest_open(frame)
        buffer = []

    while True:
        reason = should_stop()
        if reason is not None:
            flush()
            result.stopped_reason = reason
            return result
        if throttle is not None:
            await throttle()  # keeps the candle group well under its 10/s, 600/min budget
        rows = cast(
            list[dict[str, Any]], (await f.get(URL, {"market": market, "to": to_param(to), "count": PAGE})).json()
        )
        result.requests += 1
        if not rows:
            flush()
            store.mark_done(market)
            result.done = True
            return result
        page = parse_candles(rows, fetched_ns=store.now_ns())
        oldest = oldest_open(page)
        if page["event_ns"].to_list()[-1] >= to:  # parse_candles sorts ascending: last = newest
            raise ExternalServiceError(f"upbit candles for {market} did not move before to={to_param(to)}")
        buffer.append(page)
        to = oldest
        pages += 1
        if pages % pages_per_chunk == 0:
            flush()
