"""v2.5 step 1 (N01): a collector that is alive but failing recovers by itself.

Two outages of the first 16 days were of this kind: an Upbit market list fixed at start (one delisted code made
every batched order-book request a 404 for 74 hours) and a process whose imports broke under it (every request
failed for 53 hours while the heartbeat kept beating)."""

import asyncio
import json
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from quant_research.capture import run as run_module
from quant_research.capture import upbit
from quant_research.capture.clockref import ClockReference
from quant_research.capture.http import Fetcher, HttpStatusError
from quant_research.capture.run import (
    EXIT_STALE,
    Health,
    StaleJobError,
    StaleRestarts,
    periodic,
    rest_main,
    run_role,
    throttled,
)
from quant_research.capture.sinks import Sinks
from quant_research.config import Settings
from quant_research.errors import ExternalServiceError

Handler = Callable[[httpx.Request], httpx.Response]


async def no_sleep(_s: float) -> None:
    return None


class NoJitter(random.Random):
    def uniform(self, a: float, b: float) -> float:
        return 0.0


def fetcher(handler: Handler) -> Fetcher:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Fetcher(client, "upbit", sleep=no_sleep, now_s=lambda: 1_000.0, rng=NoJitter())


def make_sinks(tmp_path: Path) -> Sinks:
    clock = ClockReference()
    clock.update([(-16.0, 42)], measured_ns=1)
    return Sinks(tmp_path, "0.1.0", "sha", clock, now_ns=lambda: 2, fsync=lambda _fd: None)


def records(tmp_path: Path, source: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in sorted((tmp_path / source).glob("*/*.jsonl")):
        out += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()[1:]]
    return out


def orderbook_handler(known: set[str]) -> tuple[list[str], Handler]:
    asked: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        markets = r.url.params["markets"]
        asked.append(markets)
        if any(m not in known for m in markets.split(",")):
            return httpx.Response(404, json={"error": {"name": 404, "message": "Code not found"}})
        return httpx.Response(200, json=[{"market": m, "orderbook_units": []} for m in markets.split(",")])

    return asked, handler


def test_orderbook_refreshes_the_market_list_when_a_code_is_unknown(tmp_path: Path) -> None:
    asked, handler = orderbook_handler({"KRW-BTC", "KRW-ETH", "KRW-NEW"})
    f, sinks = fetcher(handler), make_sinks(tmp_path)
    markets = ["KRW-BTC", "KRW-GONE", "KRW-ETH"]
    refreshed: list[int] = []

    async def refresh() -> list[str]:
        refreshed.append(1)
        return ["KRW-BTC", "KRW-ETH", "KRW-NEW"]

    assert asyncio.run(upbit.orderbook_refreshing(f, sinks, markets, refresh)) is True
    sinks.close()
    assert markets == ["KRW-BTC", "KRW-ETH", "KRW-NEW"]  # in place: the trades job reads the same list
    assert asked == ["KRW-BTC,KRW-GONE,KRW-ETH", "KRW-BTC,KRW-ETH,KRW-NEW"] and refreshed == [1]
    assert len(records(tmp_path, "upbit.orderbook_l1_1s")) == 1
    assert records(tmp_path, "collector.universe")[0]["payload"] == {
        "upbit_krw": ["KRW-BTC", "KRW-ETH", "KRW-NEW"],
        "reason": "unknown_market",
    }


def test_orderbook_with_a_known_list_does_not_refresh(tmp_path: Path) -> None:
    asked, handler = orderbook_handler({"KRW-BTC"})
    f, sinks = fetcher(handler), make_sinks(tmp_path)

    async def refresh() -> list[str]:
        raise AssertionError("no refresh on a successful request")

    assert asyncio.run(upbit.orderbook_refreshing(f, sinks, ["KRW-BTC"], refresh)) is False
    sinks.close()
    assert asked == ["KRW-BTC"]


