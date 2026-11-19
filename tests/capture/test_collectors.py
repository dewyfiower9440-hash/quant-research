"""Collector I/O against httpx.MockTransport (recorded response shapes; no real network)."""

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
from quant_research.capture.http import Fetcher, Venue
from quant_research.capture.ratelimit import BannedError
from quant_research.capture.run import Health, git_sha, next_backoff, periodic
from quant_research.capture.sinks import Sinks
from quant_research.errors import ExternalServiceError

Handler = Callable[[httpx.Request], httpx.Response]


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)


class NoJitter(random.Random):
    """Deterministic jitter for tests."""

    def uniform(self, a: float, b: float) -> float:
        return 0.0


def _fetcher(handler: Handler, venue: Venue = "binance", sleeps: Sleeps | None = None) -> Fetcher:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Fetcher(client, venue, sleep=sleeps or Sleeps(), now_s=lambda: 1_000.0, rng=NoJitter())


def _sinks(tmp_path: Path) -> Sinks:
    clock = ClockReference()
    clock.update([(-16.0, 42)], measured_ns=1)
    return Sinks(tmp_path, "0.1.0", "sha", clock, now_ns=lambda: 2, fsync=lambda _fd: None)


def _records(tmp_path: Path, source: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in sorted((tmp_path / source).glob("*/*.jsonl")):
        out += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()[1:]]
    return out


def test_fetcher_retries_server_errors_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, json={"ok": True})

    sleeps = Sleeps()
    r = asyncio.run(_fetcher(handler, sleeps=sleeps).get("https://x/y"))
    assert r.json() == {"ok": True}
    assert calls["n"] == 3
    assert sleeps.calls == [1.0, 1.0]  # max(Retry 1.0 for 5xx, backoff 0.5/1.0)


def test_fetcher_gives_up_after_bounded_attempts() -> None:
    with pytest.raises(ExternalServiceError, match="after 4 attempts"):
        asyncio.run(_fetcher(lambda _r: httpx.Response(500)).get("https://x/y"))


def test_fetcher_ban_stops_without_retry() -> None:
    calls = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(418, headers={"Retry-After": "120"})

    with pytest.raises(BannedError):
        asyncio.run(_fetcher(handler).get("https://x/y"))
    assert calls["n"] == 1


def test_fetcher_client_errors_fail_immediately_with_context() -> None:
    with pytest.raises(ExternalServiceError, match="HTTP 400"):
        asyncio.run(_fetcher(lambda _r: httpx.Response(400, text="bad param")).get("https://x/y"))


