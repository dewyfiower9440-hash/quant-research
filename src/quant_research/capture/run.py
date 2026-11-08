"""Collector processes and their supervisor (design v2.1 §3.1, §12).

    python -m quant_research.capture.run rest        # REST pollers (Binance + Upbit)
    python -m quant_research.capture.run ws          # WebSocket streams (Binance forceOrder)
    python -m quant_research.capture.run supervisor  # starts both, restarts with backoff

Each process holds a single-instance lock and writes a heartbeat file. HTTP 418/451 stop the process
with exit code 3 and the supervisor does not restart it (a ban must not be hammered).
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
from typing import Any

import httpx
import structlog
import websockets

from quant_research.capture import binance, upbit
from quant_research.capture.clockref import ClockReference
from quant_research.capture.http import Fetcher
from quant_research.capture.lock import AlreadyRunningError, InstanceLock
from quant_research.capture.ratelimit import BannedError, GeoBlockedError
from quant_research.capture.sinks import Sinks
from quant_research.config import Settings, load_settings
from quant_research.log import configure_logging

EXIT_STOP_NO_RESTART = 3
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
    """Run `job` every `every_s` seconds (wall-clock aligned). A failing iteration is logged, counted and
    written to the incident stream; the loop continues. Bans/geo-blocks stop the whole process."""
    if initial_delay_s:
        await _wait(stop, initial_delay_s)
    while not stop.is_set():
        try:
            await job()
            health.ok[name] = health.ok.get(name, 0) + 1
        except (BannedError, GeoBlockedError):
            stop.set()
            raise
        except Exception as e:  # task boundary: record, keep collecting other data
            health.errors[name] = health.errors.get(name, 0) + 1
            health.last_error[name] = f"{type(e).__name__}: {e}"[:500]
            log.exception("collector.job_failed", job=name)
            if sinks is not None:
                sinks.write("collector.incidents", {"job": name, "error": health.last_error[name]})
        await _wait(stop, every_s - (time.time() % every_s) if every_s <= 60 else every_s)


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=max(0.0, seconds))
    except TimeoutError:
        return


def _sinks(settings: Settings) -> Sinks:
    repo = Path(__file__).resolve().parents[3]
    return Sinks(
        raw_root=settings.data_root / "raw",
        collector_version=version("quant-research"),
        git_sha=git_sha(repo),
        clock=ClockReference(),
    )


async def rest_main(
    settings: Settings,
    stop: asyncio.Event,
    health: Health,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    sinks = _sinks(settings)
    timeout = httpx.Timeout(10.0)
    async with (
        httpx.AsyncClient(timeout=timeout, transport=transport) as cb,
        httpx.AsyncClient(timeout=timeout, transport=transport) as cu,
    ):
        f_fut, f_spot = Fetcher(cb, "binance"), Fetcher(cb, "binance")
        f_up = Fetcher(cu, "upbit")
        await binance.measure_clock(f_spot, sinks.clock, sinks)
        fut_syms = set(await binance.top_symbols(f_fut, settings.binance_top_n))
        up_mkts = await upbit.top_krw_markets(f_up, settings.upbit_top_n)
        state = upbit.TradeCursorState.load(settings.data_root / "state" / "upbit_trades.json")
        sinks.write("collector.universe", {"binance_um": sorted(fut_syms), "upbit_krw": up_mkts})

        async def trades() -> None:
            for m in up_mkts:
                await upbit.trades_catchup(f_up, sinks, m, state)

        async def metrics() -> None:
            await binance.metrics_refetch(f_fut, sinks, sorted(fut_syms), now_ms=time.time_ns() // 1_000_000)

        jobs = [
            periodic("clock", 60, lambda: binance.measure_clock(f_spot, sinks.clock, sinks), health, stop, sinks=sinks),
            periodic(
                "book_ticker", 1, lambda: binance.book_ticker_once(f_fut, sinks, fut_syms), health, stop, sinks=sinks
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
            periodic("upbit_markets", 86_400, lambda: upbit.market_snapshot(f_up, sinks), health, stop, sinks=sinks),
            periodic("upbit_trades", 1_800, trades, health, stop, sinks=sinks, initial_delay_s=5),
            periodic("metrics_30d", 21_600, metrics, health, stop, sinks=sinks, initial_delay_s=30),
            periodic("heartbeat", 10, _heartbeat(health, sinks), health, stop),
        ]
        try:
            await asyncio.gather(*jobs)
        finally:
            sinks.close()


def _heartbeat(health: Health, sinks: Sinks) -> Callable[[], Awaitable[None]]:
    async def beat() -> None:
        health.last_error.update({f"gap:{g.source}": f"{g.start_ns}-{g.end_ns}" for g in sinks.gaps})
        health.write()

    return beat


WsConnect = Callable[..., AbstractAsyncContextManager[Any]]


async def ws_main(
    settings: Settings, stop: asyncio.Event, health: Health, *, connect: WsConnect = websockets.connect
) -> None:
    sinks = _sinks(settings)
    backoff = 1.0
    try:
        while not stop.is_set():
            opened = time.monotonic()
            try:
                async with connect(binance.FORCE_ORDER_URL, open_timeout=10, max_size=2**22) as ws:
                    backoff = 1.0
                    while not stop.is_set() and time.monotonic() - opened < 23 * 3600:  # renew before 24 h cutoff
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=10)
                        except TimeoutError:
                            health.write()
                            continue
                        binance.handle_force_order(sinks, raw)
                        health.ok["force_order"] = health.ok.get("force_order", 0) + 1
            except (OSError, websockets.WebSocketException) as e:
                health.errors["force_order"] = health.errors.get("force_order", 0) + 1
                health.last_error["force_order"] = f"{type(e).__name__}: {e}"[:500]
                sinks.write("collector.incidents", {"job": "force_order", "error": health.last_error["force_order"]})
                await _wait(stop, backoff)
                backoff = min(300.0, backoff * 2)
    finally:
        sinks.close()


def next_backoff(previous_s: float, healthy_for_s: float) -> float:
    """Restart delay: doubles up to 5 min; resets after 10 min of healthy running."""
    return 5.0 if healthy_for_s >= 600 else min(300.0, max(5.0, previous_s * 2))


def _child_entry(role: str) -> None:
    """Child process target (multiprocessing spawn on Windows imports this module fresh)."""
    main([role])


def supervise(
    settings: Settings,
    children: list[str],
    *,
    max_runtime_s: float | None = None,
    target: Callable[[str], None] = _child_entry,
    poll_s: float = 5.0,
) -> dict[str, int]:
    """Start children, restart crashed ones with backoff, retire those that exit with code 3.
    Returns restart counts per child."""
    ctx = multiprocessing.get_context("spawn")
    procs: dict[str, multiprocessing.process.BaseProcess] = {}
    started: dict[str, float] = {}
    delay: dict[str, float] = dict.fromkeys(children, 5.0)
    restarts: dict[str, int] = dict.fromkeys(children, 0)
    retired: set[str] = set()
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
        while t_end is None or time.monotonic() < t_end:
            time.sleep(poll_s)
            for name, p in list(procs.items()):
                code = p.exitcode
                if code is None or name in retired:
                    continue
                ran = time.monotonic() - started[name]
                log.warning("supervisor.child_exited", child=name, code=code, ran_s=round(ran, 1))
                if code == EXIT_STOP_NO_RESTART:
                    retired.add(name)
                    log.error("supervisor.child_retired", child=name, reason="ban/geo-block; manual action needed")
                    continue
                delay[name] = next_backoff(delay[name], ran)
                time.sleep(min(delay[name], poll_s))
                restarts[name] += 1
                spawn(name)
    finally:
        for p in procs.values():
            if p.is_alive():
                p.terminate()
        for p in procs.values():
            p.join(timeout=20)
            if p.is_alive():
                p.kill()
    return restarts


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="quant_research.capture.run")
    ap.add_argument("role", choices=["rest", "ws", "supervisor"])
    ap.add_argument("--duration", type=float, default=None, help="stop after N seconds (tests, timed runs)")
    a = ap.parse_args(argv)
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
    except (BannedError, GeoBlockedError) as e:
        health.last_error["fatal"] = str(e)
        health.write()
        log.error("collector.stopped", error=str(e))
        return EXIT_STOP_NO_RESTART
    health.write()
    return 0


if __name__ == "__main__":
    main()