def test_orderbook_errors_other_than_404_are_not_a_stale_list(tmp_path: Path) -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad"})

    f, sinks = fetcher(handler), make_sinks(tmp_path)

    async def refresh() -> list[str]:
        raise AssertionError("a 400 is not an unknown market")

    with pytest.raises(HttpStatusError) as e:
        asyncio.run(upbit.orderbook_refreshing(f, sinks, ["KRW-BTC"], refresh))
    sinks.close()
    assert e.value.status == 400 and isinstance(e.value, ExternalServiceError)


def test_orderbook_still_unknown_after_the_refresh_raises_once(tmp_path: Path) -> None:
    asked, handler = orderbook_handler(set())
    f, sinks = fetcher(handler), make_sinks(tmp_path)

    async def refresh() -> list[str]:
        return ["KRW-BTC"]

    with pytest.raises(HttpStatusError):
        asyncio.run(upbit.orderbook_refreshing(f, sinks, ["KRW-GONE"], refresh))
    sinks.close()
    assert len(asked) == 2  # one retry, never a loop


class Clock:
    def __init__(self, step: float) -> None:
        self.t, self.step = 0.0, step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


def test_periodic_raises_stale_when_a_job_only_fails_for_the_whole_limit(tmp_path: Path) -> None:
    health, stop = Health("rest", tmp_path / "hb.json"), asyncio.Event()
    n = {"i": 0}

    async def job() -> None:
        n["i"] += 1
        raise ValueError("module broke under the process")

    with pytest.raises(StaleJobError) as e:
        asyncio.run(
            periodic(
                "book_ticker",
                0.001,
                job,
                health,
                stop,
                restart_after_s=300,
                restart_min_failures=1,
                monotonic=Clock(200),
            )
        )
    assert n["i"] == 3  # failing since t=200: the second failure is 200 s in, the third 400 s
    assert e.value.job == "book_ticker" and "ValueError" in str(e.value)


def test_periodic_one_success_ends_the_failing_stretch(tmp_path: Path) -> None:
    health, stop = Health("rest", tmp_path / "hb.json"), asyncio.Event()
    n = {"i": 0}

    async def job() -> None:
        n["i"] += 1
        if n["i"] >= 9:
            stop.set()
        if n["i"] % 2:
            raise ValueError("flaky")

    asyncio.run(
        periodic("j", 0.001, job, health, stop, restart_after_s=300, restart_min_failures=1, monotonic=Clock(200))
    )
    assert health.errors == {"j": 5} and health.ok == {"j": 4}


def test_periodic_without_a_limit_never_raises_stale(tmp_path: Path) -> None:
    health, stop = Health("rest", tmp_path / "hb.json"), asyncio.Event()
    n = {"i": 0}

    async def job() -> None:
        n["i"] += 1
        if n["i"] >= 5:
            stop.set()
        raise ValueError("x")

    asyncio.run(periodic("j", 0.001, job, health, stop, monotonic=Clock(1_000)))
    assert health.errors == {"j": 5}


def test_periodic_keeps_running_when_the_restart_budget_is_spent(tmp_path: Path) -> None:
    """An exchange that is really down must not restart the process (and its other sources) every five minutes."""
    health, stop = Health("rest", tmp_path / "hb.json"), asyncio.Event()
    n = {"i": 0}
    asked: list[str] = []

    async def job() -> None:
        n["i"] += 1
        if n["i"] >= 6:
            stop.set()
        raise ValueError("down")

    def allow(job_name: str) -> bool:
        asked.append(job_name)
        return False

    asyncio.run(
        periodic(
            "j",
            0.001,
            job,
            health,
            stop,
            restart_after_s=300,
            restart_min_failures=1,
            monotonic=Clock(200),
            allow_restart=allow,
        )
    )
    # failures at 200, 400, ... : asked at 600 (400 s in), refused, asked again one limit later at 1000
    assert n["i"] == 6 and asked == ["j", "j"]


