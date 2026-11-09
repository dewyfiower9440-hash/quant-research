"""Binance T0 collectors (design v2.1 §5.1, §5.5; ADR-003/004/005). Raw payloads only; parsing is
done later by the normalizer from the raw segments."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast

from quant_research.capture.clockref import ClockReference
from quant_research.capture.http import Fetcher
from quant_research.capture.ratelimit import BannedError
from quant_research.capture.sinks import Sinks
from quant_research.domain.probe import clock_offset_ms
from quant_research.errors import ExternalServiceError

FAPI = "https://fapi.binance.com"
SPOT = "https://api.binance.com"
FORCE_ORDER_URL = "wss://fstream.binance.com/market/stream?streams=!forceOrder@arr"

PERIOD_MS = 300_000
PAGE_ROWS = 500
UNSUPPORTED_CONTRACT = '"code":-4104'  # Invalid contract type (basis is not offered for TradFi perps)
METRIC_ENDPOINTS: dict[str, tuple[str, str]] = {
    "binance.um.oi_hist": ("/futures/data/openInterestHist", "symbol"),
    "binance.um.top_ls_account": ("/futures/data/topLongShortAccountRatio", "symbol"),
    "binance.um.top_ls_position": ("/futures/data/topLongShortPositionRatio", "symbol"),
    "binance.um.global_ls_account": ("/futures/data/globalLongShortAccountRatio", "symbol"),
    "binance.um.taker_ls": ("/futures/data/takerlongshortRatio", "symbol"),
    "binance.um.basis": ("/futures/data/basis", "pair"),
}


async def top_symbols(f: Fetcher, n: int) -> list[str]:
    """USDT perpetuals ranked by 24h quote volume (current snapshot; PIT universe is rebuilt from bulk later)."""
    rows = cast(list[dict[str, Any]], (await f.get(f"{FAPI}/fapi/v1/ticker/24hr")).json())
    perps = [r for r in rows if str(r["symbol"]).endswith("USDT") and "_" not in str(r["symbol"])]
    perps.sort(key=lambda r: float(r["quoteVolume"]), reverse=True)
    return [str(r["symbol"]) for r in perps[:n]]


def minute_weight_limit(exchange_info: dict[str, Any]) -> int | None:
    for lim in exchange_info.get("rateLimits", []):
        lim = cast(dict[str, Any], lim)
        if lim.get("rateLimitType") == "REQUEST_WEIGHT" and lim.get("interval") == "MINUTE":
            return int(lim["limit"]) // max(1, int(lim.get("intervalNum", 1)))
    return None


async def reference_snapshots(f_fut: Fetcher, f_spot: Fetcher, sinks: Sinks) -> None:
    """Daily: exchangeInfo (filters have no history; ADR in design §5.1) and 24h tickers, spot and futures."""
    fut_info = cast(dict[str, Any], (await f_fut.get(f"{FAPI}/fapi/v1/exchangeInfo")).json())
    limit = minute_weight_limit(fut_info)
    if limit:
        f_fut.binance_weight_limit = limit
    sinks.write("binance.um.exchange_info", fut_info)
    spot_info = cast(dict[str, Any], (await f_spot.get(f"{SPOT}/api/v3/exchangeInfo")).json())
    limit = minute_weight_limit(spot_info)
    if limit:
        f_spot.binance_weight_limit = limit
    sinks.write("binance.spot.exchange_info", spot_info)
    sinks.write("binance.um.ticker_24h", (await f_fut.get(f"{FAPI}/fapi/v1/ticker/24hr")).json())
    sinks.write("binance.spot.ticker_24h", (await f_spot.get(f"{SPOT}/api/v3/ticker/24hr")).json())


async def book_ticker_once(f: Fetcher, sinks: Sinks, symbols: set[str]) -> int:
    """ADR-003: one REST call returns every symbol with its event `time`; keep the selected ones."""
    rows = cast(list[dict[str, Any]], (await f.get(f"{FAPI}/fapi/v1/ticker/bookTicker")).json())
    kept = [r for r in rows if r.get("symbol") in symbols]
    sinks.write("binance.um.book_ticker_1s", kept)
    return len(kept)


async def metrics_refetch(
    f: Fetcher,
    sinks: Sinks,
    symbols: Sequence[str],
    *,
    now_ms: Callable[[], int],
    days: float = 30,
    pause_s: float = 0.35,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    stop: asyncio.Event | None = None,
    on_error: Callable[[str, str, Exception], None] | None = None,
    unsupported: set[tuple[str, str]] | None = None,
) -> int:
    """Re-download the rolling 30-day window of 5m derivatives metrics (design v2.1: every row version is
    kept; the normalizer adds only changed rows as new versions). ~1000 req/5 min/IP -> paced.

    A full pass takes tens of minutes, so the window is recomputed per symbol (never older than the API
    keeps), a failing symbol does not skip the rest (on_error records it), and `stop` is honoured per page.
    BannedError always propagates. A (source, symbol) answered with -4104 (contract type not offered, e.g.
    basis for TradFi perps) joins `unsupported` and is skipped by later passes sharing that set."""
    calls = 0
    for source, (path, key) in METRIC_ENDPOINTS.items():
        for sym in symbols:
            if unsupported is not None and (source, sym) in unsupported:
                continue
            now = now_ms()
            start = int(now - days * 86_400_000) + PERIOD_MS
            try:
                while start < now:
                    if stop is not None and stop.is_set():
                        return calls
                    end = min(start + PAGE_ROWS * PERIOD_MS, now)
                    params: dict[str, str | int] = {key: sym, "period": "5m", "limit": PAGE_ROWS, "startTime": start}
                    params["endTime"] = end
                    if key == "pair":
                        params["contractType"] = "PERPETUAL"
                    r = await f.get(f"{FAPI}{path}", params)
                    sinks.write(source, {"params": params, "data": r.json()})
                    calls += 1
                    start = end
                    await sleep(pause_s)
            except BannedError:
                raise
            except ExternalServiceError as e:
                if unsupported is not None and UNSUPPORTED_CONTRACT in str(e):
                    unsupported.add((source, sym))
                if on_error is None:
                    raise
                on_error(source, sym, e)
    return calls


async def measure_clock(
    f: Fetcher, clock: ClockReference, sinks: Sinks, *, now_ms: Callable[[], int] | None = None, burst: int = 3
) -> None:
    """Burst of /api/v3/time calls; the lowest-RTT sample becomes the offset (ADR-005)."""
    clock_ms = now_ms or (lambda: time.time_ns() // 1_000_000)
    samples: list[tuple[float, int]] = []
    for _ in range(burst):
        t0 = clock_ms()
        server = int(cast(dict[str, Any], (await f.get(f"{SPOT}/api/v3/time")).json())["serverTime"])
        t1 = clock_ms()
        samples.append(clock_offset_ms(local_send_ms=t0, server_ms=server, local_recv_ms=t1))
    clock.update(samples, measured_ns=time.time_ns())
    sinks.write("binance.server_time", {"samples": samples, "offset_ms": clock.offset_ms, "rtt_ms": clock.rtt_ms})


def handle_force_order(sinks: Sinks, raw: str | bytes) -> None:
    text = raw if isinstance(raw, str) else raw.decode("utf-8")
    sinks.write("binance.um.force_order", json.loads(text))
