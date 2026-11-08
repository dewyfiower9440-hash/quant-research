"""Process orchestration: supervisor restart/retire/stop-all and graceful stop with real child processes,
and the rest/ws loops end-to-end against injected network fakes (no real exchange traffic)."""

import asyncio
import json
import sys
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from quant_research.capture.ratelimit import BannedError
from quant_research.capture.run import (
    EXIT_RETIRE,
    EXIT_STOP_ALL,
    STOP_FILE,
    Health,
    rest_main,
    supervise,
    ws_main,
)
from quant_research.config import Settings


def crash_child(_role: str) -> None:
    sys.exit(1)


def retire_child(_role: str) -> None:
    sys.exit(EXIT_RETIRE)


def ban_child(_role: str) -> None:
    sys.exit(EXIT_STOP_ALL)


def patient_child(role: str) -> None:
    """Waits for the STOP file like a real collector, then exits cleanly (records that it did)."""
    root = Path(role.split("|", 1)[1])
    while not (root / "run" / STOP_FILE).exists():
        time.sleep(0.1)
    (root / "clean_exit").write_text("ok", encoding="utf-8")


def dispatch_child(role: str) -> None:
    kind = role.split("|", 1)[0]
    {"crash": crash_child, "retire": retire_child, "ban": ban_child, "patient": patient_child}[kind](role)


def test_supervisor_restarts_crashing_child_with_backoff_and_retires_blocked_child(tmp_path: Path) -> None:
    started = time.monotonic()
    result = supervise(
        Settings(data_root=tmp_path),
        ["crash", "retire"],
        max_runtime_s=16.0,
        target=dispatch_child,
        poll_s=0.5,
        grace_s=5.0,
    )
    assert result.restarts["retire"] == 0 and result.retired == {"retire"}
    # Backoff after the first crash is 10 s, after the second 20 s: exactly one restart fits in 16 s.
    # (The reviewed bug slept min(delay, poll_s) and would restart every ~0.5-2 s: many restarts.)
    assert result.restarts["crash"] == 1
    assert time.monotonic() - started < 30


def test_supervisor_stops_everything_when_a_child_reports_a_ban(tmp_path: Path) -> None:
    result = supervise(
        Settings(data_root=tmp_path),
        ["ban", f"patient|{tmp_path}"],
        max_runtime_s=30.0,
        target=dispatch_child,
        poll_s=0.3,
    )
    assert result.stopped_all
    assert (tmp_path / "clean_exit").read_text(encoding="utf-8") == "ok"  # stopped via STOP file, not killed


def _records(root: Path, source: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in sorted((root / "raw" / source).glob("*/*.jsonl")):
        out += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()[1:]]
    return out