def test_stale_restarts_allow_one_restart_per_job_and_window(tmp_path: Path) -> None:
    hour = 3_600 * 10**9
    now = {"ns": 100 * hour}
    budget = StaleRestarts(tmp_path / "run" / "rest.stale.json", min_gap_ns=6 * hour, now_ns=lambda: now["ns"])
    assert budget.allow("upbit_orderbook") is True
    assert budget.allow("upbit_orderbook") is False
    assert budget.allow("book_ticker") is True  # per job
    now["ns"] += 6 * hour
    again = StaleRestarts(tmp_path / "run" / "rest.stale.json", min_gap_ns=6 * hour, now_ns=lambda: now["ns"])
    assert again.allow("upbit_orderbook") is True  # the state survives the restart it caused


def test_stale_restarts_survive_an_unreadable_state_file(tmp_path: Path) -> None:
    path = tmp_path / "rest.stale.json"
    path.write_text("", encoding="utf-8")
    assert StaleRestarts(path, min_gap_ns=1, now_ns=lambda: 5).allow("j") is True
    assert json.loads(path.read_text(encoding="utf-8")) == {"j": 5}


def test_run_role_turns_a_stale_job_into_its_own_exit_code(tmp_path: Path) -> None:
    health = Health("rest", tmp_path / "rest.heartbeat.json")

    async def entry(_settings: Settings, _stop: asyncio.Event, _health: Health) -> None:
        raise StaleJobError("upbit_orderbook", 305.0, "HttpStatusError: 404")

    code = asyncio.run(run_role(entry, Settings(data_root=tmp_path), asyncio.Event(), health, None))
    assert code == EXIT_STALE and code not in (0, 3, 4)
    body = json.loads((tmp_path / "rest.heartbeat.json").read_text(encoding="utf-8"))
    assert "upbit_orderbook" in body["last_error"]["fatal"]


def test_health_records_when_each_job_last_succeeded(tmp_path: Path) -> None:
    health, stop = Health("rest", tmp_path / "hb.json"), asyncio.Event()

    async def job() -> None:
        stop.set()

    asyncio.run(periodic("j", 0.001, job, health, stop))
    health.write()
    body = json.loads((tmp_path / "hb.json").read_text(encoding="utf-8"))
    assert body["last_ok_ns"]["j"] >= health.started_ns


# ---------------------------------------------------------------- review findings (2026-11-24), each reproduced first
def test_a_venue_that_is_down_is_not_a_stale_job(tmp_path: Path) -> None:
    """A restart cannot bring a venue or the network back; it would only take the other venue's collection down."""
    health, stop = Health("rest", tmp_path / "hb.json"), asyncio.Event()
    n = {"i": 0}

    async def job() -> None:
        n["i"] += 1
        if n["i"] >= 8:
            stop.set()
        raise ExternalServiceError("GET https://api.upbit.com/v1/orderbook failed after 4 attempts")

    asyncio.run(
        periodic("j", 0.001, job, health, stop, restart_after_s=300, restart_min_failures=1, monotonic=Clock(200))
    )
    assert n["i"] == 8 and health.errors == {"j": 8}


def test_an_answer_the_venue_keeps_giving_is_a_stale_job(tmp_path: Path) -> None:
    health, stop = Health("rest", tmp_path / "hb.json"), asyncio.Event()

    async def job() -> None:
        raise HttpStatusError(404, "GET .../orderbook -> HTTP 404: Code not found")

    with pytest.raises(StaleJobError):
        asyncio.run(
            periodic("j", 0.001, job, health, stop, restart_after_s=300, restart_min_failures=1, monotonic=Clock(200))
        )


def test_two_failures_around_a_clock_jump_are_not_a_stale_job(tmp_path: Path) -> None:
    """Resume from standby: one failure before, one after, hours apart on the clock. Not five minutes of failing."""
    health, stop = Health("rest", tmp_path / "hb.json"), asyncio.Event()
    n = {"i": 0}

    async def job() -> None:
        n["i"] += 1
        if n["i"] >= 3:
            stop.set()
            return
        raise ValueError("no route to host")

    asyncio.run(periodic("j", 0.001, job, health, stop, restart_after_s=300, monotonic=Clock(3_600)))
    assert n["i"] == 3 and health.errors == {"j": 2}  # the default asks for many consecutive failures too


