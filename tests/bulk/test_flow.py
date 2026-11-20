"""v2.2 B4 tier T5: aggTrades -> 1m taker-flow aggregates (raw trades are not stored), no network."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import httpx
import polars as pl
import pytest

from quant_research.bulk.download import BulkTarget
from quant_research.bulk.flow import (
    AGG_FLOW_DATASET,
    LARGE_QUOTE,
    aggregate_minutes,
    download_flow_month,
    fetch_verified_to_file,
    flow_from_zip,
    parse_agg_chunk,
)
from quant_research.catalog.bootstrap import bootstrap
from quant_research.catalog.store import Catalog
from quant_research.data.lake import load_dataset

M = 60 * 10**9
T0_MS = 1_735_689_600_000  # 2025-01-01T00:00Z
HEADER = "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n"


def _rows(ms_unit: int = 1) -> str:
    """Two minutes: minute 0 has a taker buy 2 @ 100 and a taker sell 1 @ 101 and a large taker buy;
    minute 1 has one taker sell 3 @ 99."""
    rows = [
        (1, 100.0, 2.0, T0_MS + 1_000, "false"),
        (2, 101.0, 1.0, T0_MS + 30_000, "true"),
        (3, 50_000.0, 3.0, T0_MS + 59_999, "False"),  # quote 150k >= LARGE_QUOTE
        (4, 99.0, 3.0, T0_MS + 60_000, "true"),
    ]
    return "".join(f"{i},{p},{q},{i * 10},{i * 10 + 1},{t * ms_unit},{b}\n" for i, p, q, t, b in rows)


def _zip(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("BTCUSDT-aggTrades-2025-01.csv", text)
    return buf.getvalue()


def test_parse_reads_files_with_and_without_a_header_and_ms_or_us_times() -> None:
    plain = parse_agg_chunk(_rows().encode())
    assert plain.height == 4 and plain["ts_ns"][0] == (T0_MS + 1_000) * 10**6
    assert parse_agg_chunk((HEADER + _rows()).encode()).equals(plain)
    assert parse_agg_chunk(_rows(ms_unit=1_000).encode()).equals(plain)  # microsecond stamps
    assert plain["taker_buy"].to_list() == [True, False, True, False]  # is_buyer_maker true = taker sell
    with pytest.raises(ValueError, match="columns"):
        parse_agg_chunk(b"1,2,3\n")


def test_minutes_carry_taker_buy_and_sell_volume_counts_and_large_trades() -> None:
    m = aggregate_minutes(parse_agg_chunk(_rows().encode()))
    first, second = m.row(0, named=True), m.row(1, named=True)
    assert first["event_ns"] == T0_MS * 10**6 and second["event_ns"] == T0_MS * 10**6 + M
    assert (first["buy_qty"], first["sell_qty"], first["trades"]) == (5.0, 1.0, 3)
    assert first["buy_quote"] == pytest.approx(200.0 + 150_000.0) and first["sell_quote"] == pytest.approx(101.0)
    assert first["large_buy_quote"] == pytest.approx(150_000.0) and first["large_sell_quote"] == 0.0
    assert (second["sell_qty"], second["buy_qty"], second["trades"]) == (3.0, 0.0, 1)
    assert LARGE_QUOTE == 100_000.0


def test_streaming_a_zip_in_tiny_chunks_gives_the_same_minutes(tmp_path: Path) -> None:
    path = tmp_path / "a.zip"
    path.write_bytes(_zip(HEADER + _rows()))
    whole = aggregate_minutes(parse_agg_chunk((HEADER + _rows()).encode()))
    for chunk in (7, 33, 1 << 20):  # lines and minutes split across chunks
        assert flow_from_zip(path, chunk_bytes=chunk).equals(whole)


def _client(files: dict[str, bytes], *, bad_checksum: bool = False) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(".CHECKSUM"):
            body = files[path.removesuffix(".CHECKSUM")]
            digest = "0" * 64 if bad_checksum else hashlib.sha256(body).hexdigest()
            return httpx.Response(200, text=f"{digest}  x.zip\n")
        return httpx.Response(200, content=files[path])

    return httpx.Client(transport=httpx.MockTransport(handler))


PATH = "/data/futures/um/monthly/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2025-01.zip"


def test_fetch_streams_to_a_file_and_refuses_a_checksum_mismatch(tmp_path: Path) -> None:
    blob = _zip(_rows())
    with _client({PATH: blob}) as client:
        digest, size = fetch_verified_to_file(client, f"https://h{PATH}", tmp_path / "ok.zip")
    assert (tmp_path / "ok.zip").read_bytes() == blob and size == len(blob)
    assert digest == hashlib.sha256(blob).hexdigest()
    with _client({PATH: blob}, bad_checksum=True) as client, pytest.raises(ValueError, match="checksum"):
        fetch_verified_to_file(client, f"https://h{PATH}", tmp_path / "bad.zip")
    assert not list(tmp_path.glob("bad.zip*"))  # nothing left behind


def test_a_month_becomes_one_aggregated_lake_file_recorded_once_without_the_raw_zip(tmp_path: Path) -> None:
    data = tmp_path / "data"
    catalog = Catalog.open(tmp_path / "c.db")
    bootstrap(catalog)
    target = BulkTarget(AGG_FLOW_DATASET, "aggTrades", "BTCUSDT", "2025-01", f"https://h{PATH}", 1, "2025-02-01T00:00Z")
    try:
        assert catalog.seal(AGG_FLOW_DATASET).epoch == 2  # sealed like every bulk dataset (ADR-040)
        with _client({PATH: _zip(HEADER + _rows())}) as client:
            done = download_flow_month(target, data_root=data, catalog=catalog, client=client, work_dir=tmp_path / "w")
            again = download_flow_month(target, data_root=data, catalog=catalog, client=client, work_dir=tmp_path / "w")
        assert (done.downloaded, done.rows, again.downloaded, again.already_have) == (1, 2, 0, 1)
        lake = load_dataset(data, AGG_FLOW_DATASET)
        assert lake.height == 2 and set(lake["symbol"].to_list()) == {"BTCUSDT"}
        assert not list((tmp_path / "w").glob("*"))  # the raw zip is deleted after aggregation
        rows = catalog.conn.execute("select rows_written from bulk_file where dataset_id = ?", (AGG_FLOW_DATASET,))
        assert [r[0] for r in rows] == [2]
    finally:
        catalog.close()


def test_a_paused_gate_downloads_nothing(tmp_path: Path) -> None:
    catalog = Catalog.open(tmp_path / "c.db")
    bootstrap(catalog)
    target = BulkTarget(AGG_FLOW_DATASET, "aggTrades", "BTCUSDT", "2025-01", f"https://h{PATH}", 1, "L")
    try:
        with _client({PATH: _zip(_rows())}) as client:
            done = download_flow_month(
                target,
                data_root=tmp_path / "data",
                catalog=catalog,
                client=client,
                work_dir=tmp_path / "w",
                heavy_allowed=lambda: (False, "hot"),
            )
        assert done.paused_reason == "hot" and done.downloaded == 0
        assert pl.DataFrame(catalog.conn.execute("select * from bulk_file").fetchall()).height == 0
    finally:
        catalog.close()


def test_crlf_a_missing_final_newline_and_blank_lines_parse_to_the_same_minutes(tmp_path: Path) -> None:
    """B4 review 3: a blank line became an all-null row that passed the width check and wrote a minute with
    event_ns None and one trade."""
    whole = aggregate_minutes(parse_agg_chunk(_rows().encode()))
    variants = {
        "crlf": (HEADER + _rows()).replace("\n", "\r\n"),
        "no_final_newline": _rows().rstrip("\n"),
        "blank_lines": _rows().replace("\n", "\n\n", 1) + "\n\n",
    }
    for name, text in variants.items():
        path = tmp_path / f"{name}.zip"
        path.write_bytes(_zip(text))
        for chunk in (5, 1 << 20):
            got = flow_from_zip(path, chunk_bytes=chunk)
            assert got.equals(whole), (name, chunk)
    with pytest.raises(ValueError, match="missing"):
        parse_agg_chunk(b"1,100.0,1.0,1,1,,true\n")  # a row without a time is an error, not a minute


def test_milliseconds_and_microseconds_are_told_apart_per_row() -> None:
    """B4 review 4: the unit was decided by the chunk's largest stamp, so ms rows next to us rows became 1970."""
    mixed = "1,100.0,1.0,1,1,1735689601000,false\n2,100.0,1.0,2,2,1735689602000000,true\n"
    assert parse_agg_chunk(mixed.encode())["ts_ns"].to_list() == [1735689601000 * 10**6, 1735689602000000 * 10**3]