def _gaps(root: Path, source: str) -> list[dict[str, Any]]:
    path = root / "raw" / "_gaps" / f"{source}.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def _exchange(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/api/v3/time"):
        return httpx.Response(200, json={"serverTime": time.time_ns() // 1_000_000})
    if path.endswith("/fapi/v1/ticker/24hr"):
        return httpx.Response(200, json=[{"symbol": "BTCUSDT", "quoteVolume": "9"}])
    if path.endswith("/fapi/v1/ticker/bookTicker"):
        return httpx.Response(200, json=[{"symbol": "BTCUSDT", "bidPrice": "1", "askPrice": "2", "time": 3}])
    if path.endswith("exchangeInfo"):
        return httpx.Response(200, json={"rateLimits": []})
    if path.endswith("/market/all"):
        return httpx.Response(200, json=[{"market": "KRW-BTC"}])
    if path.endswith("/v1/ticker"):
        return httpx.Response(200, json=[{"market": "KRW-BTC", "acc_trade_price_24h": 1}])
    if path.endswith("/v1/orderbook"):
        return httpx.Response(200, json=[{"market": "KRW-BTC", "orderbook_units": []}])
    return httpx.Response(200, json=[])


def test_rest_main_collects_t0_sources_and_records_its_restart_gap(tmp_path: Path) -> None:
    settings = Settings(data_root=tmp_path, binance_top_n=5, upbit_top_n=5)
    health = Health("rest", tmp_path / "run" / "rest.heartbeat.json")
    health.path.parent.mkdir(parents=True)
    health.path.write_text(json.dumps({"at_ns": 123}), encoding="utf-8")  # previous run's last heartbeat

    async def go() -> None:
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(2.5, stop.set)
        await rest_main(settings, stop, health, transport=httpx.MockTransport(_exchange))

    asyncio.run(go())
    assert _records(tmp_path, "upbit.orderbook_l1_1s")
    assert _records(tmp_path, "binance.um.exchange_info")
    first_book = _records(tmp_path, "binance.um.book_ticker_1s")[0]
    assert first_book["offset_ms"] is not None and first_book["clock_suspect"] is False
    assert _records(tmp_path, "collector.universe")[0]["payload"] == {
        "binance_um": ["BTCUSDT"],
        "upbit_krw": ["KRW-BTC"],
    }
    gap = _gaps(tmp_path, "process.rest")[0]
    assert gap["start_ns"] == 123 and gap["cause"] == "restart"
    assert health.ok.get("book_ticker", 0) >= 1 and not health.errors


def test_rest_main_stops_when_the_stop_file_appears(tmp_path: Path) -> None:
    settings = Settings(data_root=tmp_path, binance_top_n=5, upbit_top_n=5)
    health = Health("rest", tmp_path / "run" / "rest.heartbeat.json")

    async def go() -> float:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop.call_later(1.0, lambda: (tmp_path / "run" / STOP_FILE).write_text("stop", encoding="utf-8"))
        loop.call_later(20.0, stop.set)  # safety net
        t = time.monotonic()
        await rest_main(settings, stop, health, transport=httpx.MockTransport(_exchange))
        return time.monotonic() - t

    assert asyncio.run(go()) < 10


class FakeWs:
    def __init__(self, messages: list[str], stop: asyncio.Event) -> None:
        self.messages, self.stop = messages, stop

    async def recv(self) -> str:
        if self.messages:
            return self.messages.pop(0)
        self.stop.set()
        raise TimeoutError


def test_ws_main_measures_its_own_clock_reconnects_and_records_the_gap(tmp_path: Path) -> None:
    """Review #1 (own clock: offsets not null) and #4 (reconnect span is a gap)."""
    settings = Settings(data_root=tmp_path)
    health = Health("ws", tmp_path / "run" / "ws.heartbeat.json")
    attempts = {"n": 0}

    async def go() -> None:
        stop = asyncio.Event()

        @asynccontextmanager
        async def connect(_url: str, **_kw: object) -> AsyncGenerator[FakeWs]:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError("network down")
            await asyncio.sleep(0.2)  # let the clock job measure first
            yield FakeWs(['{"data":{"e":"forceOrder","o":{"s":"BTCUSDT"}}}'], stop)

        await ws_main(settings, stop, health, connect=connect, transport=httpx.MockTransport(_exchange))

    asyncio.run(go())
    assert attempts["n"] == 2
    assert health.errors["force_order"] == 1
    rec = _records(tmp_path, "binance.um.force_order")[0]
    assert rec["payload"]["data"]["e"] == "forceOrder"
    assert rec["offset_ms"] is not None and rec["clock_suspect"] is False
    assert _records(tmp_path, "collector.incidents.ws")[0]["payload"]["job"] == "force_order"
    reconnect = _gaps(tmp_path, "binance.um.force_order")
    assert reconnect[0]["cause"] == "reconnect" and reconnect[0]["start_ns"] < reconnect[0]["end_ns"]


def test_ws_main_handshake_418_is_a_ban(tmp_path: Path) -> None:
    """Review #2: a ban on the WS handshake must stop collection, not reconnect forever."""
    settings = Settings(data_root=tmp_path)
    health = Health("ws", tmp_path / "run" / "ws.heartbeat.json")

    async def go() -> None:
        stop = asyncio.Event()

        @asynccontextmanager
        async def connect(_url: str, **_kw: object) -> AsyncGenerator[FakeWs]:
            raise InvalidStatus(Response(418, "I'm a teapot", Headers()))
            yield FakeWs([], stop)  # unreachable; makes this an async generator

        await ws_main(settings, stop, health, connect=connect, transport=httpx.MockTransport(_exchange))

    with pytest.raises(BannedError):
        asyncio.run(go())


class ProbeWs:
    """Serves messages continuously and records whether a heartbeat existed before the 3rd one."""

    def __init__(self, stop: asyncio.Event, heartbeat: Path) -> None:
        self.stop, self.heartbeat, self.served = stop, heartbeat, 0
        self.heartbeat_seen_while_busy = False

    async def recv(self) -> str:
        self.served += 1
        if self.served == 3:
            self.heartbeat_seen_while_busy = self.heartbeat.exists()
            self.stop.set()
        return '{"data":{"e":"forceOrder"}}'


def test_ws_main_writes_heartbeat_while_messages_keep_arriving(tmp_path: Path) -> None:
    settings = Settings(data_root=tmp_path)
    health = Health("ws", tmp_path / "run" / "ws.heartbeat.json")
    probe: list[ProbeWs] = []

    async def go() -> None:
        stop = asyncio.Event()

        @asynccontextmanager
        async def connect(_url: str, **_kw: object) -> AsyncGenerator[ProbeWs]:
            health.path.unlink(missing_ok=True)  # ignore the start-up heartbeat
            ws = ProbeWs(stop, health.path)
            probe.append(ws)
            yield ws

        await ws_main(
            settings, stop, health, connect=connect, heartbeat_s=0.0, transport=httpx.MockTransport(_exchange)
        )

    asyncio.run(go())
    assert probe[0].heartbeat_seen_while_busy


def test_supervisor_itself_shuts_down_when_the_stop_file_appears(tmp_path: Path) -> None:
    """Operator stop: children exit cleanly and must not be restarted into a stop loop."""
    import threading

    def drop_stop_file() -> None:
        time.sleep(2.0)
        (tmp_path / "run").mkdir(parents=True, exist_ok=True)
        (tmp_path / "run" / STOP_FILE).write_text("stop", encoding="utf-8")

    threading.Thread(target=drop_stop_file, daemon=True).start()
    started = time.monotonic()
    result = supervise(
        Settings(data_root=tmp_path), [f"patient|{tmp_path}"], max_runtime_s=60.0, target=dispatch_child, poll_s=0.3
    )
    assert time.monotonic() - started < 20
    assert result.restarts[f"patient|{tmp_path}"] == 0
    assert (tmp_path / "clean_exit").read_text(encoding="utf-8") == "ok"


def test_redirect_log_writes_per_role_file_and_exports_dir_for_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quant_research.capture.run import LOG_DIR_ENV, redirect_log

    monkeypatch.delenv(LOG_DIR_ENV, raising=False)
    original = sys.stderr
    try:
        assert redirect_log(None, "rest") is None  # nothing configured: console logging stays
        path = redirect_log(tmp_path / "logs", "supervisor")
        assert path == tmp_path / "logs" / "supervisor.log"
        print("hello", file=sys.stderr)
        child = redirect_log(None, "ws")  # a spawned child sees only the environment
        assert child == tmp_path / "logs" / "ws.log"
    finally:
        for stream in (sys.stderr,):
            if stream is not original:
                stream.close()
        sys.stderr = original
    assert (tmp_path / "logs" / "supervisor.log").read_text(encoding="utf-8") == "hello\n"
