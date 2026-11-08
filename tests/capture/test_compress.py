"""ADR-006: closed raw segments are gzip-compressed, verified, and only then is the original removed."""

import gzip
from pathlib import Path

import pytest

from quant_research.capture.compress import compress_closed_segments
from quant_research.capture.segment import SegmentWriter, hour_key

HOUR_NS = 3_600 * 10**9
T0 = (1_794_000_000 * 10**9) // HOUR_NS * HOUR_NS


class Clock:
    def __init__(self, t: int) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


def _segment(root: Path, t: int, payloads: list[int]) -> Path:
    clock = Clock(t)
    w = SegmentWriter(root, "src", "v", "sha", clock, fsync=lambda _fd: None)
    for p in payloads:
        w.write(p, offset_ms=None)
    w.close()
    assert w.path is not None
    return w.path


def test_segments_of_hours_that_ended_long_enough_ago_are_compressed_and_verified(tmp_path: Path) -> None:
    old = _segment(tmp_path, T0, [1, 2, 3])
    original = old.read_bytes()
    current = _segment(tmp_path, T0 + HOUR_NS + 5, [4])  # still inside its hour
    done = compress_closed_segments(tmp_path, now_ns=T0 + HOUR_NS + 11 * 60 * 10**9, min_age_s=600)
    assert done.compressed == [old]
    assert not old.exists()
    assert gzip.decompress(old.with_suffix(".jsonl.gz").read_bytes()) == original
    assert current.exists()


def test_recently_ended_hour_is_left_alone_until_the_grace_period_passes(tmp_path: Path) -> None:
    seg = _segment(tmp_path, T0, [1])
    done = compress_closed_segments(tmp_path, now_ns=T0 + HOUR_NS + 60 * 10**9, min_age_s=600)
    assert done.compressed == [] and seg.exists()


def test_a_file_that_cannot_be_removed_is_kept_and_reported(tmp_path: Path) -> None:
    seg = _segment(tmp_path, T0, [1])

    def locked(_p: Path) -> None:
        raise PermissionError(13, "file in use")

    done = compress_closed_segments(tmp_path, now_ns=T0 + 2 * HOUR_NS, min_age_s=600, unlink=locked)
    assert done.compressed == [] and [p for p, _ in done.skipped] == [seg]
    assert seg.exists()
    # retried later: the earlier verified .gz is rebuilt idempotently
    done = compress_closed_segments(tmp_path, now_ns=T0 + 2 * HOUR_NS, min_age_s=600)
    assert done.compressed == [seg] and not seg.exists()


def test_quarantine_gaps_and_unrelated_folders_are_not_touched(tmp_path: Path) -> None:
    for name in ("_gaps", "_quarantine"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "x.jsonl").write_text("{}\n", encoding="utf-8")
    done = compress_closed_segments(tmp_path, now_ns=T0 + 10 * HOUR_NS, min_age_s=0)
    assert done.compressed == []


def test_unparseable_segment_names_are_skipped_not_guessed(tmp_path: Path) -> None:
    odd = tmp_path / "src" / "2026-11-08" / "manual-notes.jsonl"
    odd.parent.mkdir(parents=True)
    odd.write_text("x\n", encoding="utf-8")
    done = compress_closed_segments(tmp_path, now_ns=T0 + 10 * HOUR_NS, min_age_s=0)
    assert done.compressed == [] and odd.exists()


def test_writer_closes_an_idle_file_once_its_hour_has_ended(tmp_path: Path) -> None:
    """Sparse sources must not keep last hour's file open (it could not be compressed on Windows)."""
    clock = Clock(T0 + 5)
    w = SegmentWriter(tmp_path, "src", "v", "sha", clock, fsync=lambda _fd: None)
    w.write(1, offset_ms=None)
    clock.t = T0 + HOUR_NS + 1
    w.sync_if_due()
    assert w.path is not None
    done = compress_closed_segments(tmp_path, now_ns=T0 + 2 * HOUR_NS, min_age_s=600)
    assert done.compressed == [w.path]
    clock.t += 1
    w.write(2, offset_ms=None)  # reopens a new file for the new hour
    w.close()
    assert w.path is not None and w.path.exists() and hour_key(clock.t) in w.path.name


@pytest.mark.parametrize("level", [0, 10])
def test_invalid_compression_level_is_rejected(tmp_path: Path, level: int) -> None:
    with pytest.raises(ValueError, match="level"):
        compress_closed_segments(tmp_path, now_ns=T0, level=level)