def test_fetcher_transport_errors_are_retried() -> None:
    calls = {"n": 0}

    def handler(r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("down", request=r)
        return httpx.Response(200, json=[])

    asyncio.run(_fetcher(handler).get("https://x/y"))
    assert calls["n"] == 2


def test_fetcher_paces_binance_when_weight_is_high() -> None:
    sleeps = Sleeps()
    f = _fetcher(lambda _r: httpx.Response(200, json={}, headers={"x-mbx-used-weight-1m": "2300"}), sleeps=sleeps)
    asyncio.run(f.get("https://x/y"))
    assert sleeps.calls == [pytest.approx(60.0 - 1_000.0 % 60 + 0.5)]


def test_fetcher_paces_upbit_when_second_budget_is_exhausted() -> None:
    sleeps = Sleeps()
    f = _fetcher(
        lambda _r: httpx.Response(200, json={}, headers={"remaining-req": "group=trade; min=600; sec=0"}),
        venue="upbit",
        sleeps=sleeps,
    )
    asyncio.run(f.get("https://x/y"))
    assert len(sleeps.calls) == 1 and 0 < sleeps.calls[0] <= 1.05


def test_book_ticker_keeps_only_selected_symbols_with_event_time(tmp_path: Path) -> None:
    rows = [
        {"symbol": "BTCUSDT", "bidPrice": "1", "askPrice": "2", "time": 5},
        {"symbol": "DOGEUSDT", "bidPrice": "1", "askPrice": "2", "time": 5},
    ]
    sinks = _sinks(tmp_path)
    n = asyncio.run(binance.book_ticker_once(_fetcher(lambda _r: httpx.Response(200, json=rows)), sinks, {"BTCUSDT"}))
    sinks.close()
    assert n == 1
    recs = _records(tmp_path, "binance.um.book_ticker_1s")
    assert recs[0]["payload"] == [rows[0]]
    assert recs[0]["offset_ms"] == -16.0


def test_top_symbols_ranks_usdt_perpetuals_by_quote_volume() -> None:
    rows = [
        {"symbol": "ETHUSDT", "quoteVolume": "5"},
        {"symbol": "BTCUSDT", "quoteVolume": "9"},
        {"symbol": "BTCUSDT_261225", "quoteVolume": "99"},
        {"symbol": "ETHBTC", "quoteVolume": "50"},
    ]
    assert asyncio.run(binance.top_symbols(_fetcher(lambda _r: httpx.Response(200, json=rows)), 5)) == [
        "BTCUSDT",
        "ETHUSDT",
    ]


def test_metrics_refetch_pages_the_30_day_window_for_every_endpoint(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return httpx.Response(200, json=[{"timestamp": int(r.url.params["startTime"])}])

    sinks, sleeps = _sinks(tmp_path), Sleeps()
    now = 1_794_000_000_000
    calls = asyncio.run(
        binance.metrics_refetch(_fetcher(handler), sinks, ["BTCUSDT"], now_ms=lambda: now, sleep=sleeps)
    )
    sinks.close()
    pages = -(-(30 * 86_400_000 - binance.PERIOD_MS) // (binance.PAGE_ROWS * binance.PERIOD_MS))
    assert calls == pages * len(binance.METRIC_ENDPOINTS)
    basis = [r for r in seen if r.url.path == "/futures/data/basis"]
    assert basis and all(
        r.url.params["pair"] == "BTCUSDT" and r.url.params["contractType"] == "PERPETUAL" for r in basis
    )
    oi = [r for r in seen if r.url.path == "/futures/data/openInterestHist"]
    assert int(oi[-1].url.params["endTime"]) == now
    assert len(sleeps.calls) == calls  # paced between every request


def test_measure_clock_uses_lowest_rtt_sample(tmp_path: Path) -> None:
    ticks = iter([0, 100, 1_000, 1_010, 2_000, 2_300])  # rtts 100, 10, 300

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"serverTime": 5_000})

    sinks = _sinks(tmp_path)
    clock = ClockReference()
    asyncio.run(binance.measure_clock(_fetcher(handler), clock, sinks, now_ms=lambda: next(ticks)))
    sinks.close()
    assert clock.rtt_ms == 10
    assert clock.offset_ms == 5_000 - 1_005


def test_reference_snapshots_store_both_markets_and_adopt_runtime_weight_limit(tmp_path: Path) -> None:
    info = {"rateLimits": [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 6000}]}

    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=info if r.url.path.endswith("exchangeInfo") else [])

    f_fut, f_spot, sinks = _fetcher(handler), _fetcher(handler), _sinks(tmp_path)
    asyncio.run(binance.reference_snapshots(f_fut, f_spot, sinks))
    sinks.close()
    assert f_fut.binance_weight_limit == 6000 and f_spot.binance_weight_limit == 6000
    for src in (
        "binance.um.exchange_info",
        "binance.spot.exchange_info",
        "binance.um.ticker_24h",
        "binance.spot.ticker_24h",
    ):
        assert len(_records(tmp_path, src)) == 1


def test_minute_weight_limit_absent_returns_none() -> None:
    assert binance.minute_weight_limit({"rateLimits": []}) is None


def test_force_order_message_is_stored_raw(tmp_path: Path) -> None:
    sinks = _sinks(tmp_path)
    binance.handle_force_order(sinks, b'{"stream":"!forceOrder@arr","data":{"e":"forceOrder"}}')
    sinks.close()
    assert _records(tmp_path, "binance.um.force_order")[0]["payload"]["data"] == {"e": "forceOrder"}


def _trade(seq: int, ts: int) -> dict[str, int]:
    return {"sequential_id": seq, "timestamp": ts}


def test_upbit_trades_first_run_walks_today_and_seven_earlier_days(tmp_path: Path) -> None:
    seen: list[dict[str, str]] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(dict(r.url.params))
        return httpx.Response(200, json=[_trade(10, 1_000)])  # short page -> end of that day

    state = upbit.TradeCursorState(tmp_path / "state.json")
    sinks = _sinks(tmp_path)
    pages = asyncio.run(upbit.trades_catchup(_fetcher(handler, "upbit"), sinks, "KRW-BTC", state))
    sinks.close()
    assert pages == 8
    assert [p.get("days_ago") for p in seen] == [None, "1", "2", "3", "4", "5", "6", "7"]
    assert upbit.TradeCursorState.load(tmp_path / "state.json").last_ts_ms == {"KRW-BTC": 1_000}


def test_upbit_trades_follow_cursor_and_stop_at_known_timestamp(tmp_path: Path) -> None:
    full = [_trade(100 - i, 5_000 - i) for i in range(upbit.TRADE_PAGE)]
    second = [_trade(9, 4_000), _trade(8, 3_000)]  # reaches known=3_500
    pages_out = iter([full, second])
    seen: list[dict[str, str]] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(dict(r.url.params))
        return httpx.Response(200, json=next(pages_out))

    state = upbit.TradeCursorState(tmp_path / "s.json", {"KRW-BTC": 3_500})
    sinks = _sinks(tmp_path)
    pages = asyncio.run(upbit.trades_catchup(_fetcher(handler, "upbit"), sinks, "KRW-BTC", state))
    sinks.close()
    assert pages == 2
    assert seen[1]["cursor"] == str(full[-1]["sequential_id"])
    assert state.last_ts_ms["KRW-BTC"] == 5_000


