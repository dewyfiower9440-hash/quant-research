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
OPTIONAL_HEARTBEATS = ("paper",)  # watched once it wrote a heartbeat OR its task is registered (run/<name>.expected)


SUPERVISOR_STATE_STALE_NS = 600 * 10**9  # older state = no supervisor running: its streaks are not current


def _restart_streaks(path: Path, now_ns: int) -> dict[str, int]:
    try:
        body = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):  # no supervisor state yet, or caught mid-write
        return {}
    at = body.get("at_ns")
    if not isinstance(at, int) or now_ns - at > SUPERVISOR_STATE_STALE_NS:
        return {}
    raw = body.get("streaks")
    if not isinstance(raw, dict):
        return {}
    return {str(k): int(v) for k, v in cast(dict[object, object], raw).items() if isinstance(v, int)}


def _heartbeat_ns(path: Path) -> int | None:
    try:
        body = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):  # missing, or caught mid-write: treated as missing
        return None
    at = body.get("at_ns")
    return at if isinstance(at, int) else None


WATCHED_JOBS = ("book_ticker", "upbit_orderbook")  # the 1 Hz sources that cannot be fetched again


HEARTBEAT_CURRENT_NS = 120 * 10**9  # an older heartbeat is behind (resume from standby): it says nothing of now
STALE_MIN_FAILURES = 30  # the same run of failures the collector asks for before it restarts itself


def _job_ok_ns(path: Path, now_ns: int) -> dict[str, int]:
    """Last success per watched job from the rest heartbeat; a job with none yet counts from the process start.
    Empty when the heartbeat is missing, unreadable, behind, or of the format without `last_ok_ns`."""
    try:
        body = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    started, at_ns, raw = body.get("started_ns"), body.get("at_ns"), body.get("last_ok_ns")
    if not isinstance(started, int) or not isinstance(at_ns, int) or not isinstance(raw, dict):
        return {}
    if now_ns - at_ns > HEARTBEAT_CURRENT_NS:
        return {}
    last = cast(dict[object, object], raw)
    raw_streak = body.get("fail_streak")
    streak = cast(dict[object, object], raw_streak) if isinstance(raw_streak, dict) else {}
    out: dict[str, int] = {}
    for job in WATCHED_JOBS:
        failing = streak.get(job)
        if not isinstance(failing, int) or failing < STALE_MIN_FAILURES:
            continue  # an old last success alone is not failing (a standby leaves one; so does a job not yet run)
        at = last.get(job)
        out[job] = at if isinstance(at, int) else started
    return out


def _stale_restarts(path: Path) -> dict[str, int]:
    """run/rest.stale.json (written by `capture.run.StaleRestarts`): job -> time of its last stale restart."""
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in cast(dict[object, object], raw).items() if isinstance(v, int)}


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
        heartbeats={
            **{name: _heartbeat_ns(run / f"{name}.heartbeat.json") for name in WATCHED_HEARTBEATS},
            **{
                name: _heartbeat_ns(run / f"{name}.heartbeat.json")
                for name in OPTIONAL_HEARTBEATS
                if (run / f"{name}.heartbeat.json").exists() or (run / f"{name}.expected").exists()
            },
        },
        free_disk=shutil.disk_usage(data_root).free,
        available_ram=ram,
        clock_offset_ms=clock.offset_ms,
        clock_measured_ns=clock.measured_ns,
        ssd_state_ns=None if ssd is None else ssd.at_ns,
        ssd_level=None if ssd is None else ssd.level,
        receive_gaps=gaps,
        restart_streaks=_restart_streaks(run / "supervisor.state.json", now_ns),
        job_ok_ns=_job_ok_ns(run / "rest.heartbeat.json", now_ns),
        stale_restarts=_stale_restarts(run / "rest.stale.json"),
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
