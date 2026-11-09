"""Step 8: bulk kline parsing, grid QA, checksum verification and idempotent download (no real network)."""

import hashlib
import io
import sqlite3
import zipfile
from pathlib import Path

import httpx
import pytest

from quant_research.bulk.download import BulkTarget, download_targets
from quant_research.bulk.klines import missing_minutes, parse_kline_csv
from quant_research.catalog.store import Catalog
from quant_research.data.lake import load_dataset

MIN_MS = 60_000
NO_HEADER = "1577836800000,7189.43,7190.52,7177,7182.44,246.092,1577836859999,1767430.16,336,46.63,334813.19,0\n"
HEADER = (
    "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
    "taker_buy_volume,taker_buy_quote_volume,ignore\n"
)


def test_parse_handles_files_with_and_without_header_and_converts_to_ns() -> None:
    a = parse_kline_csv(NO_HEADER)
    b = parse_kline_csv(HEADER + NO_HEADER)
    assert a.equals(b)
    row = a.row(0, named=True)
    assert row["event_ns"] == 1577836800000 * 1_000_000 and row["close_ns"] == 1577836859999 * 1_000_000
    assert row["close"] == 7182.44 and row["trades"] == 336 and row["quote_volume"] == pytest.approx(1767430.16)


def test_parse_detects_microsecond_timestamps() -> None:
    us = "1735689600000000,1,2,0.5,1.5,10,1735689659999999,15,3,4,6,0\n"
    assert parse_kline_csv(us)["event_ns"].to_list() == [1735689600000000 * 1000]


def test_missing_minutes_reports_gaps_on_the_one_minute_grid() -> None:
    opens_ns = [t * MIN_MS * 1_000_000 for t in (0, 1, 2, 5, 6)]
    gaps = missing_minutes(opens_ns, month_start_ns=0, month_end_ns=8 * MIN_MS * 1_000_000)
    assert gaps == [(3 * MIN_MS * 1_000_000, 5 * MIN_MS * 1_000_000), (7 * MIN_MS * 1_000_000, 8 * MIN_MS * 1_000_000)]


def _zip(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, text)
    return buf.getvalue()


def _server(files: dict[str, bytes], *, bad_checksum: bool = False) -> httpx.MockTransport:
    def handler(r: httpx.Request) -> httpx.Response:
        path = r.url.path
        if path.endswith(".CHECKSUM"):
            body = files[path.removesuffix(".CHECKSUM")]
            digest = "0" * 64 if bad_checksum else hashlib.sha256(body).hexdigest()
            return httpx.Response(200, text=f"{digest}  {path.rsplit('/', 1)[-1].removesuffix('.CHECKSUM')}\n")
        return httpx.Response(200, content=files[path])

    return httpx.MockTransport(handler)


def _target(month: str = "2020-01", last_modified: str = "2020-02-01T00:00:00.000Z") -> BulkTarget:
    return BulkTarget(
        dataset_id="binance.um.bulk.klines_1m",
        kind="klines",
        symbol="BTCUSDT",
        month=month,
        url=f"https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-{month}.zip",
        size=0,
        last_modified=last_modified,
    )


def _files(text: str = NO_HEADER, month: str = "2020-01") -> dict[str, bytes]:
    return {f"/data/futures/um/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-{month}.zip": _zip("x.csv", text)}


def test_download_verifies_writes_records_and_is_idempotent(tmp_path: Path) -> None:
    cat = Catalog.open(tmp_path / "meta" / "catalog.db")
    client = httpx.Client(transport=_server(_files()))
    first = download_targets([_target()], data_root=tmp_path, catalog=cat, client=client, pause_s=0)
    second = download_targets([_target()], data_root=tmp_path, catalog=cat, client=client, pause_s=0)
    assert (first.downloaded, first.rows) == (1, 1) and second.downloaded == 0 and second.already_have == 1
    assert load_dataset(tmp_path, "binance.um.bulk.klines_1m")["close"].to_list() == [7182.44]
    assert list((tmp_path).rglob("*.zip")) == []  # zips are verified in memory, never kept
    cat.close()


def test_checksum_mismatch_is_rejected_and_nothing_is_written(tmp_path: Path) -> None:
    cat = Catalog.open(tmp_path / "meta" / "catalog.db")
    client = httpx.Client(transport=_server(_files(), bad_checksum=True))
    done = download_targets([_target()], data_root=tmp_path, catalog=cat, client=client, pause_s=0)
    assert done.downloaded == 0 and len(done.failed) == 1 and "checksum" in done.failed[0][1]
    assert load_dataset(tmp_path, "binance.um.bulk.klines_1m").height == 0
    cat.close()


def test_regenerated_file_is_kept_as_a_new_version(tmp_path: Path) -> None:
    cat = Catalog.open(tmp_path / "meta" / "catalog.db")
    download_targets(
        [_target()], data_root=tmp_path, catalog=cat, client=httpx.Client(transport=_server(_files())), pause_s=0
    )
    changed = NO_HEADER.replace("7182.44", "7182.45")
    done = download_targets(
        [_target(last_modified="2026-10-07T00:00:00.000Z")],
        data_root=tmp_path,
        catalog=cat,
        client=httpx.Client(transport=_server(_files(changed))),
        pause_s=0,
    )
    assert done.downloaded == 1
    versions = cat.conn.execute("select count(*) from bulk_file where url like '%2020-01.zip'").fetchone()[0]
    assert versions == 2
    cat.close()


