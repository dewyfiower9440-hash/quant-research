"""Phase 0 environment probe (thin I/O shell; pure logic lives in quant_research.domain.probe).

Public endpoints only, no API keys, no orders. Bulk downloads are limited to two small BTCUSDT
sample files. Results are written as JSON under C:/dev/qr-data/measurements/.

Usage:
    uv run python tools/phase0_probe.py --ws-seconds 30
    uv run python tools/phase0_probe.py --ws-seconds 3600 --only ws   # 1-hour volume measurement
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import io
import json
import shutil
import time
import uuid
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import httpx
import websockets

from quant_research.domain.probe import (
    aligned_to_interval,
    clock_offset_ms,
    detect_epoch_unit,
    earliest_with_data,
    message_rate,
)

OUT_DIR = Path("C:/dev/qr-data/measurements")
TIMEOUT = httpx.Timeout(10.0)
UA = {"User-Agent": "quant-research-phase0-probe"}


def now_ms() -> int:
    return time.time_ns() // 1_000_000


# --------------------------------------------------------------------------- clocks
def probe_clocks(client: httpx.Client) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, url, field in [
        ("binance_spot", "https://api.binance.com/api/v3/time", "serverTime"),
        ("binance_futures", "https://fapi.binance.com/fapi/v1/time", "serverTime"),
    ]:
        samples = []
        for _ in range(5):
            t0 = now_ms()
            r = client.get(url)
            t1 = now_ms()
            r.raise_for_status()
            off, rtt = clock_offset_ms(local_send_ms=t0, server_ms=int(r.json()[field]), local_recv_ms=t1)
            samples.append({"offset_ms": off, "rtt_ms": rtt})
            time.sleep(0.2)
        best = min(samples, key=lambda s: s["rtt_ms"])
        out[name] = {"best": best, "samples": samples}
    # Upbit has no time endpoint (unverified that one exists); the HTTP Date header has 1 s resolution.
    t0 = now_ms()
    r = client.get("https://api.upbit.com/v1/market/all")
    t1 = now_ms()
    date_hdr = r.headers.get("date")
    if date_hdr:
        server = int(datetime.strptime(date_hdr, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=UTC).timestamp() * 1000)
        off, rtt = clock_offset_ms(local_send_ms=t0, server_ms=server, local_recv_ms=t1)
        out["upbit_http_date"] = {"offset_ms": off, "rtt_ms": rtt, "resolution_ms": 1000}
    return out


# --------------------------------------------------------------------------- universes
def top_futures_symbols(client: httpx.Client, n: int) -> list[str]:
    r = client.get("https://fapi.binance.com/fapi/v1/ticker/24hr")
    r.raise_for_status()
    rows = [x for x in r.json() if str(x["symbol"]).endswith("USDT")]
    rows.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
    return [str(x["symbol"]) for x in rows[:n]]


def top_upbit_krw_markets(client: httpx.Client, n: int) -> list[str]:
    r = client.get("https://api.upbit.com/v1/market/all")
    r.raise_for_status()
    krw = [str(m["market"]) for m in r.json() if str(m["market"]).startswith("KRW-")]
    tick: list[dict[str, Any]] = []
    for i in range(0, len(krw), 100):
        rr = client.get("https://api.upbit.com/v1/ticker", params={"markets": ",".join(krw[i : i + 100])})
        rr.raise_for_status()
        tick.extend(rr.json())
        time.sleep(0.15)
    tick.sort(key=lambda x: float(x["acc_trade_price_24h"]), reverse=True)
    return [str(x["market"]) for x in tick[:n]]


# --------------------------------------------------------------------------- websockets
async def count_ws(url: str, seconds: float, subscribe: Any | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"url": url, "connected": False, "messages": 0, "bytes": 0, "error": None}
    cpu0 = time.process_time()
    try:
        async with websockets.connect(url, open_timeout=10, max_size=2**22) as ws:
            result["connected"] = True
            if subscribe is not None:
                await ws.send(json.dumps(subscribe))
            deadline = time.monotonic() + seconds
            samples: list[str] = []
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except TimeoutError:
                    break
                data = msg if isinstance(msg, str) else msg.decode("utf-8", "replace")
                result["messages"] += 1
                result["bytes"] += len(data.encode("utf-8"))
                if len(samples) < 3:
                    samples.append(data[:400])
            result["samples"] = samples
    except Exception as e:  # probe boundary: record any connection failure as data, never crash the run
        result["error"] = f"{type(e).__name__}: {e}"
    result["cpu_s"] = round(time.process_time() - cpu0, 3)
    if result["connected"]:
        result["rate_per_s"] = round(message_rate(result["messages"], seconds), 2)
        result["bytes_per_day_est"] = int(result["bytes"] / seconds * 86_400)
    return result


async def probe_ws(fut_syms: list[str], upbit_mkts: list[str], seconds: float) -> dict[str, Any]:
    s20 = "/".join(f"{s.lower()}@bookTicker" for s in fut_syms)
    candidates = {
        "binance_spot_bookTicker_btc": "wss://stream.binance.com:9443/stream?streams=btcusdt@bookTicker",
        "binance_spot_data_stream": "wss://data-stream.binance.vision/stream?streams=btcusdt@bookTicker",
        "futures_public_bookTicker_top20": f"wss://fstream.binance.com/public/stream?streams={s20}",
        "futures_legacy_bookTicker_top20": f"wss://fstream.binance.com/stream?streams={s20}",
        "futures_market_markPrice_arr": "wss://fstream.binance.com/market/stream?streams=!markPrice@arr@1s",
        "futures_market_forceOrder_arr": "wss://fstream.binance.com/market/stream?streams=!forceOrder@arr",
    }
    tasks = {k: count_ws(v, seconds) for k, v in candidates.items()}
    upbit_sub = [
        {"ticket": str(uuid.uuid4())},
        {"type": "trade", "codes": upbit_mkts},
        {"type": "orderbook", "codes": [f"{m}.1" for m in upbit_mkts]},
        {"format": "SIMPLE"},
    ]
    tasks["upbit_trade_orderbook_top"] = count_ws("wss://api.upbit.com/websocket/v1", seconds, upbit_sub)
    results = await asyncio.gather(*tasks.values())
    return dict(zip(tasks.keys(), results, strict=True))


# --------------------------------------------------------------------------- bulk samples
def probe_bulk(client: httpx.Client) -> dict[str, Any]:
    out: dict[str, Any] = {}
    day = (date.today() - timedelta(days=2)).isoformat()
    for ds in ("metrics", "bookDepth"):
        url = f"https://data.binance.vision/data/futures/um/daily/{ds}/BTCUSDT/BTCUSDT-{ds}-{day}.zip"
        info: dict[str, Any] = {"url": url}
        r = client.get(url)
        info["status"] = r.status_code
        if r.status_code == 200:
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                name = z.namelist()[0]
                text = z.read(name).decode("utf-8")
            lines = text.splitlines()
            info["zip_bytes"] = len(r.content)
            info["rows"] = len(lines) - 1
            info["header"] = lines[0]
            info["first_rows"] = lines[1:4]
            info["last_row"] = lines[-1]
        out[ds] = info
    return out


# --------------------------------------------------------------------------- upbit history
def probe_upbit_minutes(client: httpx.Client) -> dict[str, Any]:
    start = date(2017, 1, 1)
    calls: list[dict[str, Any]] = []

    def has_data(day_index: int) -> bool:
        d = start + timedelta(days=day_index)
        to = datetime(d.year, d.month, d.day, tzinfo=UTC) + timedelta(days=1)
        r = client.get(
            "https://api.upbit.com/v1/candles/minutes/1",
            params={"market": "KRW-BTC", "to": to.strftime("%Y-%m-%dT%H:%M:%SZ"), "count": 1},
        )
        time.sleep(0.15)
        r.raise_for_status()
        rows = r.json()
        ok = bool(rows) and str(rows[0]["candle_date_time_utc"])[:10] == d.isoformat()
        calls.append({"day": d.isoformat(), "rows": len(rows), "ok": ok})
        return ok

    hi = (date.today() - start).days
    first = earliest_with_data(has_data, lo=0, hi=hi)
    return {
        "market": "KRW-BTC",
        "earliest_day_with_1m": None if first is None else (start + timedelta(days=first)).isoformat(),
        "probes": calls,
        "note": "assumes monotone retention (no data before X, data after); gaps on a probed day would bias it",
    }


async def probe_upbit_ask_bid(market: str, seconds: float) -> dict[str, Any]:
    """Classify trades against the best quote seen just before them to infer ask_bid meaning."""
    best: dict[str, float] = {}
    counts: dict[str, int] = {}
    sub = [
        {"ticket": str(uuid.uuid4())},
        {"type": "orderbook", "codes": [f"{market}.1"]},
        {"type": "trade", "codes": [market]},
        {"format": "DEFAULT"},
    ]
    async with websockets.connect("wss://api.upbit.com/websocket/v1", open_timeout=10) as ws:
        await ws.send(json.dumps(sub))
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except TimeoutError:
                break
            m = json.loads(raw)
            if m.get("type") == "orderbook":
                u = m["orderbook_units"][0]
                best = {"ask": float(u["ask_price"]), "bid": float(u["bid_price"])}
            elif m.get("type") == "trade" and best:
                p = float(m["trade_price"])
                side = "at_or_above_ask" if p >= best["ask"] else "at_or_below_bid" if p <= best["bid"] else "inside"
                key = f"{m['ask_bid']}->{side}"
                counts[key] = counts.get(key, 0) + 1
    return {"market": market, "seconds": seconds, "counts": counts}


# --------------------------------------------------------------------------- compute + host
def probe_compute() -> dict[str, Any]:
    import duckdb
    import numba
    import numpy as np
    import polars as pl

    out: dict[str, Any] = {}
    rng = np.random.default_rng(0)
    n = 5_000_000
    df = pl.DataFrame({"sym": rng.integers(0, 100, n), "px": rng.random(n)})
    t = time.perf_counter()
    df.group_by("sym").agg(pl.col("px").mean().alias("px_mean"), pl.col("px").std().alias("px_std"))
    out["polars_groupby_5m_rows_s"] = round(time.perf_counter() - t, 3)
    t = time.perf_counter()
    df.with_columns(pl.col("px").rolling_mean(60).over("sym"))
    out["polars_rolling60_over_sym_5m_rows_s"] = round(time.perf_counter() - t, 3)
    con = duckdb.connect()
    con.register("df", df.to_arrow())
    t = time.perf_counter()
    con.execute("select sym, avg(px), stddev(px) from df group by sym").fetchall()
    out["duckdb_groupby_5m_rows_s"] = round(time.perf_counter() - t, 3)

    @numba.njit(cache=False)
    def path_loop(px: Any) -> float:  # path-dependent stop logic shape
        pnl = 0.0
        peak = px[0]
        for i in range(1, px.shape[0]):
            peak = max(peak, px[i])
            if px[i] < peak * 0.98:
                pnl += px[i] - peak
                peak = px[i]
        return pnl

    arr = rng.random(n)
    t = time.perf_counter()
    path_loop(arr)
    out["numba_first_call_incl_jit_s"] = round(time.perf_counter() - t, 3)
    t = time.perf_counter()
    path_loop(arr)
    out["numba_loop_5m_s"] = round(time.perf_counter() - t, 4)
    return out


def probe_host() -> dict[str, Any]:
    class MemStatus(ctypes.Structure):
        _fields_: ClassVar[list[tuple[str, type[ctypes.c_ulong] | type[ctypes.c_ulonglong]]]] = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    st = MemStatus()
    st.dwLength = ctypes.sizeof(MemStatus)
    kernel32 = ctypes.WinDLL("kernel32")
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        raise OSError("GlobalMemoryStatusEx failed")
    du = shutil.disk_usage("C:/")
    return {
        "ram_total_gb": round(st.ullTotalPhys / 2**30, 2),
        "ram_avail_gb": round(st.ullAvailPhys / 2**30, 2),
        "disk_c_free_gb": round(du.free / 2**30, 1),
        "ssd_smart_temperature": "미측정: Get-StorageReliabilityCounter requires admin (not approved)",
    }


def interpret_bulk_timestamps(bulk: dict[str, Any]) -> dict[str, Any]:
    """First column of each sample: epoch unit (if numeric) and 5-minute grid alignment."""
    out: dict[str, Any] = {}
    for ds, info in bulk.items():
        if info.get("status") != 200:
            out[ds] = {"status": info.get("status")}
            continue
        cells = [row.split(",")[0] for row in info["first_rows"]]
        entry: dict[str, Any] = {"first_cells": cells}
        if all(c.isdigit() for c in cells):
            values = [int(c) for c in cells]
            unit = detect_epoch_unit(values[0])
            scale = {"s": 1000, "ms": 1, "us": 1 / 1000, "ns": 1 / 1_000_000}[unit]
            entry["unit"] = unit
            entry["aligned_5m"] = aligned_to_interval([int(v * scale) for v in values], 300_000)
        else:
            entry["unit"] = "text datetime"
        out[ds] = entry
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws-seconds", type=float, default=30.0)
    ap.add_argument("--only", choices=["all", "ws"], default="all")
    a = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"started_utc": datetime.now(UTC).isoformat(), "ws_seconds": a.ws_seconds}
    with httpx.Client(timeout=TIMEOUT, headers=UA) as client:
        fut = top_futures_symbols(client, 20)
        upb = top_upbit_krw_markets(client, 20)
        report["universe"] = {"binance_futures_top20": fut, "upbit_krw_top20": upb}
        if a.only == "all":
            report["clocks"] = probe_clocks(client)
            report["bulk_samples"] = probe_bulk(client)
            report["upbit_minute_history"] = probe_upbit_minutes(client)
    report["ws"] = asyncio.run(probe_ws(fut, upb, a.ws_seconds))
    if a.only == "all":
        try:
            report["upbit_ask_bid"] = asyncio.run(probe_upbit_ask_bid("KRW-BTC", 60))
        except Exception as e:  # probe boundary: the failure itself is a measurement result
            report["upbit_ask_bid"] = {"error": f"{type(e).__name__}: {e}"}
        report["compute"] = probe_compute()
        report["host"] = probe_host()
        report["bulk_timestamps"] = interpret_bulk_timestamps(report["bulk_samples"])
    report["finished_utc"] = datetime.now(UTC).isoformat()
    path = OUT_DIR / f"phase0-{a.only}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
