"""Step 7: raw segments -> immutable Parquet, idempotent, replayable, bitemporal (design v2.1 §3.1, §5.4)."""

import gzip
import json
from pathlib import Path

import polars as pl
import pytest

from quant_research.catalog.store import Catalog
from quant_research.data import compact as compact_module
from quant_research.data.compact import compact_closed_segments
from quant_research.data.lake import SegmentStream, load_dataset, read_segment

HOUR_NS = 3_600 * 10**9
T0 = (1_794_000_000 * 10**9) // HOUR_NS * HOUR_NS
MS = 1_000_000


def _segment(
    raw: Path, source: str, hour_ns: int, payloads: list[object], *, gz: bool = False, tail: bytes = b""
) -> Path:
    day = "2026-11-08"
    from quant_research.capture.segment import hour_key

    path = raw / source / day / f"{source}_{hour_key(hour_ns)}_{hour_ns + 1}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"header": {"format": 2, "source": source, "git_sha": "abc"}})]
    for i, p in enumerate(payloads):
        lines.append(json.dumps({"recv_ns": hour_ns + 10 + i, "offset_ms": -5.0, "clock_suspect": False, "payload": p}))
    data = ("\n".join(lines) + "\n").encode("utf-8") + tail
    if gz:
        path = path.with_suffix(".jsonl.gz")
        path.write_bytes(gzip.compress(data))
    else:
        path.write_bytes(data)
    return path


def _oi(value: str, ts_ms: int = 1_794_000_000_000) -> dict[str, object]:
    return {
        "params": {"symbol": "BTCUSDT"},
        "data": [{"symbol": "BTCUSDT", "sumOpenInterest": value, "sumOpenInterestValue": "1", "timestamp": ts_ms}],
    }


def _book(bid: str) -> list[dict[str, object]]:
    return [
        {
            "symbol": "BTCUSDT",
            "bidPrice": bid,
            "bidQty": "1",
            "askPrice": "2",
            "askQty": "1",
            "time": 1,
            "lastUpdateId": 5,
        }
    ]


def _setup(tmp_path: Path) -> tuple[Path, Catalog]:
    data = tmp_path / "data"
    return data, Catalog.open(data / "meta" / "catalog.db")


def test_closed_segments_become_parquet_and_are_compacted_once(tmp_path: Path) -> None:
    data, cat = _setup(tmp_path)
    _segment(data / "raw", "binance.um.book_ticker_1s", T0, [_book("1"), _book("1.5")])
    first = compact_closed_segments(data, cat, now_ns=T0 + 2 * HOUR_NS)
    assert (first.compacted, first.rows, first.failed) == (1, 2, [])
    again = compact_closed_segments(data, cat, now_ns=T0 + 2 * HOUR_NS)
    assert again.compacted == 0 and again.already_done == 1
    df = load_dataset(data, "binance.um.book_ticker_1s")
    assert df["bid_price"].to_list() == [1.0, 1.5]
    assert df["received_ns"].to_list() == [T0 + 10, T0 + 11]
    cat.close()


def test_compressed_later_segment_is_not_compacted_twice(tmp_path: Path) -> None:
    data, cat = _setup(tmp_path)
    seg = _segment(data / "raw", "binance.um.book_ticker_1s", T0, [_book("1")])
    compact_closed_segments(data, cat, now_ns=T0 + 2 * HOUR_NS)
    seg.with_suffix(".jsonl.gz").write_bytes(gzip.compress(seg.read_bytes()))
    seg.unlink()  # what ADR-006 compression does
    assert compact_closed_segments(data, cat, now_ns=T0 + 3 * HOUR_NS).compacted == 0
    cat.close()


def test_current_hour_and_snapshot_sources_are_left_alone(tmp_path: Path) -> None:
    data, cat = _setup(tmp_path)
    _segment(data / "raw", "binance.um.book_ticker_1s", T0, [_book("1")])
    _segment(data / "raw", "upbit.markets", T0 - HOUR_NS, [[{"market": "KRW-BTC"}]])
    done = compact_closed_segments(data, cat, now_ns=T0 + 30 * 10**9)
    assert done.compacted == 0
    cat.close()


