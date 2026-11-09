"""Phase-1 completion step 4: T1 extra bulk data (funding, metrics, bookDepth, spot klines), no network."""

import hashlib
import io
import zipfile
from pathlib import Path

import httpx
import polars as pl
import pytest

from quant_research.bulk.download import BulkTarget
from quant_research.bulk.extras import (
    FIVE_MIN_NS,
    download_month_group,
    parse_book_depth_csv,
    parse_funding_csv,
    parse_metrics_csv,
)
from quant_research.bulk.s3 import listing, wanted_months
from quant_research.catalog.store import Catalog
from quant_research.data.lake import load_dataset

FUNDING = "calc_time,funding_interval_hours,last_funding_rate\n1577836800000,8,-0.00012359\n1577865600000,8,0.0001\n"
METRICS_HEADER = (
    "create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,"
    "sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
)
DEPTH = (
    "timestamp,percentage,depth,notional\n"
    "2023-01-01 00:06:05,-5,16770.3,271987123.35\n"
    "2023-01-01 00:06:05,5.00,1.5,2.5\n"  # 2023 files write -5, 2026 files write 5.00 (pilot)
)
SEP_1_2020_NS = 1_598_918_400 * 10**9


def _metrics_day(day: int, slots: range) -> str:
    rows = [
        f"2020-09-{day:02d} {s // 12:02d}:{s % 12 * 5:02d}:00,BTCUSDT,39080.2,456144339.2,1.17,1.23,1.35,0.78"
        for s in slots
    ]
    return METRICS_HEADER + "\n".join(rows) + "\n"


def test_parse_funding_converts_ms_to_ns_and_keeps_interval_and_rate() -> None:
    f = parse_funding_csv(FUNDING)
    assert f.row(0, named=True) == {
        "event_ns": 1577836800000 * 10**6,
        "funding_interval_hours": 8,
        "last_funding_rate": pytest.approx(-0.00012359),
    }
    assert f.height == 2


def test_parse_metrics_reads_utc_text_times_and_drops_exact_duplicate_rows() -> None:
    text = _metrics_day(1, range(3))
    doubled = text + text.split("\n", 1)[1]  # the 2020-09-01 file repeats its rows (Phase 0 finding)
    m = parse_metrics_csv(doubled)
    assert m.height == 3
    assert m["event_ns"].to_list() == [SEP_1_2020_NS + i * FIVE_MIN_NS for i in range(3)]
    assert "symbol" not in m.columns  # the symbol column is added once when the lake file is written


def test_parse_book_depth_keeps_each_level_of_a_snapshot_in_both_percentage_formats() -> None:
    d = parse_book_depth_csv(DEPTH)
    assert d["percentage"].to_list() == [-5.0, 5.0]
    assert d["event_ns"].to_list() == [1_672_531_565 * 10**9] * 2


def _zip(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, text)
    return buf.getvalue()


def _server(files: dict[str, bytes], bad: frozenset[str] = frozenset()) -> httpx.Client:
    def handler(r: httpx.Request) -> httpx.Response:
        path = r.url.path
        if path.endswith(".CHECKSUM"):
            body = files[path.removesuffix(".CHECKSUM")]
            digest = "0" * 64 if path.removesuffix(".CHECKSUM") in bad else hashlib.sha256(body).hexdigest()
            return httpx.Response(200, text=f"{digest}  x.zip\n")
        return httpx.Response(200, content=files[path])

    return httpx.Client(transport=httpx.MockTransport(handler))


def _day_target(day: int, last_modified: str = "2020-10-01T00:00:00.000Z") -> BulkTarget:
    path = f"/data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-2020-09-{day:02d}.zip"
    return BulkTarget(
        "binance.um.bulk.metrics_5m", "metrics", "BTCUSDT", "2020-09", f"https://h{path}", 1, last_modified
    )


