"""Step 4: incremental backup of closed raw files with a sha256 manifest, and a restore drill."""

import gzip
import json
from pathlib import Path

import pytest

from quant_research.capture import backup as backup_module
from quant_research.capture.backup import (
    KEEP_CATALOG_SNAPSHOTS,
    backup_closed_files,
    restore_and_verify,
    restore_catalog_and_verify,
    snapshot_catalog,
)
from quant_research.capture.compress import compress_closed_segments
from quant_research.capture.segment import SegmentWriter
from quant_research.catalog.store import ROW_COUNT_SQL, Catalog

HOUR_NS = 3_600 * 10**9
T0 = (1_794_000_000 * 10**9) // HOUR_NS * HOUR_NS


class Clock:
    def __init__(self, t: int) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


def _segment(root: Path, source: str, t: int, payloads: list[int]) -> SegmentWriter:
    w = SegmentWriter(root, source, "v", "sha", Clock(t), fsync=lambda _fd: None)
    for p in payloads:
        w.write(p, offset_ms=None)
    return w


def _layout(tmp_path: Path) -> tuple[Path, SegmentWriter]:
    raw = tmp_path / "data" / "raw"
    old = _segment(raw, "a", T0, [1, 2, 3])
    old.close()
    compress_closed_segments(raw, now_ns=T0 + 2 * HOUR_NS, min_age_s=0)
    closed_plain = _segment(raw, "b", T0, [4])
    closed_plain.close()
    current = _segment(raw, "c", T0 + 2 * HOUR_NS, [5])  # open, current hour
    (raw / "_gaps").mkdir()
    (raw / "_gaps" / "process.rest.jsonl").write_text('{"x":1}\n', encoding="utf-8")
    (tmp_path / "data" / "state").mkdir()
    (tmp_path / "data" / "state" / "upbit_trades.json").write_text('{"KRW-BTC": 5}', encoding="utf-8")
    return tmp_path / "data", current


def test_backup_copies_closed_files_skips_the_open_hour_and_is_incremental(tmp_path: Path) -> None:
    data, current = _layout(tmp_path)
    dest = tmp_path / "backup"
    first = backup_closed_files(data, dest, now_ns=T0 + 2 * HOUR_NS + 5)
    copied = {p.relative_to(data).as_posix() for p in first.copied}
    assert any(c.startswith("raw/a/") and c.endswith(".jsonl.gz") for c in copied)
    assert any(c.startswith("raw/b/") and c.endswith(".jsonl") for c in copied)
    assert "raw/_gaps/process.rest.jsonl" in copied and "state/upbit_trades.json" in copied
    assert not any(c.startswith("raw/c/") for c in copied)  # current hour is still being written
    again = backup_closed_files(data, dest, now_ns=T0 + 2 * HOUR_NS + 5)
    assert again.copied == []  # unchanged files are not copied twice
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest) == copied
    current.close()


