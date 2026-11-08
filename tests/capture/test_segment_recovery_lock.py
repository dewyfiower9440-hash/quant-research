"""Raw capture durability: segment rotation, fsync cadence, write failures, tail quarantine, gaps, lock."""

import json
from pathlib import Path
from typing import cast

import pytest

from quant_research.capture.lock import AlreadyRunningError, InstanceLock
from quant_research.capture.recovery import Gap, inspect_segment, latest_segment, quarantine_tail, record_gap
from quant_research.capture.segment import FORMAT_VERSION, SegmentWriter

HOUR_NS = 3_600 * 10**9
T0 = (1_794_000_000 * 10**9) // HOUR_NS * HOUR_NS  # an hour-aligned instant in 2026-11 (UTC)


class FakeClock:
    def __init__(self, t: int) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


def _synced() -> list[int]:
    """Records the file descriptors passed to fsync."""
    return []


def _writer(tmp_path: Path, clock: FakeClock, synced: list[int], **kw: int) -> SegmentWriter:
    return SegmentWriter(
        root=tmp_path,
        source="binance.forceorder",
        collector_version="0.1.0",
        git_sha="abc123",
        now_ns=clock,
        fsync=synced.append,
        **kw,
    )


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_segment_writes_header_then_records_with_offset_and_suspect_flag(tmp_path: Path) -> None:
    clock, synced = FakeClock(T0 + 5), _synced()
    w = _writer(tmp_path, clock, synced)
    w.write({"e": "forceOrder"}, offset_ms=-16.0, clock_suspect=True)
    w.close()
    assert w.path is not None
    rows = _lines(w.path)
    header = cast(dict[str, object], rows[0]["header"])
    assert isinstance(header.pop("pid"), int)
    assert header == {
        "format": FORMAT_VERSION,
        "source": "binance.forceorder",
        "collector_version": "0.1.0",
        "git_sha": "abc123",
        "created_ns": T0 + 5,
    }
    assert rows[1] == {"recv_ns": T0 + 5, "offset_ms": -16.0, "clock_suspect": True, "payload": {"e": "forceOrder"}}


def test_segment_rotates_on_utc_hour_change_into_a_new_file(tmp_path: Path) -> None:
    clock, synced = FakeClock(T0 + 1), _synced()
    w = _writer(tmp_path, clock, synced)
    w.write(1, offset_ms=None)
    first = w.path
    clock.t = T0 + HOUR_NS + 1
    w.write(2, offset_ms=None)
    second = w.path
    w.close()
    assert first is not None and second is not None and first != second
    assert [r.get("payload") for r in _lines(first)[1:]] == [1]
    assert [r.get("payload") for r in _lines(second)[1:]] == [2]


def test_segment_fsyncs_every_n_lines_and_on_close(tmp_path: Path) -> None:
    clock, synced = FakeClock(T0), _synced()
    w = _writer(tmp_path, clock, synced, fsync_every_lines=3, fsync_every_ns=10**15)
    for i in range(7):
        w.write(i, offset_ms=None)
    # The segment opens lazily on the first write: header sync, then after records 3 and 6.
    assert len(synced) == 3
    w.close()
    assert len(synced) == 4  # record 7 is synced on close


def test_segment_fsyncs_when_time_interval_elapses(tmp_path: Path) -> None:
    clock, synced = FakeClock(T0), _synced()
    w = _writer(tmp_path, clock, synced, fsync_every_lines=10**6, fsync_every_ns=2 * 10**9)
    w.write(1, offset_ms=None)
    assert len(synced) == 1  # header only: record 1 is within the 2 s window
    clock.t += 3 * 10**9
    w.write(2, offset_ms=None)
    assert len(synced) == 2  # window elapsed: records 1-2 synced
    w.close()
    assert len(synced) == 2  # nothing left unsynced


def test_sparse_source_is_synced_by_the_timer_without_another_write(tmp_path: Path) -> None:
    """Review #5: the last record of a rarely written source must not wait hours for the next write."""
    clock, synced = FakeClock(T0), _synced()
    w = _writer(tmp_path, clock, synced, fsync_every_lines=10**6, fsync_every_ns=2 * 10**9)
    w.write("daily snapshot", offset_ms=None)
    assert len(synced) == 1  # header
    w.sync_if_due()
    assert len(synced) == 1  # not due yet
    clock.t += 3 * 10**9
    w.sync_if_due()
    assert len(synced) == 2
    w.sync_if_due()
    assert len(synced) == 2  # nothing new to sync
    w.close()


