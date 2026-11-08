"""Regression tests for the 2026-11-08 independent review of the capture package (docs/failures.md F-005)."""

import asyncio
import json
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from quant_research.capture import binance, upbit
from quant_research.capture.clockref import ClockReference
from quant_research.capture.http import Fetcher, VenueGate
from quant_research.capture.ratelimit import BannedError
from quant_research.capture.sinks import Sinks
from quant_research.errors import ExternalServiceError


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)


class NoJitter(random.Random):
    """Deterministic jitter for tests."""

    def uniform(self, a: float, b: float) -> float:
        return 0.0


class Clock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t


def _sinks(tmp_path: Path, clock: ClockReference | None = None) -> Sinks:
    return Sinks(tmp_path, "0.1.0", "sha", clock or ClockReference(), now_ns=lambda: 2, fsync=lambda _fd: None)


def _records(root: Path, source: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in sorted((root / source).glob("*/*.jsonl")):
        out += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()[1:]]
    return out


def test_offset_is_kept_even_when_the_clock_is_suspect(tmp_path: Path) -> None:
    """Review #10: a large (suspect) offset is exactly when the correction is needed."""
    clock = ClockReference()
    clock.update([(400.0, 40)], measured_ns=1)
    sinks = _sinks(tmp_path, clock)
    sinks.write("s", {"x": 1})
    sinks.close()
    rec = _records(tmp_path, "s")[0]
    assert rec["offset_ms"] == 400.0 and rec["clock_suspect"] is True


def test_unmeasured_clock_is_flagged_not_hidden(tmp_path: Path) -> None:
    sinks = _sinks(tmp_path)
    sinks.write("s", 1)
    sinks.close()
    rec = _records(tmp_path, "s")[0]
    assert rec["offset_ms"] is None and rec["clock_suspect"] is True


def test_rate_limit_wait_seen_by_one_fetcher_blocks_the_other_on_the_same_venue() -> None:
    """Review #7: limits are per IP; the 1 Hz poller must not keep hitting after a 429."""
    gate, clock, sleeps = VenueGate(), Clock(), Sleeps()
    responses = iter([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={})])
    a = Fetcher(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: next(responses))),
        "binance",
        sleep=sleeps,
        now_s=clock,
        rng=NoJitter(),
        gate=gate,
    )
    b = Fetcher(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={}))),
        "binance",
        sleep=sleeps,
        now_s=clock,
        rng=NoJitter(),
        gate=gate,
    )
    asyncio.run(a.get("https://x/a"))
    assert gate.blocked_until == pytest.approx(1_007.0)
    sleeps.calls.clear()
    asyncio.run(b.get("https://x/b"))
    assert sleeps.calls == [pytest.approx(7.0)]  # b waited for the shared block


def test_metrics_refetch_isolates_a_failing_symbol_and_recomputes_time_per_symbol(tmp_path: Path) -> None:
    """Review #8: one bad symbol must not skip the others for 6 h; the window follows the clock."""
    now = {"ms": 1_794_000_000_000}
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        if r.url.params.get("symbol") == "DEADUSDT" or r.url.params.get("pair") == "DEADUSDT":
            return httpx.Response(400, text='{"code":-1121,"msg":"Invalid symbol."}')
        return httpx.Response(200, json=[])

    def tick() -> int:
        now["ms"] += 60_000
        return now["ms"]

    errors: list[tuple[str, str]] = []
    f = Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handler)), "binance", sleep=Sleeps(), rng=NoJitter())
    sinks = _sinks(tmp_path)
    asyncio.run(
        binance.metrics_refetch(
            f,
            sinks,
            ["DEADUSDT", "BTCUSDT"],
            now_ms=tick,
            sleep=Sleeps(),
            on_error=lambda src, sym, _e: errors.append((src, sym)),
        )
    )
    sinks.close()
    assert len(errors) == len(binance.METRIC_ENDPOINTS)
    assert all(sym == "DEADUSDT" for _src, sym in errors)
    btc_ends = {int(r.url.params["endTime"]) for r in seen if "BTCUSDT" in str(r.url)}
    assert max(btc_ends) > 1_794_000_000_000 + 60_000  # later symbols use a later "now"