def test_changed_append_log_is_copied_again(tmp_path: Path) -> None:
    data, current = _layout(tmp_path)
    dest = tmp_path / "backup"
    backup_closed_files(data, dest, now_ns=T0 + 2 * HOUR_NS + 5)
    with (data / "raw" / "_gaps" / "process.rest.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"x":2}\n')
    again = backup_closed_files(data, dest, now_ns=T0 + 2 * HOUR_NS + 5)
    assert [p.name for p in again.copied] == ["process.rest.jsonl"]
    current.close()


def test_restore_drill_reproduces_every_file_and_line_count(tmp_path: Path) -> None:
    data, current = _layout(tmp_path)
    dest = tmp_path / "backup"
    backup_closed_files(data, dest, now_ns=T0 + 2 * HOUR_NS + 5)
    report = restore_and_verify(dest, tmp_path / "restore")
    assert report.ok and report.files == 4 and not report.mismatches
    gz = next((tmp_path / "restore" / "raw" / "a").glob("*/*.jsonl.gz"))
    assert len(gzip.decompress(gz.read_bytes()).splitlines()) == 4  # header + 3 records
    current.close()


def test_restore_drill_detects_a_corrupted_backup(tmp_path: Path) -> None:
    data, current = _layout(tmp_path)
    dest = tmp_path / "backup"
    backup_closed_files(data, dest, now_ns=T0 + 2 * HOUR_NS + 5)
    victim = dest / "files" / "state" / "upbit_trades.json"
    victim.write_text('{"KRW-BTC": 6}', encoding="utf-8")
    report = restore_and_verify(dest, tmp_path / "restore")
    assert not report.ok
    assert report.mismatches == ["state/upbit_trades.json"]
    current.close()


def test_a_file_compressed_between_listing_and_copy_is_skipped_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-11-09 06:46Z: compress replaced the .jsonl by its .gz while backup ran -> FileNotFoundError."""
    data, current = _layout(tmp_path)
    dest = tmp_path / "backup"
    listed = backup_module.eligible_files(data, T0 + 2 * HOUR_NS + 5)
    victim = next(p for p in listed if p.parent.parent.name == "b")

    def listing_then_compress(root: Path, now_ns: int) -> list[Path]:
        victim.unlink()  # the concurrent compress job wins the race
        return listed

    monkeypatch.setattr(backup_module, "eligible_files", listing_then_compress)
    done = backup_closed_files(data, dest, now_ns=T0 + 2 * HOUR_NS + 5)
    assert done.vanished == 1 and victim not in done.copied
    assert len(done.copied) == len(listed) - 1  # every other file was still backed up
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert victim.relative_to(data).as_posix() not in manifest
    current.close()


DAY_NS = 86_400 * 10**9


def _catalog_with_rows(data: Path, gaps: int) -> None:
    catalog = Catalog.open(data / "meta" / "catalog.db")
    try:
        for i in range(gaps):
            catalog.conn.execute(
                "insert into gap(source, start_ns, end_ns, cause, detail, recorded_ns) values (?,?,?,?,?,?)",
                ("s", i, i + 1, "unknown", "t", 1),
            )
    finally:
        catalog.close()


def test_catalog_snapshot_restores_with_identical_hash_and_row_counts(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _catalog_with_rows(data, 3)
    dest = tmp_path / "backup"
    snap = snapshot_catalog(data, dest, now_ns=T0)
    assert snap is not None and snap.tables["gap"] == 3
    report = restore_catalog_and_verify(dest, tmp_path / "restore")
    assert report.ok and report.files == 1


def test_catalog_snapshot_restore_detects_a_corrupted_snapshot(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _catalog_with_rows(data, 2)
    dest = tmp_path / "backup"
    snap = snapshot_catalog(data, dest, now_ns=T0)
    assert snap is not None
    blob = bytearray(snap.path.read_bytes())
    blob[-100] ^= 0xFF
    snap.path.write_bytes(bytes(blob))
    report = restore_catalog_and_verify(dest, tmp_path / "restore")
    assert not report.ok and report.mismatches == [snap.path.name]


def test_catalog_snapshot_keeps_one_per_day_for_the_last_seven_days(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _catalog_with_rows(data, 1)
    dest = tmp_path / "backup"
    for day in range(9):
        snapshot_catalog(data, dest, now_ns=T0 + day * DAY_NS)
        snapshot_catalog(data, dest, now_ns=T0 + day * DAY_NS + HOUR_NS)  # same day: replaced, not added
    kept = sorted(p.name for p in (dest / "catalog").glob("*.db"))
    assert len(kept) == KEEP_CATALOG_SNAPSHOTS == 7
    index = json.loads((dest / "catalog" / "snapshots.json").read_text(encoding="utf-8"))
    assert sorted(index) == kept


def test_catalog_snapshot_without_a_catalog_is_skipped(tmp_path: Path) -> None:
    assert snapshot_catalog(tmp_path / "data", tmp_path / "backup", now_ns=T0) is None


def test_row_count_queries_cover_every_catalog_table(tmp_path: Path) -> None:
    catalog = Catalog.open(tmp_path / "c.db")
    try:
        query = "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
        tables = {str(r[0]) for r in catalog.conn.execute(query)}
    finally:
        catalog.close()
    assert set(ROW_COUNT_SQL) == tables  # a new migration must add its table to the snapshot check