def test_grid_gaps_in_a_monthly_file_are_registered_in_the_catalog(tmp_path: Path) -> None:
    cat = Catalog.open(tmp_path / "meta" / "catalog.db")
    rows = "".join(
        f"{1577836800000 + i * MIN_MS},1,1,1,1,1,{1577836800000 + i * MIN_MS + 59999},1,1,1,1,0\n" for i in (0, 1, 3)
    )
    download_targets(
        [_target()], data_root=tmp_path, catalog=cat, client=httpx.Client(transport=_server(_files(rows))), pause_s=0
    )
    gaps = cat.conn.execute("select source, cause, detail, start_ns from gap order by start_ns").fetchall()
    assert gaps[0] == (
        "binance.um.bulk.klines_1m",
        "unknown",
        "BTCUSDT missing 1m bars in bulk file",
        (1577836800000 + 2 * MIN_MS) * 1_000_000,
    )
    assert len(gaps) == 2  # minute 2, and minute 4 to the end of January
    cat.close()


def test_thermal_pause_stops_before_downloading(tmp_path: Path) -> None:
    cat = Catalog.open(tmp_path / "meta" / "catalog.db")
    done = download_targets(
        [_target()],
        data_root=tmp_path,
        catalog=cat,
        client=httpx.Client(transport=_server(_files())),
        pause_s=0,
        heavy_allowed=lambda: (False, "SSD at 79 C"),
    )
    assert done.downloaded == 0 and done.paused_reason == "SSD at 79 C"
    cat.close()


def test_reader_returns_only_the_latest_version_of_a_regenerated_file(tmp_path: Path) -> None:
    cat = Catalog.open(tmp_path / "meta" / "catalog.db")
    download_targets(
        [_target()], data_root=tmp_path, catalog=cat, client=httpx.Client(transport=_server(_files())), pause_s=0
    )
    changed = NO_HEADER.replace("7182.44", "7182.45")
    download_targets(
        [_target(last_modified="2026-10-07T00:00:00.000Z")],
        data_root=tmp_path,
        catalog=cat,
        client=httpx.Client(transport=_server(_files(changed))),
        pause_s=0,
    )
    assert load_dataset(tmp_path, "binance.um.bulk.klines_1m")["close"].to_list() == [7182.45]
    assert len(list((tmp_path / "lake").rglob("*.parquet"))) == 2  # history kept on disk
    cat.close()


def test_parse_does_not_infer_integer_columns_from_early_rows() -> None:
    """F-008: mark/index files start with quote_volume `0` and later contain `0.00000000`."""
    t = 1_790_812_800_000
    # Polars infers types from the first 100 rows by default: keep the decimal after that window.
    rows = "".join(f"{t + i * MIN_MS},1,1,1,1,0,{t + i * MIN_MS + 59999},0,1,0,0,0\n" for i in range(150))
    rows += f"{t + 150 * MIN_MS},1,1,1,1,0,{t + 150 * MIN_MS + 59999},0.00000000,1,0,0,0\n"
    frame = parse_kline_csv(rows)
    assert frame["quote_volume"].to_list() == [0.0] * 151


def test_a_corrupt_csv_fails_only_its_own_file(tmp_path: Path) -> None:
    cat = Catalog.open(tmp_path / "meta" / "catalog.db")
    files = {
        **_files(HEADER + "1577836800000,not,a,number\n", month="2020-01"),  # malformed data row
        **_files(HEADER, month="2020-03"),  # header only: an empty monthly file must not count as done
        **_files(NO_HEADER, month="2020-02"),
    }
    done = download_targets(
        [_target("2020-01"), _target("2020-03"), _target("2020-02")],
        data_root=tmp_path,
        catalog=cat,
        client=httpx.Client(transport=_server(files)),
        pause_s=0,
    )
    assert done.downloaded == 1 and len(done.failed) == 2
    assert any("no rows" in why for _url, why in done.failed)
    cat.close()


def test_gaps_and_file_record_are_written_together_or_not_at_all(tmp_path: Path) -> None:
    """Review #6: a failure after the gap rows must not leave gaps for a file that is not recorded."""
    cat = Catalog.open(tmp_path / "meta" / "catalog.db")
    cat.conn.execute(
        "create trigger fail_bulk_file before insert on bulk_file begin select raise(abort, 'disk full'); end"
    )
    rows = "".join(
        f"{1577836800000 + i * MIN_MS},1,1,1,1,1,{1577836800000 + i * MIN_MS + 59999},1,1,1,1,0\n" for i in (0, 2)
    )
    client = httpx.Client(transport=_server(_files(rows)))
    with pytest.raises(sqlite3.IntegrityError, match="disk full"):
        download_targets([_target()], data_root=tmp_path, catalog=cat, client=client, pause_s=0)
    assert cat.conn.execute("select count(*) from gap").fetchone()[0] == 0
    assert not cat.conn.in_transaction  # rolled back, the connection is usable for the next file
    cat.close()
