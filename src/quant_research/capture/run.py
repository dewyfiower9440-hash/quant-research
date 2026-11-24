"""Collector processes and their supervisor (design v2.1 §3.1, §12).

    python -m quant_research.capture.run rest        # REST pollers (Binance + Upbit)
    python -m quant_research.capture.run ws          # WebSocket streams (Binance forceOrder)
    python -m quant_research.capture.run supervisor  # starts both, restarts with backoff

Each process holds a single-instance lock, measures the exchange clock itself, writes a heartbeat,
fsyncs its segments every 2 s, records a restart gap from its previous heartbeat, and stops cleanly
when `<data_root>/run/STOP` appears. Exit codes: 3 = Binance IP ban (supervisor stops everything),
4 = location blocked for this process (supervisor retires only this child), 5 = a 1 Hz job only failed for
five minutes (the supervisor restarts the process; at most once per job in six hours).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing
import multiprocessing.process
import os
import sys
import time
from collections.abc import Awaitable, Callable, Coroutine
from concurrent.futures import ProcessPoolExecutor
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx
import structlog
import websockets
from websockets.exceptions import InvalidStatus

from quant_research.adapters.notify import ToastNotifier
from quant_research.capture import binance, upbit
from quant_research.capture.backup import backup_closed_files, snapshot_catalog
from quant_research.capture.clockref import ClockReference
from quant_research.capture.compress import compress_closed_segments
from quant_research.capture.http import Fetcher, HttpStatusError, ThrottledError, VenueGate
from quant_research.capture.lock import AlreadyRunningError, InstanceLock
from quant_research.capture.ratelimit import BannedError, GeoBlockedError
from quant_research.capture.recovery import Gap
from quant_research.capture.sinks import Sinks
from quant_research.capture.thermal import heavy_work_allowed, read_state
from quant_research.config import Settings, load_settings
from quant_research.errors import ExternalServiceError
from quant_research.log import configure_logging
from quant_research.monitor.observe import monitor_once

EXIT_STOP_ALL = 3
EXIT_RETIRE = 4
EXIT_STALE = 5  # a 1 Hz job did nothing but fail for STALE_AFTER_S: the supervisor restarts the process
STOP_FILE = "STOP"
STALE_AFTER_S = 300.0
STALE_MIN_FAILURES = 30  # consecutive failures a stretch needs as well (a clock jump over a standby is not one)
UNIVERSE_REFRESH_MIN_S = 60.0  # the market list is asked for at most this often while a 404 lasts
STALE_RESTART_GAP_NS = 6 * 3_600 * 10**9  # at most one such restart per job in this window
if TYPE_CHECKING:  # the collector process itself never imports Polars (compaction runs in a child)
    from quant_research.data.compact import CompactResult
    from quant_research.data.gapscan import GapScanResult

log = structlog.get_logger(__name__)
POLARS_THREADS = 2  # the collectors share the PC with research jobs and the bulk download


def git_sha(repo: Path) -> str:
    """Read HEAD without spawning git: .git/HEAD -> ref file or packed-refs."""
    head = (repo / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    if not head.startswith("ref: "):
        return head
    ref = head[5:]
    ref_file = repo / ".git" / ref
    if ref_file.exists():
        return ref_file.read_text(encoding="utf-8").strip()
    packed = repo / ".git" / "packed-refs"
    for line in packed.read_text(encoding="utf-8").splitlines() if packed.exists() else []:
        sha, _, name = line.partition(" ")
        if name == ref:
            return sha
    raise FileNotFoundError(f"cannot resolve {ref} in {repo}")


@dataclass
class Health:
    name: str
    path: Path
    started_ns: int = field(default_factory=time.time_ns)
    ok: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    last_ok_ns: dict[str, int] = field(default_factory=lambda: dict[str, int]())  # the monitor's "alive but failing"
    fail_streak: dict[str, int] = field(default_factory=lambda: dict[str, int]())  # consecutive failures per job
    errors: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    last_error: dict[str, str] = field(default_factory=lambda: dict[str, str]())

    def previous_beat_ns(self) -> int | None:
        """`at_ns` of the heartbeat left by the previous run of this process, if any."""
        if not self.path.exists():
            return None
        try:
            body = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # F-017: a crash mid-write left a 0-byte file and every restart died here for 7 days.
            log.warning("heartbeat.unreadable", path=str(self.path), error=str(e))
            return None
        if not isinstance(body, dict):
            log.warning("heartbeat.unreadable", path=str(self.path), error="not a JSON object")
            return None
        at = cast(dict[str, Any], body).get("at_ns")
        return at if isinstance(at, int) else None

    def write(self) -> None:
        """Write through a temp file and rename, so a crash never leaves a truncated heartbeat (F-017)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "name": self.name,
            "pid": os.getpid(),
            "at_ns": time.time_ns(),
            "started_ns": self.started_ns,
            "ok": self.ok,
            "last_ok_ns": self.last_ok_ns,
            "fail_streak": self.fail_streak,
            "errors": self.errors,
            "last_error": self.last_error,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body), encoding="utf-8")
        try:
            os.replace(tmp, self.path)
        except PermissionError:  # Windows: a reader (monitor) holds the file open for a moment
            self.path.write_text(json.dumps(body), encoding="utf-8")
            tmp.unlink(missing_ok=True)

    def succeed(self, job: str) -> None:
        self.ok[job] = self.ok.get(job, 0) + 1
        self.last_ok_ns[job] = time.time_ns()
        self.fail_streak[job] = 0

    def fail(self, job: str, error: Exception) -> str:
        self.errors[job] = self.errors.get(job, 0) + 1
        self.fail_streak[job] = self.fail_streak.get(job, 0) + 1
        self.last_error[job] = f"{type(error).__name__}: {error}"[:500]
        return self.last_error[job]


