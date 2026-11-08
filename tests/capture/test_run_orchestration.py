"""Process orchestration: supervisor restart/retire with real child processes, and the rest/ws loops
end-to-end against injected network fakes (no real exchange traffic)."""

import asyncio
import json
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx

from quant_research.capture.run import EXIT_STOP_NO_RESTART, Health, rest_main, supervise, ws_main
from quant_research.config import Settings


def crash_child(_role: str) -> None:
    sys.exit(1)


def banned_child(_role: str) -> None:
    sys.exit(EXIT_STOP_NO_RESTART)


def dispatch_child(role: str) -> None:
    (crash_child if role == "crash" else banned_child)(role)


def test_supervisor_restarts_crashing_child_and_retires_banned_child() -> None:
    restarts = supervise(Settings(), ["crash", "banned"], max_runtime_s=6.0, target=dispatch_child, poll_s=0.5)
    assert restarts["crash"] >= 1
    assert restarts["banned"] == 0


def _records(root: Path, source: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in sorted((root / "raw" / source).glob("*/*.jsonl")):
        out += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()[1:]]
    return out


def _exchange(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/api/v3/time"):
        return httpx.Response(200, json={"serverTime": 1})
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


def test_rest_main_collects_t0_sources_until_stopped(tmp_path: Path) -> None:
    settings = Settings(data_root=tmp_path, binance_top_n=5, upbit_top_n=5)
    health = Health("rest", tmp_path / "run" / "rest.heartbeat.json")

    async def go() -> None:
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(2.5, stop.set)
        await rest_main(settings, stop, health, transport=httpx.MockTransport(_exchange))

    asyncio.run(go())
    assert _records(tmp_path, "binance.um.book_ticker_1s")
    assert _records(tmp_path, "upbit.orderbook_l1_1s")
    assert _records(tmp_path, "binance.server_time")
    assert _records(tmp_path, "binance.um.exchange_info")
    universe = _records(tmp_path, "collector.universe")[0]["payload"]
    assert universe == {"binance_um": ["BTCUSDT"], "upbit_krw": ["KRW-BTC"]}
    assert health.ok.get("book_ticker", 0) >= 1 and not health.errors


class FakeWs:
    def __init__(self, messages: list[str], stop: asyncio.Event) -> None:
        self.messages, self.stop = messages, stop

    async def recv(self) -> str:
        if self.messages:
            return self.messages.pop(0)
        self.stop.set()
        raise TimeoutError


def test_ws_main_stores_messages_and_reconnects_after_errors(tmp_path: Path) -> None:
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
            yield FakeWs(['{"data":{"e":"forceOrder","o":{"s":"BTCUSDT"}}}'], stop)

        await ws_main(settings, stop, health, connect=connect)

    asyncio.run(go())
    assert attempts["n"] == 2
    assert health.errors["force_order"] == 1
    assert _records(tmp_path, "binance.um.force_order")[0]["payload"]["data"]["e"] == "forceOrder"
    assert _records(tmp_path, "collector.incidents")[0]["payload"]["job"] == "force_order"


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
            ws = ProbeWs(stop, health.path)
            probe.append(ws)
            yield ws

        await ws_main(settings, stop, health, connect=connect, heartbeat_s=0.0)

    asyncio.run(go())
    assert probe[0].heartbeat_seen_while_busy