def test_an_empty_zip_or_checksum_fails_the_month_without_recording_anything(tmp_path: Path) -> None:
    """B4 review 5: IndexError from an empty zip / empty .CHECKSUM escaped the handled errors and crashed the
    tool; a bad checksum must also record nothing and leave no file behind."""
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w"):
        pass
    catalog = Catalog.open(tmp_path / "c.db")
    bootstrap(catalog)
    work = tmp_path / "w"
    work.mkdir()
    (work / "old.zip.123.abc.part").write_bytes(b"x" * 10)  # left by a killed run (review 6)
    target = BulkTarget(AGG_FLOW_DATASET, "aggTrades", "BTCUSDT", "2025-01", f"https://h{PATH}", 1, "L")
    try:
        for files, bad in (({PATH: empty.getvalue()}, False), ({PATH: _zip(_rows())}, True)):
            with _client(files, bad_checksum=bad) as client:
                done = download_flow_month(
                    target, data_root=tmp_path / "d", catalog=catalog, client=client, work_dir=work
                )
            assert done.downloaded == 0 and len(done.failed) == 1
        assert catalog.conn.execute("select count(*) from bulk_file").fetchone()[0] == 0
        assert not list(work.glob("*"))  # no zip, no partial download, the stale .part cleaned
    finally:
        catalog.close()

    def blank_checksum(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="")

    with httpx.Client(transport=httpx.MockTransport(blank_checksum)) as client, pytest.raises(ValueError):
        fetch_verified_to_file(client, f"https://h{PATH}", tmp_path / "x.zip")