def test_stale_restarts_survive_a_state_file_that_is_not_an_object(tmp_path: Path) -> None:
    path = tmp_path / "rest.stale.json"
    path.write_text("[]", encoding="utf-8")
    assert StaleRestarts(path, min_gap_ns=1, now_ns=lambda: 7).allow("j") is True
    assert json.loads(path.read_text(encoding="utf-8")) == {"j": 7}


def test_stale_restarts_refuse_when_the_state_cannot_be_written(tmp_path: Path) -> None:
    """Without a record the budget cannot hold: no restart rather than one every five minutes."""
    blocked = tmp_path / "is-a-file"
    blocked.write_text("x", encoding="utf-8")
    assert StaleRestarts(blocked / "rest.stale.json", min_gap_ns=1, now_ns=lambda: 7).allow("j") is False


def test_orderbook_does_not_ask_again_when_the_refreshed_list_is_the_same(tmp_path: Path) -> None:
    asked, handler = orderbook_handler(set())
    f, sinks = fetcher(handler), make_sinks(tmp_path)
    markets = ["KRW-BTC"]

    async def refresh() -> list[str]:
        return ["KRW-BTC"]

    with pytest.raises(HttpStatusError):
        asyncio.run(upbit.orderbook_refreshing(f, sinks, markets, refresh))
    sinks.close()
    assert asked == ["KRW-BTC"] and records(tmp_path, "collector.universe") == []


def test_orderbook_keeps_its_list_when_the_refresh_returns_nothing(tmp_path: Path) -> None:
    asked, handler = orderbook_handler(set())
    f, sinks = fetcher(handler), make_sinks(tmp_path)
    markets = ["KRW-BTC"]

    async def refresh() -> list[str]:
        return []

    with pytest.raises(HttpStatusError):
        asyncio.run(upbit.orderbook_refreshing(f, sinks, markets, refresh))
    sinks.close()
    assert markets == ["KRW-BTC"] and asked == ["KRW-BTC"]


def test_orderbook_with_no_list_yet_takes_one_first(tmp_path: Path) -> None:
    """The list could not be read at start (Upbit down): the 1 Hz job fills it when the venue is back."""
    asked, handler = orderbook_handler({"KRW-BTC"})
    f, sinks = fetcher(handler), make_sinks(tmp_path)
    markets: list[str] = []

    async def refresh() -> list[str]:
        return ["KRW-BTC"]

    assert asyncio.run(upbit.orderbook_refreshing(f, sinks, markets, refresh)) is True
    sinks.close()
    assert markets == ["KRW-BTC"] and asked == ["KRW-BTC"]
    assert records(tmp_path, "collector.universe")[0]["payload"]["reason"] == "empty_list"


def test_orderbook_with_no_list_and_no_refresh_result_fails_without_a_request(tmp_path: Path) -> None:
    asked, handler = orderbook_handler(set())
    f, sinks = fetcher(handler), make_sinks(tmp_path)

    async def refresh() -> list[str]:
        return []

    with pytest.raises(ExternalServiceError, match="no Upbit markets"):
        asyncio.run(upbit.orderbook_refreshing(f, sinks, [], refresh))
    sinks.close()
    assert asked == []


def test_throttled_runs_the_call_at_most_once_per_interval() -> None:
    now = {"t": 0.0}
    calls: list[int] = []

    async def fetch() -> list[str]:
        calls.append(1)
        return ["KRW-BTC"]

    limited = throttled(fetch, every_s=60.0, monotonic=lambda: now["t"])
    assert asyncio.run(limited()) == ["KRW-BTC"]
    now["t"] = 59.0
    with pytest.raises(ExternalServiceError, match="throttled"):
        asyncio.run(limited())
    now["t"] = 60.0
    assert asyncio.run(limited()) == ["KRW-BTC"] and len(calls) == 2