def test_replay_is_deterministic(tmp_path: Path) -> None:
    results: list[list[bytes]] = []
    for name in ("a", "b"):
        data, cat = Path(tmp_path / name), Catalog.open(tmp_path / name / "meta" / "catalog.db")
        _segment(data / "raw", "binance.um.oi_hist", T0, [_oi("10"), _oi("10")], gz=True)
        compact_closed_segments(data, cat, now_ns=T0 + 2 * HOUR_NS)
        files = sorted((data / "lake").rglob("*.parquet"))
        results.append([f.read_bytes() for f in files])
        cat.close()
    assert results[0] == results[1] and results[0]


def test_torn_tail_rows_are_skipped_and_counted(tmp_path: Path) -> None:
    data, cat = _setup(tmp_path)
    _segment(data / "raw", "binance.um.book_ticker_1s", T0, [_book("1")], tail=b'{"recv_ns": 9, "pay')
    done = compact_closed_segments(data, cat, now_ns=T0 + 2 * HOUR_NS)
    assert done.rows == 1 and done.skipped_bytes == len(b'{"recv_ns": 9, "pay')
    cat.close()


def test_open_reader_does_not_block_a_new_compaction(tmp_path: Path) -> None:
    data, cat = _setup(tmp_path)
    _segment(data / "raw", "binance.um.book_ticker_1s", T0, [_book("1")])
    compact_closed_segments(data, cat, now_ns=T0 + 2 * HOUR_NS)
    existing = next((data / "lake").rglob("*.parquet"))
    with existing.open("rb"):  # a reader holds the old file (Windows would block a replace)
        _segment(data / "raw", "binance.um.book_ticker_1s", T0 + HOUR_NS, [_book("2")])
        done = compact_closed_segments(data, cat, now_ns=T0 + 3 * HOUR_NS)
    assert done.compacted == 1 and done.failed == []
    assert load_dataset(data, "binance.um.book_ticker_1s")["bid_price"].to_list() == [1.0, 2.0]
    cat.close()


def test_refetched_metrics_keep_first_receipt_and_changed_values_as_versions(tmp_path: Path) -> None:
    data, cat = _setup(tmp_path)
    _segment(data / "raw", "binance.um.oi_hist", T0, [_oi("10")])
    _segment(data / "raw", "binance.um.oi_hist", T0 + HOUR_NS, [_oi("10"), _oi("11")])  # repeat + revision
    compact_closed_segments(data, cat, now_ns=T0 + 3 * HOUR_NS)
    versions = load_dataset(data, "binance.um.oi_hist").sort("received_ns")
    assert versions["sum_open_interest"].to_list() == [10.0, 11.0]
    assert versions["received_ns"].to_list() == [T0 + 10, T0 + HOUR_NS + 11]
    cat.close()


def test_upbit_trades_are_deduplicated_by_sequential_id(tmp_path: Path) -> None:
    data, cat = _setup(tmp_path)
    page = {
        "market": "KRW-BTC",
        "days_ago": 0,
        "rows": [
            {
                "market": "KRW-BTC",
                "timestamp": 1,
                "trade_price": 1.0,
                "trade_volume": 2.0,
                "ask_bid": "BID",
                "sequential_id": 7,
            }
        ],
    }
    _segment(data / "raw", "upbit.trades", T0, [page, page])  # overlapping catch-up pages
    compact_closed_segments(data, cat, now_ns=T0 + 2 * HOUR_NS)
    assert load_dataset(data, "upbit.trades")["sequential_id"].to_list() == [7]
    cat.close()


def test_normalizer_failure_is_reported_and_the_segment_is_retried_later(tmp_path: Path) -> None:
    data, cat = _setup(tmp_path)
    _segment(data / "raw", "upbit.orderbook_l1_1s", T0, [[{"market": "KRW-X", "timestamp": 1, "orderbook_units": []}]])
    done = compact_closed_segments(data, cat, now_ns=T0 + 2 * HOUR_NS)
    assert done.compacted == 0 and len(done.failed) == 1 and "orderbook_units" in done.failed[0][1]
    assert compact_closed_segments(data, cat, now_ns=T0 + 2 * HOUR_NS).failed  # not marked done
    cat.close()


