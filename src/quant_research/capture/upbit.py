"""Upbit T0 collectors (design v2.1 §5.2). Verified 2026-11-08: orderbook `count=1` works, trade
`cursor` pages to older trades, `days_ago` accepts 1..7, the `announcement` WS type is rejected
(INVALID_PARAM) on the Korean API, so announcements are not collected."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from quant_research.capture.http import Fetcher, HttpStatusError, ThrottledError
from quant_research.capture.recovery import Gap
from quant_research.capture.sinks import Sinks
from quant_research.errors import ExternalServiceError

API = "https://api.upbit.com/v1"  # no trailing slash: paths ending in "/" fail from 2026-11-16
TRADE_PAGE = 500


async def top_krw_markets(f: Fetcher, n: int) -> list[str]:
    markets = cast(list[dict[str, Any]], (await f.get(f"{API}/market/all")).json())
    krw = [str(m["market"]) for m in markets if str(m["market"]).startswith("KRW-")]
    tickers: list[dict[str, Any]] = []
    for i in range(0, len(krw), 100):
        tickers.extend(
            cast(list[dict[str, Any]], (await f.get(f"{API}/ticker", {"markets": ",".join(krw[i : i + 100])})).json())
        )
    tickers.sort(key=lambda t: float(t["acc_trade_price_24h"]), reverse=True)
    return [str(t["market"]) for t in tickers[:n]]


async def market_snapshot(f: Fetcher, sinks: Sinks) -> None:
    """Daily: market list with warning/caution flags (their history exists only if we snapshot it)."""
    sinks.write("upbit.markets", (await f.get(f"{API}/market/all", {"is_details": "true"})).json())


async def orderbook_once(f: Fetcher, sinks: Sinks, markets: list[str]) -> None:
    payload = (await f.get(f"{API}/orderbook", {"markets": ",".join(markets), "count": 1})).json()
    sinks.write("upbit.orderbook_l1_1s", payload)


async def orderbook_refreshing(
    f: Fetcher, sinks: Sinks, markets: list[str], refresh: Callable[[], Awaitable[list[str]]]
) -> bool:
    """`orderbook_once`, and when the venue no longer knows one of the codes (404), take a new list from `refresh`
    and ask once more. `markets` is replaced in place (the trades job reads the same list). True when the list was
    refreshed. v2.5 N01: the list used to be fixed at start, and one delisted code failed every batched request
    for 74 hours until a restart."""
    if not markets:  # the list could not be read at start (the venue was down)
        fresh = await refresh()
        if not fresh:
            raise ExternalServiceError("no Upbit markets to ask for (the market list is empty)")
        markets[:] = fresh
        sinks.write("collector.universe", {"upbit_krw": list(markets), "reason": "empty_list"})
        await orderbook_once(f, sinks, markets)
        return True
    try:
        await orderbook_once(f, sinks, markets)
        return False
    except HttpStatusError as e:
        if e.status != 404:
            raise
        try:
            fresh = await refresh()
        except ThrottledError:
            raise e from None  # the 404 is the failure to report and to count, not our own refresh limit
        if not fresh or fresh == markets:
            raise  # nothing new to ask with: the same request would fail the same way
    markets[:] = fresh
    sinks.write("collector.universe", {"upbit_krw": list(markets), "reason": "unknown_market"})
    await orderbook_once(f, sinks, markets)
    return True


@dataclass
class TradeCursorState:
    """Newest trade timestamp captured per market; persisted so restarts resume (7-day API window)."""

    path: Path
    last_ts_ms: dict[str, int] = field(default_factory=lambda: dict[str, int]())

    @classmethod
    def load(cls, path: Path) -> TradeCursorState:
        if not path.exists():
            return cls(path)
        data = cast(dict[str, int], json.loads(path.read_text(encoding="utf-8")))
        return cls(path, {k: int(v) for k, v in data.items()})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.last_ts_ms), encoding="utf-8")
        os.replace(tmp, self.path)  # only this process reads/writes the state file


async def trades_catchup(
    f: Fetcher,
    sinks: Sinks,
    market: str,
    state: TradeCursorState,
    *,
    stop: asyncio.Event | None = None,
) -> int:
    """Page backwards from the newest trade until the previously captured timestamp (or 7 days back on
    the first run). Overlap is intentional; the normalizer deduplicates by `sequential_id`.

    The saved cursor only advances after a complete pass, so an interrupted pass is redone next time.
    If the previous capture is older than the API window (7 days), the uncovered span is recorded as a
    gap instead of being silently lost."""
    known = state.last_ts_ms.get(market)
    newest = known
    oldest_seen: int | None = None
    pages = 0
    reached_known = False
    for days_ago in range(8):  # 0 = today (no parameter), 1..7 = earlier UTC days
        cursor: int | None = None
        while True:
            if stop is not None and stop.is_set():
                return pages  # incomplete pass: keep the old cursor
            params: dict[str, str | int] = {"market": market, "count": TRADE_PAGE}
            if days_ago:
                params["days_ago"] = days_ago
            if cursor is not None:
                params["cursor"] = cursor
            rows = cast(list[dict[str, Any]], (await f.get(f"{API}/trades/ticks", params)).json())
            pages += 1
            if not rows:
                break
            sinks.write("upbit.trades", {"market": market, "days_ago": days_ago, "rows": rows})
            stamps = [int(r["timestamp"]) for r in rows]
            newest = max(newest or 0, *stamps)
            oldest_seen = min(oldest_seen or stamps[0], *stamps)
            if known is not None and min(stamps) <= known:
                reached_known = True
                break
            if len(rows) < TRADE_PAGE:
                break
            next_cursor = int(rows[-1]["sequential_id"])
            if cursor is not None and next_cursor >= cursor:
                raise ExternalServiceError(
                    f"upbit trades cursor did not advance for {market}: {cursor} -> {next_cursor}"
                )
            cursor = next_cursor
        if reached_known:
            break
    if known is not None and not reached_known and oldest_seen is not None:
        sinks.record_gap(Gap("upbit.trades", known, oldest_seen, f"api_window_exceeded:{market}"))
    if newest is not None:
        state.last_ts_ms[market] = newest
        state.save()
    return pages