def test_daily_files_of_a_month_become_one_lake_file_with_grid_gaps_and_resume(tmp_path: Path) -> None:
    files = {
        f"/data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-2020-09-0{d}.zip": _zip("m.csv", _metrics_day(d, s))
        for d, s in ((1, range(288)), (2, range(0, 288, 2)))  # day 2 lost every second slot
    }
    catalog = Catalog.open(tmp_path / "meta" / "catalog.db")
    try:
        with _server(files) as client:
            group = [_day_target(1), _day_target(2)]
            done = download_month_group(
                group, data_root=tmp_path, catalog=catalog, client=client, parse=parse_metrics_csv, grid_ns=FIVE_MIN_NS
            )
            assert (done.downloaded, done.rows, done.failed) == (2, 288 + 144, [])
            # odd slots 1..285 of day 2 are single-slot gaps; slot 287 joins the rest of September
            assert done.gaps == 143 + 1
            again = download_month_group(
                group, data_root=tmp_path, catalog=catalog, client=client, parse=parse_metrics_csv, grid_ns=FIVE_MIN_NS
            )
            assert (again.downloaded, again.already_have) == (0, 2)
            # day 2 regenerated upstream: the month is written again as a newer version, the old file stays
            regenerated = [_day_target(1), _day_target(2, "2020-12-01T00:00:00.000Z")]
            third = download_month_group(
                regenerated,
                data_root=tmp_path,
                catalog=catalog,
                client=client,
                parse=parse_metrics_csv,
                grid_ns=FIVE_MIN_NS,
            )
            assert third.downloaded == 1
        assert len(list((tmp_path / "lake" / "binance.um.bulk.metrics_5m").rglob("*.parquet"))) == 2
        frame = load_dataset(tmp_path, "binance.um.bulk.metrics_5m")
        assert frame.height == 288 + 144 and set(frame["symbol"].to_list()) == {"BTCUSDT"}  # newest version only
        assert catalog.conn.execute("select count(*) from bulk_file").fetchone()[0] == 3
    finally:
        catalog.close()


def test_a_checksum_mismatch_fails_the_whole_month_and_records_nothing(tmp_path: Path) -> None:
    path = "/data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-2020-09-01.zip"
    other = "/data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-2020-09-02.zip"
    files = {path: _zip("m.csv", _metrics_day(1, range(3))), other: _zip("m.csv", _metrics_day(2, range(3)))}
    catalog = Catalog.open(tmp_path / "meta" / "catalog.db")
    try:
        with _server(files, bad=frozenset({other})) as client:
            done = download_month_group(
                [_day_target(1), _day_target(2)],
                data_root=tmp_path,
                catalog=catalog,
                client=client,
                parse=parse_metrics_csv,
                grid_ns=FIVE_MIN_NS,
            )
        assert done.downloaded == 0 and len(done.failed) == 1 and "checksum mismatch" in done.failed[0][1]
        assert catalog.conn.execute("select count(*) from bulk_file").fetchone()[0] == 0
        assert not (tmp_path / "lake").exists()
    finally:
        catalog.close()


def test_a_blocked_gate_pauses_before_any_download(tmp_path: Path) -> None:
    catalog = Catalog.open(tmp_path / "meta" / "catalog.db")
    try:
        with _server({}) as client:
            done = download_month_group(
                [_day_target(1)],
                data_root=tmp_path,
                catalog=catalog,
                client=client,
                parse=parse_metrics_csv,
                grid_ns=None,
                heavy_allowed=lambda: (False, "SSD critical"),
            )
        assert done.paused_reason == "SSD critical" and done.downloaded == 0
    finally:
        catalog.close()


def test_spot_bulk_lake_reads_only_the_newest_version_of_each_month(tmp_path: Path) -> None:
    folder = tmp_path / "lake" / "binance.spot.bulk.klines_1m" / "2025-01"
    folder.mkdir(parents=True)
    pl.DataFrame({"event_ns": [1], "close": [1.0], "symbol": ["BTCUSDT"]}).write_parquet(
        folder / "BTCUSDT-2025-01-20250201T000000000Z.parquet"
    )
    pl.DataFrame({"event_ns": [1], "close": [2.0], "symbol": ["BTCUSDT"]}).write_parquet(
        folder / "BTCUSDT-2025-01-20250301T000000000Z.parquet"
    )
    assert load_dataset(tmp_path, "binance.spot.bulk.klines_1m")["close"].to_list() == [2.0]


def test_listing_follows_s3_pagination_and_keeps_only_zips() -> None:
    pages = {
        "": "<IsTruncated>true</IsTruncated>"
        "<Contents><Key>p/a.zip</Key><Size>3</Size><LastModified>L1</LastModified></Contents>"
        "<Contents><Key>p/a.zip.CHECKSUM</Key><Size>1</Size><LastModified>L1</LastModified></Contents>",
        "p/a.zip": "<IsTruncated>false</IsTruncated>"
        "<Contents><Key>p/b.zip</Key><Size>4</Size><LastModified>L2</LastModified></Contents>",
    }
    markers: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        markers.append(r.url.params["marker"])
        return httpx.Response(200, text=pages[r.url.params["marker"]])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert listing(client, "p/") == [("p/a.zip", 3, "L1"), ("p/b.zip", 4, "L2")]
    assert markers == ["", "p/a.zip"]  # the marker is the last listed zip key


def test_wanted_months_adds_warmup_and_treats_the_end_month_as_exclusive() -> None:
    months = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05"]
    assert wanted_months([["2026-03", "2026-05"]], months, 1) == {"2026-02", "2026-03", "2026-04"}
    assert wanted_months([["2026-01", None]], months, 3) == set(months)
