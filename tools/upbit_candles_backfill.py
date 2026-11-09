"""Step 9 runner: Upbit 1-minute candle backfill for the KRW top-N markets. Resumable and idempotent.

    uv run python tools/upbit_candles_backfill.py [--markets KRW-BTC ...] [--max-pages N] [--rate 6]
        [--pages-per-chunk 50] [--log-file PATH]

The first run fixes the market list and the run end (now - 10 min, floored to the minute) in
<data_root>/bulk/upbit-candles-run.json; later runs resume that same run from the catalog, so a stop
never leaves holes. Safety (same pattern as the bulk download, ADR-014):
- single instance (`run/upbit-backfill.lock`, exit 2), one request at a time at <= --rate per second;
- an Upbit 418 (IP block) stops the run at once (exit 5): the live collectors share the IP;
- before every page: SSD thermal state + free disk / RAM / own memory gate; a stop exits 3, rerun resumes;
- progress in `run/upbit-backfill.progress.json`; a complete run writes `bulk/upbit-candles-complete.json`.
Exit codes: 0 complete, 1 error, 2 already running, 3 paused by a gate, 4 pilot limit reached, 5 Upbit 418.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import time
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx

EXIT_OK, EXIT_ERROR, EXIT_RUNNING, EXIT_PAUSED, EXIT_PILOT, EXIT_BLOCKED = 0, 1, 2, 3, 4, 5
MIN_NS = 60 * 10**9


class UpbitBlockedError(Exception):
    """HTTP 418 from Upbit: stop the backfill instead of retrying into a longer block."""


async def _raise_on_418(response: httpx.Response) -> None:
    if response.status_code == 418:
        raise UpbitBlockedError(f"Upbit 418 (Retry-After={response.headers.get('Retry-After')})")


def page_limited(max_pages: int | None, gate: Callable[[], str | None]) -> Callable[[], str | None]:
    """Stop check for one market: the resource/thermal gate, plus an optional pilot page limit."""
    pages = 0

    def should_stop() -> str | None:
        nonlocal pages
        if max_pages is not None and pages >= max_pages:
            return "pilot page limit"
        pages += 1
        return gate()

    return should_stop


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", nargs="*", default=None, help="override the top-N selection (pilot)")
    ap.add_argument("--max-pages", type=int, default=None, help="pilot: stop each market after N pages")
    ap.add_argument("--rate", type=float, default=6.0, help="max requests per second (candle group allows 10)")
    ap.add_argument("--pages-per-chunk", type=int, default=50)
    ap.add_argument("--log-file", type=Path, default=None)
    ap.add_argument("--polars-threads", type=int, default=2)
    a = ap.parse_args()
    os.environ.setdefault("POLARS_MAX_THREADS", str(a.polars_threads))

    from quant_research.adapters.jobhost import Log, memory_sample, write_json
    from quant_research.backfill.upbit_candles import DATASET, CandleStore, backfill_market
    from quant_research.bulk.guard import Limits, Sample, combine, resource_gate
    from quant_research.capture.http import Fetcher
    from quant_research.capture.lock import AlreadyRunningError, InstanceLock
    from quant_research.capture.thermal import heavy_work_allowed, read_state
    from quant_research.capture.upbit import top_krw_markets
    from quant_research.catalog.bootstrap import bootstrap
    from quant_research.catalog.store import Catalog
    from quant_research.config import load_settings

    log = Log(a.log_file)
    settings = load_settings()
    root = settings.data_root
    pilot = a.max_pages is not None or a.markets is not None
    manifest_path = root / "bulk" / ("upbit-candles-pilot.json" if pilot else "upbit-candles-run.json")
    complete_path = root / "bulk" / "upbit-candles-complete.json"
    progress_path = root / "run" / "upbit-backfill.progress.json"
    if not pilot and complete_path.exists():
        log(f"ALREADY COMPLETE ({complete_path}); nothing to do")
        log.close()
        return EXIT_OK
    try:
        lock = InstanceLock(root / "run" / "upbit-backfill.lock").__enter__()
    except AlreadyRunningError as e:
        log(f"REFUSED: {e}")
        log.close()
        return EXIT_RUNNING

    limits = Limits()
    state_path = root / "run" / "ssd_state.json"
    exit_code = EXIT_ERROR
    started = time.time()
    totals = {"requests": 0, "rows": 0, "chunks": 0, "no_trade_spans": 0, "markets_done": 0}
    catalog = Catalog.open(root / "meta" / "catalog.db")
    bootstrap(catalog)

    def gate() -> str | None:
        mem = memory_sample()
        sample = Sample(shutil.disk_usage(root).free, 0, mem["available_ram"], mem["private"])
        allowed, why = combine(
            heavy_work_allowed(read_state(state_path), now_ns=time.time_ns()), resource_gate(sample, limits)
        )
        return None if allowed else why

    async def run() -> int:
        nonlocal totals
        async with httpx.AsyncClient(
            timeout=15, headers={"User-Agent": "quant-research-backfill"}, event_hooks={"response": [_raise_on_418]}
        ) as client:
            f = Fetcher(client, "upbit")
            if manifest_path.exists():  # resume the same run: same markets, same fixed end
                raw = cast(dict[str, object], json.loads(manifest_path.read_text(encoding="utf-8")))
                markets = [str(m) for m in cast(list[object], raw["markets"])]
                run_end = int(cast(int, raw["run_end_ns"]))
                selected = str(raw["selected_utc"])
            else:
                markets = list(a.markets or await top_krw_markets(f, settings.upbit_top_n))
                run_end = (time.time_ns() - 10 * MIN_NS) // MIN_NS * MIN_NS
                selected = datetime.now(UTC).isoformat(timespec="seconds")
            manifest: dict[str, object] = {
                "selected_utc": selected,
                "selection": "explicit --markets" if a.markets else f"KRW top {settings.upbit_top_n} by 24h value",
                "markets": markets,
                "run_end_ns": run_end,
            }
            if not manifest_path.exists():
                write_json(manifest_path, manifest)
            store = CandleStore(root, catalog, run_end)
            interval = 1.0 / a.rate
            last = 0.0

            async def throttle() -> None:
                nonlocal last
                wait = last + interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                last = time.monotonic()

            log(
                f"START pid={os.getpid()} pilot={pilot} markets={len(markets)} run_end={run_end} "
                f"rate={a.rate}/s POLARS_MAX_THREADS={os.environ['POLARS_MAX_THREADS']}"
            )
            for n, market in enumerate(markets, 1):
                r = await backfill_market(
                    f,
                    store,
                    market,
                    pages_per_chunk=a.pages_per_chunk,
                    should_stop=page_limited(a.max_pages, gate),
                    throttle=throttle,
                )
                for key in ("requests", "rows", "chunks", "no_trade_spans"):
                    totals[key] += getattr(r, key)
                totals["markets_done"] += int(r.done)
                mem = memory_sample()
                status = {
                    "state": "running",
                    "updated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                    "pid": os.getpid(),
                    "market_index": n,
                    "markets": len(markets),
                    "market": market,
                    "market_done": r.done,
                    "elapsed_s": int(time.time() - started),
                    "totals": totals,
                    "memory_mb": {k: v // 2**20 for k, v in mem.items()},
                }
                write_json(progress_path, status)
                log(
                    f"[{n}/{len(markets)}] {market} done={r.done} rows={r.rows} requests={r.requests} "
                    f"no_trade={r.no_trade_spans} stop={r.stopped_reason} totals={totals} "
                    f"private_mb={mem['private'] // 2**20} avail_mb={mem['available_ram'] // 2**20}"
                )
                if r.stopped_reason == "pilot page limit":
                    continue
                if r.stopped_reason is not None:
                    write_json(progress_path, {**status, "state": "paused", "note": r.stopped_reason})
                    return EXIT_PAUSED
            if pilot:
                return EXIT_PILOT if totals["markets_done"] < len(markets) else EXIT_OK
            done = catalog.conn.execute(
                "select count(*) from backfill_done where dataset_id=? and run_end_ns=?",
                (DATASET, run_end),
            ).fetchone()[0]
            if done == len(markets):
                write_json(complete_path, {**manifest, "completed_utc": datetime.now(UTC).isoformat(), **totals})
                log(f"COMPLETE marker written: {complete_path}")
                return EXIT_OK
            return EXIT_PAUSED

    try:
        exit_code = asyncio.run(run())
        return exit_code
    except UpbitBlockedError as e:
        log(f"BLOCKED {e}; stopped to protect the live collectors; rerun later resumes")
        exit_code = EXIT_BLOCKED
        return exit_code
    except Exception:
        # pythonw has no console: record the traceback before it is lost, then let it propagate
        log(f"CRASH {traceback.format_exc()}")
        raise
    finally:
        catalog.close()
        log(f"END exit={exit_code} totals={totals} elapsed={time.time() - started:.0f}s")
        log.close()
        lock.__exit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(main())
