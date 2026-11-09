"""Step 4: incremental backup of closed raw files with a sha256 manifest, and a restore drill."""

import gzip
import json
from pathlib import Path

import pytest

from quant_research.capture import backup as backup_module
from quant_research.capture.backup import backup_closed_files, restore_and_verify
from quant_research.capture.compress import compress_closed_segments
from quant_research.capture.segment import SegmentWriter

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