def test_a_lasting_404_with_the_throttled_refresh_is_still_a_stale_job(tmp_path: Path) -> None:
    """Re-review: between two refreshes the throttle's own error replaced the 404, the stretch was reset every
    second and the 74-hour kind of outage could never restart the process (nor show its cause in the log)."""
    asked, handler = orderbook_handler(set())
    f, sinks = fetcher(handler), make_sinks(tmp_path)
    health, stop = Health("rest", tmp_path / "hb.json"), asyncio.Event()
    clock = Clock(1.0)

    async def same_list() -> list[str]:
        return ["KRW-GONE"]

    refresh = throttled(same_list, every_s=60.0, monotonic=clock)
    markets = ["KRW-GONE"]

    async def job() -> None:
        await upbit.orderbook_refreshing(f, sinks, markets, refresh)

    with pytest.raises(StaleJobError):
        asyncio.run(periodic("upbit_orderbook", 0.001, job, health, stop, restart_after_s=300, monotonic=clock))
    sinks.close()
    assert "HTTP 404" in health.last_error["upbit_orderbook"]
    assert len(asked) == health.errors["upbit_orderbook"]  # one request per failure: no retry with the same list


def exchange(upbit_up: bool, book_status: int = 200) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "upbit" in request.url.host and not upbit_up:
            return httpx.Response(400, json={"error": "maintenance"})
        if path.endswith("/api/v3/time"):
            return httpx.Response(200, json={"serverTime": time.time_ns() // 1_000_000})
        if path.endswith("/fapi/v1/ticker/24hr"):
            return httpx.Response(200, json=[{"symbol": "BTCUSDT", "quoteVolume": "9"}])
        if path.endswith("/fapi/v1/ticker/bookTicker"):
            if book_status != 200:
                return httpx.Response(book_status, json={"msg": "bad"})
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

    return handler


def test_rest_main_collects_binance_while_upbit_is_down_at_start(tmp_path: Path) -> None:
    """Review: a stale restart during an Upbit outage died at start on the market list, and Binance bookTicker
    (never fetchable again) was lost until Upbit came back."""
    settings = Settings(data_root=tmp_path, binance_top_n=5, upbit_top_n=5)
    health = Health("rest", tmp_path / "run" / "rest.heartbeat.json")

    async def go() -> None:
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(2.5, stop.set)
        await rest_main(settings, stop, health, transport=httpx.MockTransport(exchange(upbit_up=False)))

    asyncio.run(go())
    assert health.ok.get("book_ticker", 0) >= 1
    assert health.errors.get("upbit_orderbook", 0) >= 1
    assert records(tmp_path / "raw", "collector.universe")[0]["payload"]["upbit_krw"] == []


def test_rest_main_ends_with_the_stale_exit_code_and_closed_sinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_module, "STALE_AFTER_S", 0.5)
    monkeypatch.setattr(run_module, "STALE_MIN_FAILURES", 1)
    settings = Settings(data_root=tmp_path, binance_top_n=5, upbit_top_n=5)
    health = Health("rest", tmp_path / "run" / "rest.heartbeat.json")
    transport = httpx.MockTransport(exchange(upbit_up=True, book_status=400))

    async def entry(s: Settings, stop: asyncio.Event, h: Health) -> None:
        await rest_main(s, stop, h, transport=transport)

    code = asyncio.run(run_role(entry, settings, asyncio.Event(), health, 30.0))
    assert code == EXIT_STALE
    assert json.loads((tmp_path / "run" / "rest.stale.json").read_text(encoding="utf-8")).keys() == {"book_ticker"}
    assert records(tmp_path / "raw", "upbit.orderbook_l1_1s")  # the other venue was collected until the exit
    # a second process inside the window keeps running: the budget is spent
    again = Health("rest", tmp_path / "run" / "rest.heartbeat.json")
    assert asyncio.run(run_role(entry, settings, asyncio.Event(), again, 3.0)) == 0