class StaleJobError(Exception):
    """A job did nothing but fail for its whole limit: the process is alive and useless for it (v2.5 N01: broken
    imports under a running process, a market list gone stale). The process exits and the supervisor starts a new
    one, which reads its code and its lists again."""

    def __init__(self, job: str, failing_for_s: float, last_error: str) -> None:
        super().__init__(f"{job} failed for {failing_for_s:.0f} s without one success: {last_error}")
        self.job = job


@dataclass
class StaleRestarts:
    """One stale restart per job and window, kept in a file so the count survives the restart it caused. Without
    it a venue that is really down would restart the process, and with it the other venue's collection, every
    few minutes."""

    path: Path
    min_gap_ns: int = STALE_RESTART_GAP_NS
    now_ns: Callable[[], int] = time.time_ns

    def allow(self, job: str) -> bool:
        """Never raises (it is called while a job's failure is being handled). A state that cannot be written
        refuses: without the record nothing would stop a restart every few minutes."""
        try:
            raw: object = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            raw = {}
        entries = cast(dict[object, object], raw) if isinstance(raw, dict) else {}
        last = {str(k): v for k, v in entries.items() if isinstance(v, int)}
        now = self.now_ns()
        if job in last and now - last[job] < self.min_gap_ns:
            return False
        last[job] = now
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(last), encoding="utf-8")
        except OSError as e:
            log.warning("collector.stale_state_unwritable", path=str(self.path), error=str(e))
            return False
        return True


def restart_can_help(error: Exception) -> bool:
    """Whether a new process could fix this failure. A venue or network that does not answer (the fetcher gave up
    after its retries: an `ExternalServiceError` that is not a definite HTTP answer) is the same for a new process,
    and restarting would only interrupt the other venue. A definite answer the venue keeps giving (a stale list) or
    an error from our own code (imports broken under the process) is what a restart is for."""
    return isinstance(error, HttpStatusError) or not isinstance(error, ExternalServiceError)


def throttled[T](
    call: Callable[[], Awaitable[T]], *, every_s: float, monotonic: Callable[[], float] = time.monotonic
) -> Callable[[], Coroutine[Any, Any, T]]:
    """`call`, but at most once per `every_s`; in between it raises (the 1 Hz order-book job would otherwise ask
    for the market list every second while a 404 lasts)."""
    last: list[float] = []

    async def limited() -> T:
        now = monotonic()
        if last and now - last[0] < every_s:
            raise ThrottledError(f"throttled: asked {now - last[0]:.0f} s ago, at most once per {every_s:.0f} s")
        last[:] = [now]
        return await call()

    return limited


