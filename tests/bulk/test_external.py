"""v2.2 B4 tier T7: key-less external sources (Fear & Greed, Coin Metrics Community, Deribit DVOL), no network."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import polars as pl
import pytest

from quant_research.bulk.external import (
    COINMETRICS_DATASET,
    COINMETRICS_METRICS,
    DVOL_DATASET,
    FNG_DATASET,
    ExternalSourceError,
    fetch_coinmetrics,
    fetch_dvol,
    fetch_fng,
    parse_coinmetrics,
    parse_dvol,
    parse_fng,
    write_monthly,
)
from quant_research.catalog.bootstrap import bootstrap
from quant_research.catalog.store import Catalog
from quant_research.data.lake import bulk_files, load_dataset, read_lake_file

DAY_S = 86_400
JAN1 = 1_735_689_600  # 2025-01-01T00:00Z
HOUR_MS = 3_600_000


def _fng_body(days: int, start: int = JAN1) -> dict[str, object]:
    data = [
        {"value": str(40 + d), "value_classification": "Fear", "timestamp": str(start + d * DAY_S)}
        for d in reversed(range(days))  # the API lists the newest first
    ]
    return {"name": "Fear and Greed Index", "data": data}


def _json(body: object) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(body).encode())


def test_fear_and_greed_parses_newest_first_into_sorted_days() -> None:
    frame = parse_fng(_fng_body(3))
    assert frame["event_ns"].to_list() == [(JAN1 + d * DAY_S) * 10**9 for d in range(3)]
    assert frame["value"].to_list() == [40.0, 41.0, 42.0]
    with pytest.raises(ExternalSourceError, match="list"):
        parse_fng({"data": "oops"})


def test_coinmetrics_follows_the_page_token_and_keeps_missing_metrics_null() -> None:
    pages = [
        {
            "data": [{"asset": "btc", "time": "2025-01-01T00:00:00.000000000Z", "PriceUSD": "94000.5", "TxCnt": "1"}],
            "next_page_token": "p2",
        },
        {"data": [{"asset": "eth", "time": "2025-01-01T00:00:00.000000000Z", "PriceUSD": "3300.1"}]},
    ]
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("next_page_token")
        seen.append(token)
        return _json(pages[0 if token is None else 1])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        frame = fetch_coinmetrics(client, ["BTC", "ETH"], start="2025-01-01")
    assert seen == [None, "p2"]
    assert frame["symbol"].to_list() == ["BTC", "ETH"]
    assert frame["PriceUSD"].to_list() == [94000.5, 3300.1]
    assert frame["CapMVRVCur"].to_list() == [None, None] and frame["TxCnt"].to_list() == [1.0, None]


def test_dvol_pages_backwards_until_the_start_and_drops_duplicates() -> None:
    start_ms, end_ms = JAN1 * 1000, JAN1 * 1000 + 5 * HOUR_MS

    def candles(lo: int, hi: int) -> list[list[float]]:
        return [[float(t), 50.0, 51.0, 49.0, 50.5] for t in range(hi, lo - 1, -HOUR_MS)]  # newest first

    def handler(request: httpx.Request) -> httpx.Response:
        end = int(request.url.params["end_timestamp"])
        if end == end_ms:
            return _json(
                {"result": {"data": candles(end_ms - 2 * HOUR_MS, end_ms), "continuation": end_ms - 2 * HOUR_MS}}
            )
        return _json({"result": {"data": candles(start_ms, end), "continuation": None}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        frame = fetch_dvol(client, "btc", start_ms=start_ms, end_ms=end_ms)
    # 6 candles once each; the one starting at end_ms has not closed yet and is dropped
    assert frame["event_ns"].to_list() == [(start_ms + k * HOUR_MS) * 10**6 for k in range(5)]
    with pytest.raises(ExternalSourceError, match="5"):
        parse_dvol({"result": {"data": [[1, 2, 3]], "continuation": None}})


def test_monthly_files_are_written_once_and_again_only_when_a_month_changes(tmp_path: Path) -> None:
    catalog = Catalog.open(tmp_path / "c.db")
    bootstrap(catalog)
    data = tmp_path / "data"
    try:
        for ds in (FNG_DATASET, COINMETRICS_DATASET, DVOL_DATASET):
            assert catalog.seal(ds).epoch == 2  # sealed with the bulk windows (ADR-040)
        assert catalog.lag_ns(FNG_DATASET) >= DAY_S * 10**9  # undocumented publication: a conservative lag

        def handler(request: httpx.Request) -> httpx.Response:
            return _json(_fng_body(40))  # 31 January days + 9 February days

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            frame = fetch_fng(client)

        def write(rows: pl.DataFrame, fetched_ns: int) -> int:
            return write_monthly(
                rows,
                data_root=data,
                catalog=catalog,
                dataset_id=FNG_DATASET,
                symbol="MARKET",
                source_url="u",
                fetched_ns=fetched_ns,
            )

        assert write(frame, 1) == 2
        assert write(frame, 2 * 10**9) == 0  # unchanged
        assert write(parse_fng(_fng_body(41)), 3 * 10**9) == 1  # one more February day: only February again
        newest = bulk_files(data, FNG_DATASET, ["MARKET"], ["2025-01", "2025-02"])  # newest version per month
        assert sum(read_lake_file(f).height for f in newest) == 41
        assert load_dataset(data, FNG_DATASET).height == 41  # B4 review 8: the newest version per month only
        rows = catalog.conn.execute(
            "select month from bulk_file where dataset_id = ? order by fetched_ns", (FNG_DATASET,)
        )
        assert [r[0] for r in rows] == ["2025-01", "2025-02", "2025-02"]
    finally:
        catalog.close()


def _fng_rows(days: range, value: float = 50.0) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "event_ns": [(JAN1 + d * DAY_S) * 10**9 for d in days],
            "value": [value] * len(days),
            "label": ["Neutral"] * len(days),
        }
    )


def test_a_partial_refetch_merges_into_the_month_instead_of_replacing_it(tmp_path: Path) -> None:
    """B4 review 2: a refetch that starts mid-month (or a source that drops a row) wrote a shorter month whose
    newest version hid the earlier days. The new rows are merged over the previous version (new values win)."""
    catalog = Catalog.open(tmp_path / "c.db")
    bootstrap(catalog)
    data = tmp_path / "data"
    try:

        def write(rows: pl.DataFrame, fetched_ns: int) -> int:
            return write_monthly(
                rows,
                data_root=data,
                catalog=catalog,
                dataset_id=FNG_DATASET,
                symbol="MARKET",
                source_url="u",
                fetched_ns=fetched_ns,
            )

        assert write(_fng_rows(range(31)), 1) == 1
        assert write(_fng_rows(range(14, 31)), 2) == 0  # the same values: nothing new
        assert write(_fng_rows(range(14, 31), value=60.0), 3) == 1
        (newest,) = bulk_files(data, FNG_DATASET, ["MARKET"], ["2025-01"])
        month = read_lake_file(newest).sort("event_ns")
        assert month.height == 31  # the first 14 days are kept
        assert month["value"].to_list() == [50.0] * 14 + [60.0] * 17
    finally:
        catalog.close()


def test_a_version_name_is_never_reused(tmp_path: Path) -> None:
    """B4 review 3: second-resolution version names let a second write in the same second overwrite an
    'immutable' file before its catalog insert failed. Names carry nanoseconds and an existing name is refused."""
    catalog = Catalog.open(tmp_path / "c.db")
    bootstrap(catalog)
    data = tmp_path / "data"
    try:
        kwargs_ns = 1_700_000_000_123_456_789
        write_monthly(
            _fng_rows(range(31)),
            data_root=data,
            catalog=catalog,
            dataset_id=FNG_DATASET,
            symbol="MARKET",
            source_url="u",
            fetched_ns=kwargs_ns,
        )
        (first,) = bulk_files(data, FNG_DATASET, ["MARKET"], ["2025-01"])
        assert "123456789" in first.name
        with pytest.raises(ExternalSourceError, match="exists"):
            write_monthly(
                _fng_rows(range(31), value=70.0),
                data_root=data,
                catalog=catalog,
                dataset_id=FNG_DATASET,
                symbol="MARKET",
                source_url="u",
                fetched_ns=kwargs_ns,
            )
        assert read_lake_file(first)["value"].to_list() == [50.0] * 31  # untouched
    finally:
        catalog.close()


def test_the_dvol_candle_still_open_at_the_fetch_is_dropped() -> None:
    """B4 review 4: with end = now the API returns the current hour's candle with a partial close."""
    start_ms = JAN1 * 1000
    end_ms = start_ms + 2 * HOUR_MS + 39 * 60_000  # 02:39: the 02:00 candle is still open

    def handler(request: httpx.Request) -> httpx.Response:
        data = [[float(start_ms + k * HOUR_MS), 50.0, 51.0, 49.0, 50.5] for k in (2, 1, 0)]
        return _json({"result": {"data": data, "continuation": None}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        frame = fetch_dvol(client, "btc", start_ms=start_ms, end_ms=end_ms)
    assert frame["event_ns"].to_list() == [start_ms * 10**6, (start_ms + HOUR_MS) * 10**6]


def test_malformed_source_rows_raise_the_source_error() -> None:
    """B4 review 7: a missing field or bad number raised KeyError / ValueError, which the tool did not catch."""
    with pytest.raises(ExternalSourceError):
        parse_fng({"data": [{"value": "x", "value_classification": "Fear", "timestamp": "1"}]})
    with pytest.raises(ExternalSourceError):
        parse_fng({"data": [{"value": "40"}]})
    with pytest.raises(ExternalSourceError):
        parse_coinmetrics({"data": [{"asset": "btc", "time": "yesterday"}]})


def test_provisional_coin_metrics_flow_metrics_are_not_fetched() -> None:
    """B4 review 1: exchange flows arrive as 'flash' values and are revised later; a backfill fetched today
    would hold the revised vintage as if known at D+2. Only metrics without that flag are fetched."""
    assert not any(m.startswith("Flow") for m in COINMETRICS_METRICS)
