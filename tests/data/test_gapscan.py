"""Step 10 (part): receive gaps of 1 Hz sources (host suspend, network outage, stalls) become catalog gaps."""

import json
from pathlib import Path

import pytest

from quant_research.capture.segment import hour_key
from quant_research.catalog.store import Catalog
from quant_research.data import gapscan as gapscan_module
from quant_research.data.gapscan import THRESHOLD_NS, scan_receive_gaps

S = 10**9
HOUR = 3_600 * S
T0 = (1_794_000_000 * S) // HOUR * HOUR
BOOK = "binance.um.book_ticker_1s"


def _segment(raw: Path, source: str, hour_ns: int, recv_ns: list[int], *, created: int | None = None) -> Path:
    path = raw / source / "2026-11-08" / f"{source}_{hour_key(hour_ns)}_{created or hour_ns + 1}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"header": {"format": 2, "source": source}})]
    lines += [json.dumps({"recv_ns": t, "offset_ms": 1.0, "clock_suspect": False, "payload": []}) for t in recv_ns]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _gaps(cat: Catalog) -> list[tuple[str, int, int, str, str]]:
    return cat.conn.execute("select source, start_ns, end_ns, cause, detail from gap order by start_ns").fetchall()


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Path, Catalog]:
    data = tmp_path / "data"
    return data, Catalog.open(data / "meta" / "catalog.db")


def test_steady_one_hertz_data_with_jitter_has_no_gaps(setup: tuple[Path, Catalog]) -> None:
    data, cat = setup
    recv = [T0 + i * S for i in range(600)] + [T0 + 605 * S]  # a 5 s hiccup is not a gap
    _segment(data / "raw", BOOK, T0, recv)
    done = scan_receive_gaps(data, cat, now_ns=T0 + 2 * HOUR)
    assert (done.scanned, done.gaps) == (1, 0) and _gaps(cat) == []
    cat.close()


def test_a_silence_inside_a_segment_is_recorded(setup: tuple[Path, Catalog]) -> None:
    data, cat = setup
    _segment(data / "raw", BOOK, T0, [T0 + S, T0 + 2 * S, T0 + 2 * S + THRESHOLD_NS + 1, T0 + 2 * S + THRESHOLD_NS + 2])
    done = scan_receive_gaps(data, cat, now_ns=T0 + 2 * HOUR)
    assert done.gaps == 1
    assert _gaps(cat) == [(BOOK, T0 + 2 * S, T0 + 2 * S + THRESHOLD_NS + 1, "unknown", "receive_gap")]
    cat.close()


def test_a_silence_across_segments_is_recorded(setup: tuple[Path, Catalog]) -> None:
    """2026-11-09: suspended 03:36:10Z, the next record was in the 06:00 segment."""
    data, cat = setup
    _segment(data / "raw", BOOK, T0, [T0 + 10 * S, T0 + 2170 * S])
    _segment(data / "raw", BOOK, T0 + 3 * HOUR, [T0 + 3 * HOUR + 2782 * S, T0 + 3 * HOUR + 2783 * S])
    scan_receive_gaps(data, cat, now_ns=T0 + 5 * HOUR)
    assert (BOOK, T0 + 2170 * S, T0 + 3 * HOUR + 2782 * S, "unknown", "receive_gap") in _gaps(cat)
    cat.close()


def test_scanning_is_incremental_and_idempotent(setup: tuple[Path, Catalog]) -> None:
    data, cat = setup
    _segment(data / "raw", BOOK, T0, [T0 + S, T0 + 100 * S])
    first = scan_receive_gaps(data, cat, now_ns=T0 + 2 * HOUR)
    again = scan_receive_gaps(data, cat, now_ns=T0 + 2 * HOUR)
    assert (first.scanned, first.gaps) == (1, 1) and (again.scanned, again.gaps) == (0, 0)
    _segment(data / "raw", BOOK, T0 + HOUR, [T0 + HOUR + 500 * S])  # next hour: only the boundary gap is new
    later = scan_receive_gaps(data, cat, now_ns=T0 + 3 * HOUR)
    assert (later.scanned, later.gaps) == (1, 1) and len(_gaps(cat)) == 2
    cat.close()


def test_open_segments_and_event_driven_sources_are_not_scanned(setup: tuple[Path, Catalog]) -> None:
    data, cat = setup
    _segment(data / "raw", BOOK, T0 + HOUR, [T0 + HOUR + S, T0 + HOUR + 900 * S])  # current hour: still open
    _segment(data / "raw", "binance.um.force_order", T0, [T0 + S, T0 + 900 * S])  # quiet markets are normal
    done = scan_receive_gaps(data, cat, now_ns=T0 + HOUR + 1000 * S)
    assert (done.scanned, done.gaps) == (0, 0) and _gaps(cat) == []
    cat.close()


def test_a_segment_that_vanished_after_listing_is_scanned_next_run(
    setup: tuple[Path, Catalog], monkeypatch: pytest.MonkeyPatch
) -> None:
    data, cat = setup
    path = _segment(data / "raw", BOOK, T0, [T0 + S, T0 + 100 * S])
    real = gapscan_module.SegmentStream

    def vanish_then_open(p: Path) -> object:
        p.unlink()  # compress replaced it by the .gz
        return real(p)

    monkeypatch.setattr(gapscan_module, "SegmentStream", vanish_then_open)
    done = scan_receive_gaps(data, cat, now_ns=T0 + 2 * HOUR)
    assert (done.scanned, done.vanished, done.gaps) == (0, 1, 0)
    assert not path.exists()
    cat.close()
