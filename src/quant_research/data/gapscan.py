"""Receive-gap scan of 1 Hz sources (plan step 10; docs/plans/2026-11-09-step10-gaps-backup.md).

Restart gaps are recorded by the collectors themselves, but a process that is merely paused (laptop
Modern Standby 2026-11-09 03:36-06:45Z) or whose requests all fail (Upbit ConnectTimeouts 2026-11-08
17:02-20:48Z) records nothing. A 1 Hz poller must produce a record about every second, so any silence
longer than THRESHOLD_NS between consecutive receive times, inside a segment or across segments, is a
gap (cause `unknown`, detail `receive_gap`: suspend, network and stalls cannot be told apart here).

Only closed segments are scanned (hour ended at least a minute ago); each is read once in constant
memory and recorded in `gapscan_segment` together with its gaps in one transaction. Event-driven
sources (forceOrder) are not scanned: silence there is normal in quiet markets.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from quant_research.capture.compress import hour_end_ns
from quant_research.catalog.store import Catalog
from quant_research.data.lake import SegmentStream, segment_stem

THRESHOLD_NS = 30 * 10**9  # initial prior: 30 missed 1 Hz samples; re-tune from the recorded gaps
CADENCE_SOURCES = ("binance.um.book_ticker_1s", "upbit.orderbook_l1_1s")
DETAIL = "receive_gap"


@dataclass
class GapScanResult:
    scanned: int = 0
    gaps: int = 0
    vanished: int = 0
    imported: int = 0  # collector gap-log lines (restart/reconnect) newly loaded into the catalog


def _closed_segments(raw_root: Path, dataset_id: str, now_ns: int) -> list[Path]:
    out: list[tuple[str, int, Path]] = []
    for path in (raw_root / dataset_id).glob("*/*.jsonl*"):
        if path.name.endswith(".tmp"):
            continue
        name = path.name.removesuffix(".gz")
        end = hour_end_ns(name)
        if end is None or end + 60 * 10**9 > now_ns:
            continue
        hour, _, created = name.removesuffix(".jsonl").rpartition("_")
        out.append((hour, int(created) if created.isdigit() else 0, path))
    return [p for _h, _c, p in sorted(out)]


def scan_receive_gaps(data_root: Path, catalog: Catalog, *, now_ns: int) -> GapScanResult:
    result = GapScanResult()
    done = {str(r[0]) for r in catalog.conn.execute("select stem from gapscan_segment")}
    for dataset_id in CADENCE_SOURCES:
        for path in _closed_segments(data_root / "raw", dataset_id, now_ns):
            stem = segment_stem(path)
            if stem in done:
                continue
            try:
                stream = SegmentStream(path)
                recv = [rec.recv_ns for rec in stream]
            except FileNotFoundError:
                result.vanished += 1  # replaced by its .gz after listing; scanned next run
                continue
            recv.sort()
            spans = [(a, b) for a, b in pairwise(recv) if b - a > THRESHOLD_NS]
            if recv:
                row = catalog.conn.execute(
                    "select max(last_recv_ns) from gapscan_segment where dataset_id = ? and last_recv_ns <= ?",
                    (dataset_id, recv[0]),
                ).fetchone()
                prev = None if row is None or row[0] is None else int(row[0])
                if prev is not None and recv[0] - prev > THRESHOLD_NS:
                    spans.insert(0, (prev, recv[0]))
            now = time.time_ns()
            with catalog.transaction():
                catalog.conn.execute(
                    "insert into gapscan_segment(stem, dataset_id, first_recv_ns, last_recv_ns, records, scanned_ns) "
                    "values (?,?,?,?,?,?)",
                    (stem, dataset_id, recv[0] if recv else None, recv[-1] if recv else None, len(recv), now),
                )
                for lo, hi in spans:
                    cur = catalog.conn.execute(
                        "insert or ignore into gap(source, start_ns, end_ns, cause, detail, recorded_ns) "
                        "values (?,?,?,?,?,?)",
                        (dataset_id, lo, hi, "unknown", DETAIL, now),
                    )
                    result.gaps += cur.rowcount
            done.add(stem)
            result.scanned += 1
    return result


def scan_once(data_root: Path) -> GapScanResult:
    """Open the catalog, scan, close: for the compaction child process (SQLite objects stay local).
    Also loads the collectors' own gap logs (`raw/_gaps`, restart/reconnect; idempotent by the gap
    identity index) so every gap reaches the catalog without a manual bootstrap run."""
    catalog = Catalog.open(data_root / "meta" / "catalog.db")
    try:
        with catalog.transaction():
            imported = catalog.import_gaps(data_root / "raw").imported
        result = scan_receive_gaps(data_root, catalog, now_ns=time.time_ns())
        result.imported = imported
        return result
    finally:
        catalog.close()
