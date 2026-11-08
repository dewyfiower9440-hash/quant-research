"""Collector processes and their supervisor (design v2.1 §3.1, §12).

    python -m quant_research.capture.run rest        # REST pollers (Binance + Upbit)
    python -m quant_research.capture.run ws          # WebSocket streams (Binance forceOrder)
    python -m quant_research.capture.run supervisor  # starts both, restarts with backoff

Each process holds a single-instance lock, measures the exchange clock itself, writes a heartbeat,
fsyncs its segments every 2 s, records a restart gap from its previous heartbeat, and stops cleanly
when `<data_root>/run/STOP` appears. Exit codes: 3 = Binance IP ban (supervisor stops everything),
4 = location blocked for this process (supervisor retires only this child).
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
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast

import httpx
import structlog
import websockets
from websockets.exceptions import InvalidStatus

from quant_research.capture import binance, upbit
from quant_research.capture.backup import backup_closed_files
from quant_research.capture.clockref import ClockReference
from quant_research.capture.compress import compress_closed_segments
from quant_research.capture.http import Fetcher, VenueGate
from quant_research.capture.lock import AlreadyRunningError, InstanceLock
from quant_research.capture.ratelimit import BannedError, GeoBlockedError
from quant_research.capture.recovery import Gap
from quant_research.capture.sinks import Sinks
from quant_research.config import Settings, load_settings
from quant_research.log import configure_logging

EXIT_STOP_ALL = 3
EXIT_RETIRE = 4
STOP_FILE = "STOP"
log = structlog.get_logger(__name__)


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
    errors: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    last_error: dict[str, str] = field(default_factory=lambda: dict[str, str]())

    def previous_beat_ns(self) -> int | None:
        """`at_ns` of the heartbeat left by the previous run of this process, if any."""
        if not self.path.exists():
            return None
        body = cast(dict[str, Any], json.loads(self.path.read_text(encoding="utf-8")))
        at = body.get("at_ns")
        return at if isinstance(at, int) else None

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "name": self.name,
            "pid": os.getpid(),
            "at_ns": time.time_ns(),
            "started_ns": self.started_ns,
            "ok": self.ok,
            "errors": self.errors,
            "last_error": self.last_error,
        }
        self.path.write_text(json.dumps(body), encoding="utf-8")

    def fail(self, job: str, error: Exception) -> str:
        self.errors[job] = self.errors.get(job, 0) + 1
        self.last_error[job] = f"{type(error).__name__}: {error}"[:500]
        return self.last_error[job]


async def periodic(
    name: str,
    every_s: float,
    job: Callable[[], Awaitable[object]],
    health: Health,
    stop: asyncio.Event,
    *,
    sinks: Sinks | None = None,
    initial_delay_s: float = 0.0,
) -> None:
    """Run `job` every `every_s` seconds (wall-clock aligned up to 60 s). A failing iteration (including a
    451 for this job) is logged, counted and written to the incident stream; the loop continues.
    A Binance ban stops the whole process."""
    if initial_delay_s:
        await _wait(stop, initial_delay_s)
    while not stop.is_set():
        try:
            await job()
            health.ok[name] = health.ok.get(name, 0) + 1
        except BannedError:
            stop.set()
            raise
        except Exception as e:  # task boundary: record, keep collecting other data
            message = health.fail(name, e)
            log.exception("collector.job_failed", job=name)
            if sinks is not None:
                sinks.write(f"collector.incidents.{health.name}", {"job": name, "error": message})
        await _wait(stop, every_s - (time.time() % every_s) if every_s <= 60 else every_s)


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=max(0.0, seconds))
    except TimeoutError:
        return


def _make_sinks(settings: Settings) -> Sinks:
    repo = Path(__file__).resolve().parents[3]
    return Sinks(
        raw_root=settings.data_root / "raw",
        collector_version=version("quant-research"),
        git_sha=git_sha(repo),
        clock=ClockReference(),
    )


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
            up_mkts = await upbit.top_krw_markets(f_up, settings.upbit_top_n)
            state = upbit.TradeCursorState.load(settings.data_root / "state" / "upbit_trades.json")
            sinks.write("collector.universe", {"binance_um": sorted(fut_syms), "upbit_krw": up_mkts})

            async def trades() -> None:
                for m in up_mkts:
                    await upbit.trades_catchup(f_up, sinks, m, state, stop=stop)

            def metric_failed(source: str, symbol: str, e: Exception) -> None:
                message = health.fail(f"metrics:{source}:{symbol}", e)
                sinks.write(
                    "collector.incidents.rest",
                    {"job": "metrics", "source": source, "symbol": symbol, "error": message},
                )

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
                    )

                return run_pass

            async def backup() -> None:
                done = await asyncio.to_thread(
                    backup_closed_files, settings.data_root, settings.backup_root, now_ns=time.time_ns()
                )
                sinks.write("collector.backup", {"copied": len(done.copied), "unchanged": done.unchanged})

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
                ),
                periodic(
                    "upbit_orderbook", 1, lambda: upbit.orderbook_once(f_up, sinks, up_mkts), health, stop, sinks=sinks
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
                periodic("metrics_30d", 86_400, metrics_pass(30), health, stop, sinks=sinks, initial_delay_s=30),
                periodic(
                    "metrics_recent",
                    3_600,
                    metrics_pass(binance.PAGE_ROWS * binance.PERIOD_MS / 86_400_000),
                    health,
                    stop,
                    sinks=sinks,
                    initial_delay_s=900,
                ),
                periodic("compress", 600, compress, health, stop, sinks=sinks, initial_delay_s=120),
                periodic("backup", 3_600, backup, health, stop, sinks=sinks, initial_delay_s=1_200),
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


def _child_entry(role: str) -> None:
    """Child process target (multiprocessing spawn on Windows imports this module fresh)."""
    main([role])


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
            sys.exit(asyncio.run(_run(entry, settings, stop, health, a.duration)))
    except AlreadyRunningError as e:
        log.error("collector.already_running", error=str(e))
        sys.exit(2)


async def _run(
    entry: Callable[[Settings, asyncio.Event, Health], Awaitable[None]],
    settings: Settings,
    stop: asyncio.Event,
    health: Health,
    duration: float | None,
) -> int:
    if duration:
        asyncio.get_running_loop().call_later(duration, stop.set)
    try:
        await entry(settings, stop, health)
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