def test_write_failure_abandons_the_file_and_the_next_write_starts_a_new_one(tmp_path: Path) -> None:
    """Review #6: records after a failed write must never follow a broken line in the same file."""
    clock = FakeClock(T0)
    calls = {"n": 0}

    def flaky_fsync(_fd: int) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device")

    w = SegmentWriter(tmp_path, "s", "v", "sha", clock, fsync=flaky_fsync, fsync_every_lines=1)
    with pytest.raises(OSError, match="No space"):
        w.write(1, offset_ms=None)
    first = w.path
    clock.t += 1
    w.write(2, offset_ms=None)
    w.close()
    assert first is not None and w.path is not None and w.path != first
    assert [r.get("payload") for r in _lines(w.path)[1:]] == [2]


def test_new_writer_never_appends_to_an_existing_segment(tmp_path: Path) -> None:
    clock, synced = FakeClock(T0), _synced()
    a = _writer(tmp_path, clock, synced)
    a.write(1, offset_ms=None)
    a.close()
    b = _writer(tmp_path, clock, synced)
    clock.t += 1  # same hour, new process start
    b.write(2, offset_ms=None)
    b.close()
    assert a.path != b.path


def test_inspect_segment_separates_valid_prefix_from_torn_tail(tmp_path: Path) -> None:
    clock, synced = FakeClock(T0), _synced()
    w = _writer(tmp_path, clock, synced)
    w.write({"a": 1}, offset_ms=None)
    clock.t += 7
    w.write({"a": 2}, offset_ms=None)
    w.close()
    assert w.path is not None
    good = w.path.read_bytes()
    w.path.write_bytes(good + b'{"recv_ns": 99, "payl' + b"\x00" * 16)  # torn write + NUL padding
    state = inspect_segment(w.path)
    assert state.valid_bytes == len(good)
    assert state.records == 2
    assert state.last_recv_ns == T0 + 7
    assert state.tail.startswith(b'{"recv_ns": 99')


def test_inspect_segment_stops_at_first_corrupt_line(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    p.write_bytes(b'{"header": {}}\n{"recv_ns": 5, "payload": 1}\nnot json\n{"recv_ns": 9, "payload": 2}\n')
    state = inspect_segment(p)
    assert state.records == 1
    assert state.last_recv_ns == 5
    assert state.tail.startswith(b"not json")


def test_quarantine_tail_copies_torn_tail_with_valid_length_and_never_touches_the_original(tmp_path: Path) -> None:
    clock, synced = FakeClock(T0), _synced()
    w = _writer(tmp_path, clock, synced)
    w.write(1, offset_ms=None)
    w.close()
    assert w.path is not None
    good = w.path.read_bytes()
    w.path.write_bytes(good + b'{"recv_ns": 1, "pa')
    state = quarantine_tail(tmp_path, "binance.forceorder", fsync=synced.append)
    assert state is not None and state.valid_bytes == len(good)
    quarantined = list((tmp_path / "_quarantine").glob("*.tail"))
    assert len(quarantined) == 1 and quarantined[0].read_bytes() == b'{"recv_ns": 1, "pa'
    index = _lines(tmp_path / "_quarantine" / "index.jsonl")
    assert index == [{"segment": str(w.path), "valid_bytes": len(good), "tail_bytes": 18}]
    assert w.path.read_bytes().endswith(b'{"recv_ns": 1, "pa')


def test_quarantine_tail_without_previous_segment_does_nothing(tmp_path: Path) -> None:
    assert quarantine_tail(tmp_path, "upbit.trades", fsync=lambda _fd: None) is None
    assert latest_segment(tmp_path, "upbit.trades") is None


def test_record_gap_appends_durably(tmp_path: Path) -> None:
    record_gap(tmp_path, Gap("process.rest", None, 5, "restart"), fsync=lambda _fd: None)
    record_gap(tmp_path, Gap("process.rest", 7, 9, "restart"), fsync=lambda _fd: None)
    assert _lines(tmp_path / "_gaps" / "process.rest.jsonl") == [
        {"source": "process.rest", "start_ns": None, "end_ns": 5, "cause": "restart"},
        {"source": "process.rest", "start_ns": 7, "end_ns": 9, "cause": "restart"},
    ]


def test_instance_lock_blocks_a_second_holder_and_releases(tmp_path: Path) -> None:
    path = tmp_path / "run" / "collector-rest.lock"
    with InstanceLock(path), pytest.raises(AlreadyRunningError, match="collector-rest"), InstanceLock(path):
        pass
    with InstanceLock(path):  # released: can be taken again
        pass