def test_load_dataset_without_files_returns_an_empty_frame(tmp_path: Path) -> None:
    assert load_dataset(tmp_path, "binance.um.force_order").height == 0
    assert isinstance(load_dataset(tmp_path, "binance.um.force_order"), pl.DataFrame)


# --- review #1/#2: bounded-memory streaming and per-segment failure isolation ---


def test_small_batches_produce_the_same_lake_file_as_one_batch(tmp_path: Path) -> None:
    payloads: list[object] = [_book(str(i)) for i in range(1, 8)]
    frames: list[pl.DataFrame] = []
    for name, batch in (("one", 10_000), ("many", 2)):
        data = tmp_path / name / "data"
        cat = Catalog.open(data / "meta" / "catalog.db")
        _segment(data / "raw", "binance.um.book_ticker_1s", T0, payloads, gz=True)
        done = compact_closed_segments(data, cat, now_ns=T0 + 2 * HOUR_NS, batch_rows=batch)
        assert done.rows == 7 and done.failed == []
        frames.append(load_dataset(data, "binance.um.book_ticker_1s"))
        cat.close()
    assert frames[0].equals(frames[1])


def test_columns_that_are_null_in_an_early_batch_get_their_type_from_later_batches(tmp_path: Path) -> None:
    data, cat = _setup(tmp_path)
    nullish = _book("1")
    nullish[0]["bidPrice"] = None  # first batch: all-null column
    _segment(data / "raw", "binance.um.book_ticker_1s", T0, [nullish, _book("2")])
    done = compact_closed_segments(data, cat, now_ns=T0 + 2 * HOUR_NS, batch_rows=1)
    assert done.failed == []
    assert load_dataset(data, "binance.um.book_ticker_1s").sort("received_ns")["bid_price"].to_list() == [None, 2.0]
    cat.close()


def test_one_unreadable_segment_does_not_block_later_segments(tmp_path: Path) -> None:
    data, cat = _setup(tmp_path)
    bad = _segment(data / "raw", "binance.um.oi_hist", T0, [_oi("1")], gz=True)
    bad.write_bytes(bad.read_bytes()[:20])  # truncated gzip stream
    _segment(data / "raw", "binance.um.oi_hist", T0 + HOUR_NS, [_oi("2", ts_ms=1_794_000_300_000)])
    _segment(data / "raw", "upbit.orderbook_l1_1s", T0, [[{"market": "KRW-X", "timestamp": 1, "orderbook_units": []}]])
    _segment(data / "raw", "binance.um.book_ticker_1s", T0, [_book("3")])  # sorts after the failing sources
    done = compact_closed_segments(data, cat, now_ns=T0 + 3 * HOUR_NS)
    assert done.compacted == 2 and len(done.failed) == 2
    assert load_dataset(data, "binance.um.book_ticker_1s")["bid_price"].to_list() == [3.0]
    cat.close()


def test_a_segment_removed_between_listing_and_reading_is_skipped_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compress job may replace .jsonl by .jsonl.gz while compaction runs; the .gz is picked up next run."""
    data, cat = _setup(tmp_path)
    _segment(data / "raw", "binance.um.book_ticker_1s", T0, [_book("1")])

    def vanish_then_open(path: Path) -> SegmentStream:
        path.unlink()  # the concurrent compress job wins the race
        return SegmentStream(path)

    monkeypatch.setattr(compact_module, "SegmentStream", vanish_then_open)
    done = compact_closed_segments(data, cat, now_ns=T0 + 2 * HOUR_NS)
    assert (done.compacted, done.failed, done.vanished) == (0, [], 1)
    cat.close()


def test_stream_matches_the_whole_file_reader_including_torn_tails(tmp_path: Path) -> None:
    tail = b'{"recv_ns": 9, "pay'
    path = _segment(tmp_path / "raw", "binance.um.book_ticker_1s", T0, [_book("1"), _book("2")], tail=tail)
    stream = SegmentStream(path)
    records = list(stream)
    whole = read_segment(path)
    assert records == whole.records and len(records) == 2
    assert stream.sha256 == whole.sha256 and stream.skipped_bytes == whole.skipped_bytes == len(tail)
