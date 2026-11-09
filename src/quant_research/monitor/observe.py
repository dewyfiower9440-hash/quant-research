"""Monitor I/O shell (plan step 10): gather an Observation from the host and the run folder, evaluate,
reconcile incidents in the catalog, log and notify the newly opened ones.

Runs inside the rest worker every few minutes (no Polars). The rest process cannot watch itself: a
crashed rest worker is restarted by the supervisor, a hung one is not detected here (documented limit).
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from quant_research.adapters.jobhost import memory_sample
from quant_research.capture.clockref import ClockReference
from quant_research.capture.thermal import read_state
from quant_research.catalog.store import Catalog
from quant_research.monitor.checks import Finding, Observation, Thresholds, evaluate
from quant_research.monitor.incidents import reconcile

Notifier = Callable[[list[Finding]], None]
WATCHED_HEARTBEATS = ("ws",)


def _heartbeat_ns(path: Path) -> int | None:
    try:
        body = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):  # missing, or caught mid-write: treated as missing
        return None
    at = body.get("at_ns")
    return at if isinstance(at, int) else None


def observe(data_root: Path, catalog: Catalog, clock: ClockReference, *, since_ns: int, now_ns: int) -> Observation:
    run = data_root / "run"
    try:
        ram: int | None = memory_sample()["available_ram"]
    except OSError:
        ram = None  # not Windows
    try:
        ssd = read_state(run / "ssd_state.json")
    except ValueError:
        ssd = None
    gaps = [
        (str(r[0]), int(r[1]), int(r[2]))
        for r in catalog.conn.execute(
            "select source, start_ns, end_ns from gap where detail = 'receive_gap' and recorded_ns > ? "
            "order by start_ns",
            (since_ns,),
        )
    ]
    return Observation(
        now_ns=now_ns,
        heartbeats={name: _heartbeat_ns(run / f"{name}.heartbeat.json") for name in WATCHED_HEARTBEATS},
        free_disk=shutil.disk_usage(data_root).free,
        available_ram=ram,
        clock_offset_ms=clock.offset_ms,
        clock_measured_ns=clock.measured_ns,
        ssd_state_ns=None if ssd is None else ssd.at_ns,
        ssd_level=None if ssd is None else ssd.level,
        receive_gaps=gaps,
    )


def monitor_once(
    data_root: Path,
    clock: ClockReference,
    notify: Notifier,
    *,
    since_ns: int,
    thresholds: Thresholds | None = None,
    now_ns: Callable[[], int] = time.time_ns,
) -> list[Finding]:
    """One monitor pass; returns the findings that were notified (new or escalated incidents)."""
    now = now_ns()
    catalog = Catalog.open(data_root / "meta" / "catalog.db")
    try:
        obs = observe(data_root, catalog, clock, since_ns=since_ns, now_ns=now)
        fresh = reconcile(catalog, evaluate(obs, thresholds or Thresholds()), now_ns=now)
    finally:
        catalog.close()
    if fresh:
        stamp = datetime.fromtimestamp(now / 1e9, UTC).isoformat(timespec="seconds")
        with (data_root / "run" / "alerts.log").open("a", encoding="utf-8") as fh:
            for f in fresh:
                fh.write(json.dumps({"at": stamp, "key": f.key, "severity": f.severity, "detail": f.detail}) + "\n")
        notify(fresh)
    return fresh