async def periodic(
    name: str,
    every_s: float,
    job: Callable[[], Awaitable[object]],
    health: Health,
    stop: asyncio.Event,
    *,
    sinks: Sinks | None = None,
    initial_delay_s: float = 0.0,
    restart_after_s: float | None = None,
    restart_min_failures: int | None = None,
    allow_restart: Callable[[str], bool] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Run `job` every `every_s` seconds (wall-clock aligned up to 60 s). A failing iteration (including a
    451 for this job) is logged, counted and written to the incident stream; the loop continues.
    A Binance ban stops the whole process.

    With `restart_after_s`, a job raises `StaleJobError` when it has done nothing but fail for that long, with
    failures a restart can help (`restart_can_help`), at least `restart_min_failures` of them in a row (default
    `STALE_MIN_FAILURES`: two failures either side of a standby are not a stretch), and `allow_restart` agrees.
    A refusal is asked again one limit later."""
    if initial_delay_s:
        await _wait(stop, initial_delay_s)
    min_failures = STALE_MIN_FAILURES if restart_min_failures is None else restart_min_failures
    failing_since: float | None = None
    failures, next_ask = 0, 0.0
    while not stop.is_set():
        try:
            await job()
            health.succeed(name)
            failing_since, failures = None, 0
        except BannedError:
            stop.set()
            raise
        except Exception as e:  # task boundary: record, keep collecting other data
            message = health.fail(name, e)
            log.exception("collector.job_failed", job=name)
            if sinks is not None:
                sinks.write(f"collector.incidents.{health.name}", {"job": name, "error": message})
            if restart_after_s is not None and restart_can_help(e):
                now = monotonic()
                if failing_since is None:
                    failing_since, next_ask = now, now + restart_after_s
                failures += 1
                if now >= next_ask and failures >= min_failures:
                    next_ask = now + restart_after_s
                    if allow_restart is None or allow_restart(name):
                        raise StaleJobError(name, now - failing_since, message) from e
            else:
                failing_since, failures = None, 0  # the venue is not answering: not a stretch a restart ends
        await _wait(stop, every_s - (time.time() % every_s) if every_s <= 60 else every_s)


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=max(0.0, seconds))
    except TimeoutError:
        return


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _make_sinks(settings: Settings) -> Sinks:
    return Sinks(
        raw_root=settings.data_root / "raw",
        collector_version=version("quant-research"),
        git_sha=git_sha(repo_root()),
        clock=ClockReference(),
    )


def thermal_gated(
    settings: Settings, sinks: Sinks, name: str, job: Callable[[], Awaitable[object]]
) -> Callable[[], Awaitable[None]]:
    """Decision 3: heavy disk work pauses while the SSD is critical (state written by the monitor task)."""

    async def gated() -> None:
        state = read_state(settings.data_root / "run" / "ssd_state.json")
        allowed, why = heavy_work_allowed(state, now_ns=time.time_ns())
        if not allowed:
            sinks.write("collector.incidents.rest", {"job": name, "thermal_pause": why})
            return
        await job()

    return gated


def _start_process(settings: Settings, health: Health, sinks: Sinks) -> None:
    """Restart gap from this process's previous heartbeat (start unknown on a first run)."""
    previous = health.previous_beat_ns()
    sinks.record_gap(Gap(f"process.{health.name}", previous, time.time_ns(), "restart"))
    health.write()


def _common_jobs(settings: Settings, health: Health, sinks: Sinks, stop: asyncio.Event) -> list[Awaitable[None]]:
    stop_file = settings.data_root / "run" / STOP_FILE

    async def watch_stop_file() -> None:
        if stop_file.exists():
            stop.set()

    async def beat() -> None:
        health.write()

    async def sync() -> None:
        sinks.sync_due()

    return [
        periodic("stop_file", 1, watch_stop_file, health, stop),
        periodic("fsync", 2, sync, health, stop, sinks=sinks),
        periodic("heartbeat", 10, beat, health, stop),
    ]


async def rest_main(
    settings: Settings,
    stop: asyncio.Event,
    health: Health,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    sinks = _make_sinks(settings)
    _start_process(settings, health, sinks)
    timeout = httpx.Timeout(10.0)
    binance_gate = VenueGate()  # limits are per IP: spot and futures share one gate
    async with (
        httpx.AsyncClient(timeout=timeout, transport=transport) as cb,
        httpx.AsyncClient(timeout=timeout, transport=transport) as cu,
    ):
        f_fut, f_spot = Fetcher(cb, "binance", gate=binance_gate), Fetcher(cb, "binance", gate=binance_gate)
        f_up = Fetcher(cu, "upbit")
        try:
            await binance.measure_clock(f_spot, sinks.clock, sinks)
            fut_syms = set(await binance.top_symbols(f_fut, settings.binance_top_n))
            try:
                up_mkts = await upbit.top_krw_markets(f_up, settings.upbit_top_n)
            except ExternalServiceError as e:
                # Upbit down at start must not keep the Binance sources from starting (review of v2.5 step 1: a
                # restart during an Upbit outage died here until Upbit came back). The 1 Hz job fills the list.
                health.fail("upbit_universe", e)
                log.warning("collector.upbit_universe_unavailable", error=str(e))
                up_mkts = []
            state = upbit.TradeCursorState.load(settings.data_root / "state" / "upbit_trades.json")
            sinks.write("collector.universe", {"binance_um": sorted(fut_syms), "upbit_krw": up_mkts})

            stale = StaleRestarts(settings.data_root / "run" / f"{health.name}.stale.json")

            async def trades() -> None:
                for m in list(up_mkts):  # a copy: the list is replaced in place when the universe is refreshed
                    await upbit.trades_catchup(f_up, sinks, m, state, stop=stop)

            def upbit_markets_now() -> Awaitable[list[str]]:
                return upbit.top_krw_markets(f_up, settings.upbit_top_n)

            upbit_markets_limited = throttled(upbit_markets_now, every_s=UNIVERSE_REFRESH_MIN_S)

            async def refresh_upbit_universe() -> None:
                """Daily: delistings and new listings reach the 1 Hz request without a restart (v2.5 N01)."""
                fresh = await upbit_markets_now()
                if fresh and fresh != up_mkts:
                    up_mkts[:] = fresh
                    sinks.write("collector.universe", {"upbit_krw": list(up_mkts), "reason": "daily_refresh"})

            def metric_failed(source: str, symbol: str, e: Exception) -> None:
                message = health.fail(f"metrics:{source}:{symbol}", e)
                sinks.write(
                    "collector.incidents.rest",
                    {"job": "metrics", "source": source, "symbol": symbol, "error": message},
                )

            unsupported: set[tuple[str, str]] = set()  # e.g. basis of TradFi perps: reported once, then skipped

            def metrics_pass(days: float) -> Callable[[], Awaitable[int]]:
                """ADR-007: full 30-day window daily (revisions), last page (~41.7 h) hourly (freshness)."""

                def run_pass() -> Awaitable[int]:
                    return binance.metrics_refetch(
                        f_fut,
                        sinks,
                        sorted(fut_syms),
                        now_ms=lambda: time.time_ns() // 1_000_000,
                        days=days,
                        stop=stop,
                        on_error=metric_failed,
                        unsupported=unsupported,
                    )

                return run_pass

            def heavy(name: str, job: Callable[[], Awaitable[object]]) -> Callable[[], Awaitable[None]]:
                return thermal_gated(settings, sinks, name, job)

            async def backup() -> None:
                done = await asyncio.to_thread(
                    backup_closed_files, settings.data_root, settings.backup_root, now_ns=time.time_ns()
                )
                # plan step 4: consistent catalog copy (VACUUM INTO), one per UTC day, 7 days kept
                snap = await asyncio.to_thread(
                    snapshot_catalog, settings.data_root, settings.backup_root, now_ns=time.time_ns()
                )
                sinks.write(
                    "collector.backup",
                    {
                        "copied": len(done.copied),
                        "unchanged": done.unchanged,
                        "vanished": done.vanished,
                        "catalog_snapshot": None if snap is None else snap.path.name,
                        "catalog_rows": None if snap is None else sum(snap.tables.values()),
                    },
                )

            async def compact() -> None:
                done = await asyncio.to_thread(compact_in_child, settings.data_root)
                sinks.write(
                    "collector.compaction", {"compacted": done.compacted, "rows": done.rows, "failed": len(done.failed)}
                )
                for path, why in done.failed:
                    sinks.write("collector.incidents.rest", {"job": "compact", "file": path, "error": why})
                # silences of the 1 Hz sources (host suspend, outages) become catalog gaps (step 10)
                scan = await asyncio.to_thread(gapscan_in_child, settings.data_root)
                sinks.write(
                    "collector.gapscan",
                    {"scanned": scan.scanned, "gaps": scan.gaps, "vanished": scan.vanished, "imported": scan.imported},
                )

            notifier = ToastNotifier(repo_root() / "tools" / "notify.ps1", settings.data_root / "run" / "alerts.json")
            monitor_since = {"ns": health.started_ns}

            async def monitor() -> None:
                """Plan step 10: host/collector checks -> catalog incidents -> toast for new ones."""
                checked = time.time_ns()
                fresh = await asyncio.to_thread(
                    monitor_once, settings.data_root, sinks.clock, notifier, since_ns=monitor_since["ns"]
                )
                monitor_since["ns"] = checked
                if fresh:
                    sinks.write(
                        "collector.incidents.monitor",
                        [{"key": f.key, "severity": f.severity, "detail": f.detail} for f in fresh],
                    )

            async def compress() -> None:
                done = await asyncio.to_thread(
                    compress_closed_segments, settings.data_root / "raw", now_ns=time.time_ns()
                )
                for path, why in done.skipped:
                    sinks.write("collector.incidents.rest", {"job": "compress", "file": str(path), "error": why})

            jobs = [
                periodic(
                    "clock", 60, lambda: binance.measure_clock(f_spot, sinks.clock, sinks), health, stop, sinks=sinks
                ),
                periodic(
                    "book_ticker",
                    1,
                    lambda: binance.book_ticker_once(f_fut, sinks, fut_syms),
                    health,
                    stop,
                    sinks=sinks,
                    restart_after_s=STALE_AFTER_S,
                    restart_min_failures=STALE_MIN_FAILURES,
                    allow_restart=stale.allow,
                ),
                periodic(
                    "upbit_orderbook",
                    1,
                    lambda: upbit.orderbook_refreshing(f_up, sinks, up_mkts, upbit_markets_limited),
                    health,
                    stop,
                    sinks=sinks,
                    restart_after_s=STALE_AFTER_S,
                    restart_min_failures=STALE_MIN_FAILURES,
                    allow_restart=stale.allow,
                ),
                periodic(
                    "upbit_universe",
                    86_400,
                    refresh_upbit_universe,
                    health,
                    stop,
                    sinks=sinks,
                    initial_delay_s=86_400,
                ),
                periodic(
                    "reference",
                    86_400,
                    lambda: binance.reference_snapshots(f_fut, f_spot, sinks),
                    health,
                    stop,
                    sinks=sinks,
                ),
                periodic(
                    "upbit_markets", 86_400, lambda: upbit.market_snapshot(f_up, sinks), health, stop, sinks=sinks
                ),
                periodic("upbit_trades", 1_800, trades, health, stop, sinks=sinks, initial_delay_s=5),
                periodic(
                    "metrics_30d",
                    86_400,
                    heavy("metrics_30d", metrics_pass(30)),
                    health,
                    stop,
                    sinks=sinks,
                    initial_delay_s=30,
                ),
                periodic(
                    "metrics_recent",
                    3_600,
                    metrics_pass(binance.PAGE_ROWS * binance.PERIOD_MS / 86_400_000),
                    health,
                    stop,
                    sinks=sinks,
                    initial_delay_s=900,
                ),
                periodic("compress", 600, heavy("compress", compress), health, stop, sinks=sinks, initial_delay_s=120),
                periodic("compact", 600, heavy("compact", compact), health, stop, sinks=sinks, initial_delay_s=300),
                periodic("backup", 3_600, heavy("backup", backup), health, stop, sinks=sinks, initial_delay_s=1_200),
                periodic("monitor", 300, monitor, health, stop, sinks=sinks, initial_delay_s=90),
                *_common_jobs(settings, health, sinks, stop),
            ]
            await asyncio.gather(*jobs)
        finally:
            sinks.close()


WsConnect = Callable[..., AbstractAsyncContextManager[Any]]


async def ws_main(
    settings: Settings,
    stop: asyncio.Event,
    health: Health,
    *,
    connect: WsConnect = websockets.connect,
    heartbeat_s: float = 10.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    sinks = _make_sinks(settings)
    _start_process(settings, health, sinks)
    last_beat = float("-inf")

    def beat() -> None:
        nonlocal last_beat
        if time.monotonic() - last_beat >= heartbeat_s:  # F-004: also while messages keep arriving
            health.write()
            last_beat = time.monotonic()

    async def stream() -> None:
        backoff = 1.0
        disconnected_ns: int | None = None
        cause = "reconnect"
        while not stop.is_set():
            opened = time.monotonic()
            try:
                async with connect(binance.FORCE_ORDER_URL, open_timeout=10, max_size=2**22) as ws:
                    backoff = 1.0
                    if disconnected_ns is not None:
                        sinks.record_gap(Gap("binance.um.force_order", disconnected_ns, time.time_ns(), cause))
                        disconnected_ns = None
                    while not stop.is_set() and time.monotonic() - opened < 23 * 3600:  # renew before 24 h cutoff
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=10)
                        except TimeoutError:
                            beat()
                            continue
                        binance.handle_force_order(sinks, raw)
                        health.ok["force_order"] = health.ok.get("force_order", 0) + 1
                        beat()
                    disconnected_ns, cause = time.time_ns(), "renewal"
            except InvalidStatus as e:
                status = e.response.status_code
                if status == 418:
                    raise BannedError("HTTP 418 on Binance WebSocket handshake") from e
                if status == 451:
                    raise GeoBlockedError("HTTP 451 on Binance WebSocket handshake") from e
                disconnected_ns, cause = disconnected_ns or time.time_ns(), "reconnect"
                await _ws_failed(health, sinks, stop, e, backoff)
                backoff = min(300.0, backoff * 2)
            except (OSError, websockets.WebSocketException) as e:
                disconnected_ns, cause = disconnected_ns or time.time_ns(), "reconnect"
                await _ws_failed(health, sinks, stop, e, backoff)
                backoff = min(300.0, backoff * 2)
        stop.set()

    timeout = httpx.Timeout(10.0)
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as cb:
        f_spot = Fetcher(cb, "binance")
        try:
            await asyncio.gather(
                stream(),
                periodic(
                    "clock", 60, lambda: binance.measure_clock(f_spot, sinks.clock, sinks), health, stop, sinks=sinks
                ),
                *_common_jobs(settings, health, sinks, stop),
            )
        finally:
            sinks.close()


async def _ws_failed(health: Health, sinks: Sinks, stop: asyncio.Event, e: Exception, backoff: float) -> None:
    message = health.fail("force_order", e)
    sinks.write("collector.incidents.ws", {"job": "force_order", "error": message})
    await _wait(stop, backoff)


def next_backoff(previous_s: float, healthy_for_s: float) -> float:
    """Restart delay: doubles up to 5 min; resets after 10 min of healthy running."""
    return 5.0 if healthy_for_s >= 600 else min(300.0, max(5.0, previous_s * 2))


def _compact_job(data_root: Path) -> CompactResult:
    """Runs inside the short-lived compaction process; Polars is imported there, never in the collector."""
    from quant_research.data.compact import compact_once

    return compact_once(data_root)


def _gapscan_job(data_root: Path) -> GapScanResult:
    from quant_research.data.gapscan import scan_once

    return scan_once(data_root)


def gapscan_in_child(data_root: Path) -> GapScanResult:
    """Receive-gap scan in a short-lived spawned process (same isolation as compaction)."""
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
        return pool.submit(_gapscan_job, data_root).result()


def compact_in_child(data_root: Path) -> CompactResult:
    """Compaction in a fresh spawned process that exits afterwards, so its peak memory (Polars heap,
    fragmented Python arenas) goes back to the OS instead of staying in the rest worker (review #1)."""
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
        return pool.submit(_compact_job, data_root).result()


def _child_entry(role: str) -> None:
    """Child process target (multiprocessing spawn on Windows imports this module fresh)."""
    main([role])


SHORT_RUN_S = 120.0  # a child that exits sooner than this counts towards a restart loop (OPS-009)
STATE_EVERY_S = 60.0  # the supervisor rewrites its state at least this often (the monitor drops older state)


def update_streak(streaks: dict[str, int], name: str, *, ran_s: float, short_s: float = SHORT_RUN_S) -> None:
    """Consecutive short runs of one child; a run of `short_s` or longer ends the streak."""
    streaks[name] = streaks.get(name, 0) + 1 if ran_s < short_s else 0


def reset_recovered(streaks: dict[str, int], *, running_for_s: dict[str, float], short_s: float = SHORT_RUN_S) -> bool:
    """A child that has been running for `short_s` has recovered: its streak ends (A6-A12 review: the streak was
    only touched on an exit, so a recovered child kept its restart-loop incident open). True when anything changed."""
    changed = False
    for name, ran in running_for_s.items():
        if ran >= short_s and streaks.get(name, 0) > 0:
            streaks[name] = 0
            changed = True
    return changed


def write_supervisor_state(path: Path, streaks: dict[str, int]) -> None:
    """run/supervisor.state.json for the monitor. Temporary file + rename; on Windows the rename is refused while
    the monitor reads the file, then the file is written in place (as `Health.write`). Never raises: a failed
    state write must not stop the supervisor and with it every collector (A6-A12 review)."""
    body = json.dumps({"streaks": streaks, "at_ns": time.time_ns()})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        try:
            os.replace(tmp, path)
        except PermissionError:
            path.write_text(body, encoding="utf-8")
            tmp.unlink(missing_ok=True)
    except OSError as e:
        log.warning("supervisor.state_write_failed", path=str(path), error=str(e))


@dataclass
class SupervisorResult:
    restarts: dict[str, int]
    retired: set[str]
    stopped_all: bool


def supervise(
    settings: Settings,
    children: list[str],
    *,
    max_runtime_s: float | None = None,
    target: Callable[[str], None] = _child_entry,
    poll_s: float = 5.0,
    grace_s: float = 30.0,
) -> SupervisorResult:
    """Start children; restart crashed ones after a backoff (scheduled, never blocking the loop); retire a
    child that exits 4; stop everything when one exits 3 (Binance ban). On exit, ask children to stop via
    the STOP file so they close their segments, and only terminate stragglers after `grace_s`."""
    ctx = multiprocessing.get_context("spawn")
    stop_file = settings.data_root / "run" / STOP_FILE
    stop_file.unlink(missing_ok=True)
    procs: dict[str, multiprocessing.process.BaseProcess] = {}
    started: dict[str, float] = {}
    restart_at: dict[str, float] = {}
    delay: dict[str, float] = dict.fromkeys(children, 5.0)
    result = SupervisorResult(dict.fromkeys(children, 0), set(), stopped_all=False)
    streaks: dict[str, int] = dict.fromkeys(children, 0)
    state_file = settings.data_root / "run" / "supervisor.state.json"
    write_supervisor_state(state_file, streaks)
    last_state = time.monotonic()
    t_end = time.monotonic() + max_runtime_s if max_runtime_s else None

    def spawn(name: str) -> None:
        p = ctx.Process(target=target, args=(name,), name=f"qr-{name}")
        p.start()
        procs[name] = p
        started[name] = time.monotonic()
        log.info("supervisor.started", child=name, pid=p.pid)

    for name in children:
        spawn(name)
    try:
        while (t_end is None or time.monotonic() < t_end) and not result.stopped_all:
            time.sleep(poll_s)
            if stop_file.exists():  # operator stop: children exit on their own; do not restart them
                log.info("supervisor.stop_file", path=str(stop_file))
                break
            now = time.monotonic()
            alive = {n: now - started[n] for n in children if n in procs and procs[n].exitcode is None}
            if reset_recovered(streaks, running_for_s=alive) or now - last_state >= STATE_EVERY_S:
                write_supervisor_state(state_file, streaks)  # also periodically: the monitor ignores a stale state file
                last_state = now
            for name in children:
                if name in result.retired:
                    continue
                if name in restart_at:
                    if now >= restart_at[name]:
                        del restart_at[name]
                        result.restarts[name] += 1
                        spawn(name)
                    continue
                code = procs[name].exitcode
                if code is None:
                    continue
                ran = now - started[name]
                log.warning("supervisor.child_exited", child=name, code=code, ran_s=round(ran, 1))
                update_streak(streaks, name, ran_s=ran)
                write_supervisor_state(state_file, streaks)
                if code == EXIT_STOP_ALL:
                    log.error("supervisor.stop_all", child=name, reason="Binance IP ban; manual action needed")
                    result.stopped_all = True
                    break
                if code == EXIT_RETIRE:
                    result.retired.add(name)
                    log.error("supervisor.child_retired", child=name, reason="location blocked")
                    continue
                delay[name] = next_backoff(delay[name], ran)
                restart_at[name] = now + delay[name]
    finally:
        stop_file.parent.mkdir(parents=True, exist_ok=True)
        stop_file.write_text("stop", encoding="utf-8")
        deadline = time.monotonic() + grace_s
        for p in procs.values():
            p.join(timeout=max(0.0, deadline - time.monotonic()))
        for p in procs.values():
            if p.is_alive():
                log.warning("supervisor.terminate", child=p.name)
                p.terminate()
                p.join(timeout=10)
    return result


LOG_DIR_ENV = "QR_LOG_DIR"


def redirect_log(log_dir: Path | None, role: str) -> Path | None:
    """Under Task Scheduler the collector runs as pythonw.exe (no console, sys.stderr is None). Send this
    process's log to <log_dir>/<role>.log; the directory is exported so spawned children do the same."""
    chosen = log_dir or (Path(os.environ[LOG_DIR_ENV]) if os.environ.get(LOG_DIR_ENV) else None)
    if chosen is None:
        return None
    chosen.mkdir(parents=True, exist_ok=True)
    os.environ[LOG_DIR_ENV] = str(chosen)
    path = chosen / f"{role}.log"
    sys.stderr = path.open("a", encoding="utf-8", buffering=1)
    return path


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="quant_research.capture.run")
    ap.add_argument("role", choices=["rest", "ws", "supervisor"])
    ap.add_argument("--duration", type=float, default=None, help="stop after N seconds (tests, timed runs)")
    ap.add_argument("--log-dir", type=Path, default=None, help="write this process's log to <dir>/<role>.log")
    a = ap.parse_args(argv)
    # Inherited by every spawned child; Polars sizes its thread pool from it at import (review #1).
    os.environ.setdefault("POLARS_MAX_THREADS", str(POLARS_THREADS))
    redirect_log(a.log_dir, a.role)
    settings = load_settings()
    configure_logging(settings.log_level, json=True)
    run_dir = settings.data_root / "run"
    try:
        with InstanceLock(run_dir / f"{a.role}.lock"):
            if a.role == "supervisor":
                supervise(settings, ["rest", "ws"], max_runtime_s=a.duration)
                sys.exit(0)
            health = Health(a.role, run_dir / f"{a.role}.heartbeat.json")
            stop = asyncio.Event()
            entry = rest_main if a.role == "rest" else ws_main
            sys.exit(asyncio.run(run_role(entry, settings, stop, health, a.duration)))
    except AlreadyRunningError as e:
        log.error("collector.already_running", error=str(e))
        sys.exit(2)


async def run_role(
    entry: Callable[[Settings, asyncio.Event, Health], Awaitable[None]],
    settings: Settings,
    stop: asyncio.Event,
    health: Health,
    duration: float | None,
) -> int:
    """One collector role to its exit code (0 clean, 3 ban, 4 location blocked, 5 stale job)."""
    if duration:
        asyncio.get_running_loop().call_later(duration, stop.set)
    try:
        await entry(settings, stop, health)
    except StaleJobError as e:
        health.last_error["fatal"] = str(e)
        health.write()
        log.error("collector.stale", job=e.job, error=str(e))
        return EXIT_STALE
    except BannedError as e:
        health.last_error["fatal"] = str(e)
        health.write()
        log.error("collector.stopped", error=str(e))
        return EXIT_STOP_ALL
    except GeoBlockedError as e:
        health.last_error["fatal"] = str(e)
        health.write()
        log.error("collector.retired", error=str(e))
        return EXIT_RETIRE
    health.write()
    return 0


if __name__ == "__main__":
    main()