def test_upbit_orderbook_and_markets_and_top_markets(tmp_path: Path) -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith("/market/all"):
            return httpx.Response(200, json=[{"market": "KRW-BTC"}, {"market": "KRW-ETH"}, {"market": "BTC-ETH"}])
        if r.url.path.endswith("/ticker"):
            return httpx.Response(
                200,
                json=[
                    {"market": "KRW-BTC", "acc_trade_price_24h": 9},
                    {"market": "KRW-ETH", "acc_trade_price_24h": 20},
                ],
            )
        assert r.url.params["count"] == "1"
        return httpx.Response(200, json=[{"market": "KRW-BTC", "orderbook_units": []}])

    f, sinks = _fetcher(handler, "upbit"), _sinks(tmp_path)
    assert asyncio.run(upbit.top_krw_markets(f, 1)) == ["KRW-ETH"]
    asyncio.run(upbit.orderbook_once(f, sinks, ["KRW-BTC"]))
    asyncio.run(upbit.market_snapshot(f, sinks))
    sinks.close()
    assert len(_records(tmp_path, "upbit.orderbook_l1_1s")) == 1
    assert len(_records(tmp_path, "upbit.markets")) == 1


def test_periodic_records_failures_and_keeps_running(tmp_path: Path) -> None:
    sinks = _sinks(tmp_path)
    health = Health("rest", tmp_path / "hb.json")
    stop = asyncio.Event()
    n = {"i": 0}

    async def job() -> None:
        n["i"] += 1
        if n["i"] == 1:
            raise ValueError("boom")
        if n["i"] >= 3:
            stop.set()

    asyncio.run(periodic("j", 0.01, job, health, stop, sinks=sinks))
    sinks.close()
    assert health.errors == {"j": 1} and health.ok == {"j": 2}
    assert "ValueError: boom" in health.last_error["j"]
    assert _records(tmp_path, "collector.incidents.rest")[0]["payload"]["job"] == "j"


def test_periodic_ban_stops_everything(tmp_path: Path) -> None:
    stop = asyncio.Event()

    async def job() -> None:
        raise BannedError("HTTP 418")

    with pytest.raises(BannedError):
        asyncio.run(periodic("j", 0.01, job, Health("rest", tmp_path / "hb.json"), stop))
    assert stop.is_set()


def test_health_file_is_written(tmp_path: Path) -> None:
    h = Health("ws", tmp_path / "run" / "ws.heartbeat.json")
    h.ok["x"] = 1
    h.write()
    body = json.loads(h.path.read_text(encoding="utf-8"))
    assert body["name"] == "ws" and body["ok"] == {"x": 1}


@pytest.mark.parametrize("stale", ["", '{"at_ns": 17', "[]", '{"at_ns": "x"}'])
def test_previous_beat_tolerates_an_empty_or_corrupt_heartbeat_file(tmp_path: Path, stale: str) -> None:
    # F-017: a 0-byte ws.heartbeat.json left by a crash made every restart die in previous_beat_ns (7 days).
    h = Health("ws", tmp_path / "run" / "ws.heartbeat.json")
    h.path.parent.mkdir(parents=True)
    h.path.write_text(stale, encoding="utf-8")
    assert h.previous_beat_ns() is None
    h.write()  # the next run then starts normally and overwrites the stale file
    assert h.previous_beat_ns() == json.loads(h.path.read_text(encoding="utf-8"))["at_ns"]


def test_health_write_goes_through_a_temp_file_and_leaves_none_behind(tmp_path: Path) -> None:
    h = Health("ws", tmp_path / "run" / "ws.heartbeat.json")
    h.write()
    h.write()
    assert [p.name for p in h.path.parent.iterdir()] == ["ws.heartbeat.json"]
    assert json.loads(h.path.read_text(encoding="utf-8"))["name"] == "ws"


def test_next_backoff_doubles_to_cap_and_resets_after_healthy_run() -> None:
    assert next_backoff(5.0, 10) == 10.0
    assert next_backoff(200.0, 10) == 300.0
    assert next_backoff(300.0, 601) == 5.0


def test_git_sha_reads_ref_file_and_packed_refs(tmp_path: Path) -> None:
    (tmp_path / ".git" / "refs" / "heads").mkdir(parents=True)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (tmp_path / ".git" / "refs" / "heads" / "main").write_text("abc\n", encoding="utf-8")
    assert git_sha(tmp_path) == "abc"
    (tmp_path / ".git" / "refs" / "heads" / "main").unlink()
    (tmp_path / ".git" / "packed-refs").write_text("# pack\ndef refs/heads/main\n", encoding="utf-8")
    assert git_sha(tmp_path) == "def"
    (tmp_path / ".git" / "HEAD").write_text("0123456789\n", encoding="utf-8")
    assert git_sha(tmp_path) == "0123456789"
