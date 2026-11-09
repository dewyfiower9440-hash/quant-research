"""Step 9: Upbit 1m candle backfill against an in-memory fake of the verified endpoint semantics
(up to `count` existing candles strictly before `to`, newest first; [] before listing)."""

import asyncio
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest

from quant_research.backfill.upbit_candles import (
    DATASET,
    CandleStore,
    backfill_market,
    no_trade_spans,
    open_ns,
    parse_candles,
    to_param,
)
from quant_research.capture.http import Fetcher
from quant_research.catalog.bootstrap import bootstrap
from quant_research.catalog.store import Catalog
from quant_research.data.lake import load_dataset
from quant_research.errors import ExternalServiceError

MIN = 60 * 10**9
BASE = 1_794_000_000 * 10**9 // MIN * MIN  # a minute boundary
RUN_END = BASE + 1_000 * MIN


def _candle(market: str, t_ns: int, price: float = 100.0) -> dict[str, Any]:
    stamp = datetime.fromtimestamp(t_ns // 10**9, tz=UTC)
    return {
        "market": market,
        "candle_date_time_utc": stamp.strftime("%Y-%m-%dT%H:%M:%S"),
        "candle_date_time_kst": "ignored",
        "opening_price": price,
        "high_price": price + 1,
        "low_price": price - 1,
        "trade_price": price + 0.5,
        "timestamp": t_ns // 1_000_000 + 59_000,
        "candle_acc_trade_price": 1000.0,
        "candle_acc_trade_volume": 10.0,
        "unit": 1,
    }


class FakeUpbit:
    def __init__(self, opens: dict[str, list[int]], *, stuck: bool = False) -> None:
        self.opens = {m: sorted(v) for m, v in opens.items()}
        self.requests: list[dict[str, str]] = []
        self.stuck = stuck

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append(params)
        assert not request.url.path.endswith("/")
        to = int(datetime.strptime(params["to"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()) * 10**9
        before = [t for t in self.opens[params["market"]] if self.stuck or t < to]
        page = sorted(before, reverse=True)[: int(params["count"])]
        return httpx.Response(200, json=[_candle(params["market"], t) for t in page])


class NoJitter(random.Random):
    def uniform(self, a: float, b: float) -> float:
        return 0.0


async def _no_sleep(_s: float) -> None:
    return None


def _fetcher(server: FakeUpbit) -> Fetcher:
    client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    return Fetcher(client, "upbit", sleep=_no_sleep, now_s=lambda: 1_000.0, rng=NoJitter())


def _store(tmp_path: Path) -> CandleStore:
    catalog = Catalog.open(tmp_path / "meta" / "catalog.db")
    bootstrap(catalog)
    return CandleStore(tmp_path, catalog, RUN_END, now_ns=lambda: RUN_END + 5 * MIN)


def test_parse_maps_fields_and_stamps_the_open_minute() -> None:
    frame = parse_candles([_candle("KRW-BTC", BASE + MIN), _candle("KRW-BTC", BASE)], fetched_ns=7)
    row = frame.row(0, named=True)
    assert frame["event_ns"].to_list() == [BASE, BASE + MIN]  # ascending
    assert row["close"] == 100.5 and row["value"] == 1000.0 and row["fetched_ns"] == 7
    assert row["last_trade_ns"] == BASE + 59 * 10**9
    assert "received_ns" not in frame.columns  # history is not live-observed: availability is modelled
    assert open_ns({"candle_date_time_utc": to_param(BASE)[:-1]}) == BASE


def test_no_trade_spans_are_the_empty_minutes_between_candles() -> None:
    spans = no_trade_spans([BASE, BASE + MIN, BASE + 4 * MIN], lo_ns=BASE, hi_ns=BASE + 6 * MIN)
    assert spans == [(BASE + 2 * MIN, BASE + 4 * MIN), (BASE + 5 * MIN, BASE + 6 * MIN)]


def test_backfill_pages_to_listing_records_chunks_no_trade_and_done(tmp_path: Path) -> None:
    opens = [BASE + i * MIN for i in range(450) if i not in (100, 101, 300)]  # 447 candles, 2 quiet spans
    server = FakeUpbit({"KRW-BTC": opens})
    store = _store(tmp_path)
    result = asyncio.run(backfill_market(_fetcher(server), store, "KRW-BTC", pages_per_chunk=2))
    assert result.done and result.rows == 447
    assert result.requests == 4  # 200 + 200 + 47 + [] (listing reached)
    assert result.chunks == 2  # two pages per chunk, then the remainder
    lake = load_dataset(tmp_path, DATASET)
    assert sorted(lake["event_ns"].to_list()) == opens
    gaps = store.catalog.conn.execute(
        "select start_ns, end_ns, cause, detail from gap where source=? order by start_ns", (DATASET,)
    ).fetchall()
    # quiet minutes inside history, plus the minutes between the last candle and the run end
    assert gaps == [
        (BASE + 100 * MIN, BASE + 102 * MIN, "no_trade", "KRW-BTC"),
        (BASE + 300 * MIN, BASE + 301 * MIN, "no_trade", "KRW-BTC"),
        (BASE + 450 * MIN, RUN_END, "no_trade", "KRW-BTC"),
    ]
    done = store.catalog.conn.execute("select first_ns from backfill_done where market='KRW-BTC'").fetchone()
    assert done == (BASE,)
    assert all(not p["to"].endswith("/") for p in server.requests)
    store.catalog.close()


def test_a_stopped_run_resumes_without_gaps_or_duplicates(tmp_path: Path) -> None:
    opens = [BASE + i * MIN for i in range(900)]
    server = FakeUpbit({"KRW-ETH": opens})
    store = _store(tmp_path)
    calls = {"n": 0}

    def stop_after_two_pages() -> str | None:
        calls["n"] += 1
        return "resource gate" if calls["n"] > 2 else None

    first = asyncio.run(
        backfill_market(_fetcher(server), store, "KRW-ETH", pages_per_chunk=50, should_stop=stop_after_two_pages)
    )
    assert not first.done and first.stopped_reason == "resource gate" and first.rows == 400  # buffer flushed
    second = asyncio.run(backfill_market(_fetcher(server), store, "KRW-ETH", pages_per_chunk=50))
    assert second.done and first.rows + second.rows == 900
    lake = load_dataset(tmp_path, DATASET)
    assert lake.height == 900 and lake["event_ns"].n_unique() == 900
    no_trade = store.catalog.conn.execute(
        "select count(*) from gap where source=? and end_ns <= ?", (DATASET, BASE + 900 * MIN)
    ).fetchone()
    assert no_trade == (0,)  # resuming at a chunk boundary invents no quiet minutes
    third = asyncio.run(backfill_market(_fetcher(server), store, "KRW-ETH"))
    assert third.done and third.requests == 0  # done markets are not fetched again
    store.catalog.close()


def test_a_market_with_no_candles_is_done_with_no_first_candle(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = asyncio.run(backfill_market(_fetcher(FakeUpbit({"KRW-NEW": []})), store, "KRW-NEW"))
    assert result.done and result.rows == 0
    assert store.catalog.conn.execute("select first_ns from backfill_done").fetchone() == (None,)
    store.catalog.close()


def test_an_api_that_does_not_page_backwards_is_an_error_not_a_loop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    server = FakeUpbit({"KRW-BAD": [RUN_END + MIN]}, stuck=True)  # returns a candle at/after `to`
    with pytest.raises(ExternalServiceError, match="did not move"):
        asyncio.run(backfill_market(_fetcher(server), store, "KRW-BAD"))
    store.catalog.close()


def test_chunk_parquet_and_catalog_rows_are_written_together(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.catalog.conn.execute(
        "create trigger fail_chunk before insert on backfill_chunk begin select raise(abort, 'disk full'); end"
    )
    frame = parse_candles([_candle("KRW-X", BASE), _candle("KRW-X", BASE + 3 * MIN)], fetched_ns=1)
    with pytest.raises(Exception, match="disk full"):
        store.write_chunk("KRW-X", frame, end_ns=BASE + 5 * MIN)
    assert store.catalog.conn.execute("select count(*) from gap").fetchone() == (0,)
    assert store.resume_to("KRW-X") == RUN_END  # nothing recorded: the next run refetches this chunk
    assert pl.read_parquet(store.written[0]).height == 2  # orphan file is harmless: same name is rewritten
    store.catalog.close()


def test_throttle_is_awaited_before_every_request(tmp_path: Path) -> None:
    store = _store(tmp_path)
    server = FakeUpbit({"KRW-BTC": [BASE + i * MIN for i in range(250)]})
    ticks: list[int] = []

    async def throttle() -> None:
        ticks.append(len(server.requests))

    result = asyncio.run(backfill_market(_fetcher(server), store, "KRW-BTC", throttle=throttle))
    assert ticks == [0, 1, 2] and result.requests == 3
    store.catalog.close()


def test_overlapping_runs_do_not_duplicate_candles_in_the_reader(tmp_path: Path) -> None:
    """A pilot and the full run have different run ends; the reader keeps one row per (market, minute)."""
    opens = [BASE + i * MIN for i in range(300)]
    catalog = Catalog.open(tmp_path / "meta" / "catalog.db")
    bootstrap(catalog)
    for run_end in (BASE + 250 * MIN, RUN_END):
        store = CandleStore(tmp_path, catalog, run_end, now_ns=lambda: RUN_END)
        asyncio.run(backfill_market(_fetcher(FakeUpbit({"KRW-BTC": opens})), store, "KRW-BTC"))
    lake = load_dataset(tmp_path, DATASET)
    assert lake.height == 300 and lake["event_ns"].to_list() == opens
    catalog.close()