def test_metrics_refetch_stops_promptly_and_bans_propagate(tmp_path: Path) -> None:
    stop = asyncio.Event()
    calls = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        stop.set()
        return httpx.Response(200, json=[])

    f = Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handler)), "binance", sleep=Sleeps(), rng=NoJitter())
    sinks = _sinks(tmp_path)
    asyncio.run(
        binance.metrics_refetch(f, sinks, ["BTCUSDT"], now_ms=lambda: 1_794_000_000_000, sleep=Sleeps(), stop=stop)
    )
    assert calls["n"] == 1
    banned = Fetcher(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(418))),
        "binance",
        sleep=Sleeps(),
        rng=NoJitter(),
    )
    with pytest.raises(BannedError):
        asyncio.run(
            binance.metrics_refetch(
                banned, sinks, ["BTCUSDT"], now_ms=lambda: 1_794_000_000_000, sleep=Sleeps(), on_error=_ignore_error
            )
        )
    sinks.close()


def _ignore_error(_source: str, _symbol: str, _error: Exception) -> None:
    """on_error sink for tests that only check that bans still propagate."""


def _upbit(handler: Callable[[httpx.Request], httpx.Response]) -> Fetcher:
    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handler)), "upbit", sleep=Sleeps(), rng=NoJitter())


def _trade(seq: int, ts: int) -> dict[str, int]:
    return {"sequential_id": seq, "timestamp": ts}


def test_upbit_cursor_that_does_not_advance_is_an_error_not_an_infinite_loop(tmp_path: Path) -> None:
    """Review #9: a repeated page must not loop forever."""
    page = [_trade(100 - i, 9_000 - i) for i in range(upbit.TRADE_PAGE)]
    state = upbit.TradeCursorState(tmp_path / "s.json")
    with pytest.raises(ExternalServiceError, match="did not advance"):
        asyncio.run(
            upbit.trades_catchup(_upbit(lambda _r: httpx.Response(200, json=page)), _sinks(tmp_path), "KRW-BTC", state)
        )
    assert state.last_ts_ms == {}  # incomplete pass: cursor not advanced


def test_upbit_interrupted_pass_keeps_the_old_cursor(tmp_path: Path) -> None:
    stop = asyncio.Event()

    def handler(_r: httpx.Request) -> httpx.Response:
        stop.set()
        return httpx.Response(200, json=[_trade(10 + i, 9_000 + i) for i in range(upbit.TRADE_PAGE)])

    state = upbit.TradeCursorState(tmp_path / "s.json", {"KRW-BTC": 1_000})
    asyncio.run(upbit.trades_catchup(_upbit(handler), _sinks(tmp_path), "KRW-BTC", state, stop=stop))
    assert state.last_ts_ms == {"KRW-BTC": 1_000}


def test_upbit_outage_longer_than_the_api_window_is_recorded_as_a_gap(tmp_path: Path) -> None:
    """Review #9: after > 7 days down, the missed span must be explicit, not silent."""
    state = upbit.TradeCursorState(tmp_path / "s.json", {"KRW-BTC": 1_000})  # far older than anything returned
    sinks = _sinks(tmp_path)
    asyncio.run(
        upbit.trades_catchup(_upbit(lambda _r: httpx.Response(200, json=[_trade(5, 50_000)])), sinks, "KRW-BTC", state)
    )
    gaps = [json.loads(x) for x in (tmp_path / "_gaps" / "upbit.trades.jsonl").read_text(encoding="utf-8").splitlines()]
    assert gaps == [
        {"source": "upbit.trades", "start_ns": 1_000, "end_ns": 50_000, "cause": "api_window_exceeded:KRW-BTC"}
    ]
    assert state.last_ts_ms["KRW-BTC"] == 50_000
