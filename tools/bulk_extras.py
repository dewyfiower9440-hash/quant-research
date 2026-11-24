"""Phase-1 completion step 4: T1 extra bulk data (design v2.1 §5.5). Resumable and idempotent.

    uv run python tools/bulk_extras.py --universe C:/dev/qr-data/bulk/universe-20261109.json \
        [--kinds fundingRate bookDepth metrics spotKlines] [--symbols BTCUSDT] [--limit-months 1] [--log-file PATH]

Kinds, in this order (cheap first):
- fundingRate  monthly, every universe symbol, its membership months (+ warm-up)
- bookDepth    daily, the BOOK_DEPTH_TOP symbols with the most membership months (design: "상위 20")
- metrics      daily, every universe symbol, its membership months (+ warm-up)
- spotKlines   monthly 1m, the spot pair of every universe symbol (1000SHIBUSDT -> SHIBUSDT), if listed
- aggTrades    (only when named) monthly, the AGG_FLOW_TOP symbols, aggregated to 1m taker flow (bulk/flow.py);
               the raw zip is deleted after aggregation (v2.2 B4 tier T5)
v2.2 B4 tiers: `--top N` keeps the N symbols with the most membership months for every kind of the run,
`--since YYYY-MM` skips earlier months (bookDepth tier T4 from 2023).
Only finished months are taken (the current UTC month is skipped). Daily files are combined per
symbol-month (bulk/extras.py). Same safety as tools/bulk_download.py: shares `run/bulk.lock` (so it never
runs beside the klines download; a pilot uses run/bulk-extras-pilot.lock), thermal + resource gate
before every month (exit 3), progress in `run/bulk-extras.progress.json`, completion marker
`bulk/complete-extras-<universe>.json`.
Exit codes: 0 complete, 1 error, 2 already running (or klines download running), 3 paused by a gate,
4 finished with failed months (retried on the next start).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

import httpx

BOOK_DEPTH_TOP = 20
EXIT_OK, EXIT_ERROR, EXIT_RUNNING, EXIT_PAUSED, EXIT_FAILED_FILES = 0, 1, 2, 3, 4
ALL_KINDS = ["fundingRate", "bookDepth", "metrics", "spotKlines"]
AGG_FLOW_TOP = 10


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def year_month(text: str) -> str:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", text):
        raise argparse.ArgumentTypeError(f"expected YYYY-MM, got {text!r}")
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", type=Path, required=True)
    ap.add_argument("--kinds", nargs="*", default=ALL_KINDS, choices=[*ALL_KINDS, "aggTrades"])
    ap.add_argument("--top", type=positive_int, default=None, help="tier: the N symbols with the most member months")
    ap.add_argument("--since", type=year_month, default=None, help="tier: skip months before YYYY-MM")
    ap.add_argument("--symbols", nargs="*", default=None, help="futures symbols (pilot)")
    ap.add_argument(
        "--limit-months", type=positive_int, default=None, help="pilot: newest N months per symbol and kind"
    )
    ap.add_argument("--pause", type=float, default=0.05)
    ap.add_argument("--log-file", type=Path, default=None)
    ap.add_argument("--polars-threads", type=int, default=2)
    a = ap.parse_args()
    os.environ.setdefault("POLARS_MAX_THREADS", str(a.polars_threads))  # before Polars is imported

    from quant_research.adapters.jobhost import Log, memory_sample, write_json
    from quant_research.bulk.download import BulkTarget
    from quant_research.bulk.extras import (
        FIVE_MIN_NS,
        Parser,
        download_month_group,
        parse_book_depth_csv,
        parse_funding_csv,
        parse_metrics_csv,
    )
    from quant_research.bulk.flow import AGG_FLOW_DATASET, download_flow_month
    from quant_research.bulk.guard import Limits, Sample, combine, resource_gate
    from quant_research.bulk.klines import MINUTE_NS, parse_kline_csv
    from quant_research.bulk.plan import RESEARCH_WINDOWS, Checked, coverage, order_symbols, unavailable_months
    from quant_research.bulk.s3 import listing, wanted_months
    from quant_research.capture.lock import AlreadyRunningError, InstanceLock
    from quant_research.capture.thermal import heavy_work_allowed, read_state
    from quant_research.catalog.bootstrap import bootstrap
    from quant_research.catalog.store import Catalog
    from quant_research.catalog.universe_load import split_multiplier
    from quant_research.config import load_settings

    kinds: dict[str, tuple[str, str, bool, Parser | None, int | None]] = {
        "aggTrades": (AGG_FLOW_DATASET, "data/futures/um/monthly/aggTrades/{s}/", False, None, None),
        "fundingRate": (
            "binance.um.bulk.funding",
            "data/futures/um/monthly/fundingRate/{s}/",
            False,
            parse_funding_csv,
            None,
        ),
        "bookDepth": (
            "binance.um.bulk.book_depth",
            "data/futures/um/daily/bookDepth/{s}/",
            True,
            parse_book_depth_csv,
            None,
        ),
        "metrics": (
            "binance.um.bulk.metrics_5m",
            "data/futures/um/daily/metrics/{s}/",
            True,
            parse_metrics_csv,
            FIVE_MIN_NS,
        ),
        "spotKlines": (
            "binance.spot.bulk.klines_1m",
            "data/spot/monthly/klines/{s}/1m/",
            False,
            parse_kline_csv,
            MINUTE_NS,
        ),
    }

    log = Log(a.log_file)
    settings = load_settings()
    root = settings.data_root
    full_run = (
        a.symbols is None
        and a.limit_months is None
        and a.top is None
        and a.since is None
        and sorted(a.kinds) == sorted(ALL_KINDS)
    )
    marker = root / "bulk" / f"complete-extras-{a.universe.stem}.json"
    progress_path = root / "run" / "bulk-extras.progress.json"
    if full_run and marker.exists():
        log(f"ALREADY COMPLETE ({marker}); nothing to do")
        log.close()
        return EXIT_OK
    # A full run shares the klines lock (never two bulk jobs at once). A pilot (--symbols/--limit-months,
    # a few files) uses its own lock so it can check a kind while the klines download runs.
    pilot = a.symbols is not None or a.limit_months is not None  # a few files: may run beside the klines job
    lock_name = "bulk-extras-pilot.lock" if pilot else "bulk.lock"  # tier runs (--top/--since/aggTrades) share it
    try:
        lock = InstanceLock(root / "run" / lock_name).__enter__()
    except AlreadyRunningError as e:
        log(f"REFUSED (bulk lock held, klines download or another extras run): {e}")
        log.close()
        return EXIT_RUNNING

    report = json.loads(a.universe.read_text(encoding="utf-8"))
    months: list[str] = report["months"]
    spans: dict[str, list[list[str | None]]] = report["spans"]
    warmup = int(report.get("warmup_months", 3))
    symbols: list[str] = a.symbols or sorted(spans)
    current_month = datetime.now(UTC).strftime("%Y-%m")
    wants = {s: {m for m in wanted_months(spans[s], months, warmup) if m < current_month} for s in symbols}
    if a.since:
        wants = {s: {m for m in ms if m >= a.since} for s, ms in wants.items()}
    by_months = sorted(symbols, key=lambda s: (-len(wants[s]), s))
    if a.top:
        symbols = sorted(by_months[: a.top])
    if a.symbols is None:
        # v2.5 N02: what the open research window still waits for comes first (the alphabetical order never
        # reached the symbols after 'O'); an explicit --symbols list keeps its own order
        symbols = order_symbols({s: wants[s] for s in symbols})
    depth_symbols = by_months[:BOOK_DEPTH_TOP] if not a.top else symbols
    flow_symbols = by_months[:AGG_FLOW_TOP] if not a.top else symbols
    limits = Limits()
    state_path = root / "run" / "ssd_state.json"
    lake_dirs = [root / "lake" / spec[0] for spec in kinds.values()]

    def bulk_bytes() -> int:
        return sum(p.stat().st_size for d in lake_dirs if d.exists() for p in d.rglob("*.parquet"))

    def sample(bulk: int) -> tuple[Sample, dict[str, int]]:
        mem = memory_sample()
        return Sample(shutil.disk_usage(root).free, bulk, mem["available_ram"], mem["private"]), mem

    current_bulk = bulk_bytes()

    def gate() -> tuple[bool, str]:
        s, _mem = sample(current_bulk)
        return combine(heavy_work_allowed(read_state(state_path), now_ns=time.time_ns()), resource_gate(s, limits))

    totals = {
        "months_downloaded": 0,
        "files": 0,
        "already": 0,
        "rows": 0,
        "bytes": 0,
        "gaps": 0,
        "failed": 0,
        "symbols_skipped": 0,
    }
    started = time.time()
    exit_code = EXIT_ERROR
    catalog = Catalog.open(root / "meta" / "catalog.db")
    bootstrap(catalog)
    # a pilot looks at a few files on purpose; a tier run skips what it saw complete within the last week
    checked = Checked.load(root / "run" / "bulk-extras-checked.json")
    held: dict[str, set[tuple[str, str]]] = {}

    kind_targets: dict[str, list[str]] = {}
    last_coverage: dict[str, list[int]] = {}

    def source_of(kind: str, sym: str) -> str:
        """The symbol the files are published and catalogued under (spot drops the contract multiplier prefix)."""
        return split_multiplier(sym.removesuffix("USDT"))[0] + "USDT" if kind == "spotKlines" else sym

    def window_coverage(kind: str) -> dict[str, list[int]]:
        """[wanted, held] (symbol, month) pairs of each research window for this kind's own targets (a --top or
        book-depth run wants only its symbols), counted under the catalogued symbol name. After the last kind
        (`kind` empty) the last kind's numbers stay."""
        if not kind:
            return last_coverage
        dataset_id = kinds[kind][0]
        if kind not in held:
            rows = catalog.conn.execute(
                "select distinct symbol, month from bulk_file where dataset_id = ?", (dataset_id,)
            )
            held[kind] = {(str(s), str(m)) for s, m in rows}
        wanted = {source_of(kind, s): wants[s] for s in kind_targets.get(kind, [])}
        missing = {(source_of(kind, s), m) for s, m in checked.unavailable(kind)}
        last_coverage.clear()
        last_coverage.update(
            {
                name: list(coverage(wanted, held[kind], window, unavailable=missing))
                for name, window in zip(("w2", "w1"), RESEARCH_WINDOWS, strict=True)
            }
        )
        return last_coverage

    def progress(state: str, kind: str, n: int, of: int, sym: str, note: str = "") -> None:
        s, mem = sample(current_bulk)
        write_json(
            progress_path,
            {
                "state": state,
                "updated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                "pid": os.getpid(),
                "kind": kind,
                "kind_index": a.kinds.index(kind) + 1 if kind else len(a.kinds),
                "kinds": a.kinds,
                "symbol_index": n,
                "symbols": of,
                "symbol": sym,
                "elapsed_s": int(time.time() - started),
                "totals": totals,
                "coverage": window_coverage(kind),  # [wanted, held] per research window, unavailable months left out
                "memory_mb": {k: v // 2**20 for k, v in mem.items()},
                "free_disk_gb": round(s.free_disk / 2**30, 1),
                "extras_gb": round(current_bulk / 2**30, 2),
                "note": note,
            },
        )

    try:
        log(f"START pid={os.getpid()} kinds={a.kinds} symbols={len(symbols)} full_run={full_run} limits={limits}")
        with httpx.Client(timeout=60, headers={"User-Agent": "quant-research-bulk"}) as client:
            for kind in a.kinds:
                dataset_id, prefix, daily, parse, grid = kinds[kind]
                targets = {"bookDepth": depth_symbols, "aggTrades": flow_symbols}.get(kind, symbols)
                kind_targets[kind] = list(targets)
                processed = 0
                for n, sym in enumerate(targets, 1):
                    source_sym = source_of(kind, sym)
                    if not pilot and checked.fresh(kind, sym, wants[sym]):
                        # seen complete within a week for at least these months: no listing request
                        totals["symbols_skipped"] += 1
                        continue
                    processed += 1
                    failed_before = totals["failed"]
                    files = listing(client, prefix.format(s=source_sym))
                    by_month: dict[str, list[BulkTarget]] = {}
                    for key, size, mod in files:
                        month = key[-14:-7] if daily else key[-11:-4]
                        if month in wants[sym]:
                            url = f"https://data.binance.vision/{key}"
                            by_month.setdefault(month, []).append(
                                BulkTarget(dataset_id, kind, source_sym, month, url, size, mod)
                            )
                    chosen = sorted(by_month)[-a.limit_months :] if a.limit_months else sorted(by_month)
                    for month in chosen:
                        if parse is None:  # aggTrades: one monthly file, streamed and aggregated
                            done = download_flow_month(
                                by_month[month][0],
                                data_root=root,
                                catalog=catalog,
                                client=client,
                                work_dir=root / "bulk" / ("tmp-aggtrades-pilot" if pilot else "tmp-aggtrades"),
                                heavy_allowed=gate,
                            )
                        else:
                            done = download_month_group(
                                by_month[month],
                                data_root=root,
                                catalog=catalog,
                                client=client,
                                parse=parse,
                                grid_ns=grid,
                                pause_s=a.pause,
                                heavy_allowed=gate,
                            )
                        if done.paused_reason:
                            log(f"PAUSED at {kind} {sym} {month}: {done.paused_reason}; rerun resumes")
                            progress("paused", kind, n, len(targets), sym, done.paused_reason)
                            exit_code = EXIT_PAUSED
                            return exit_code
                        totals["months_downloaded"] += 1 if done.downloaded else 0
                        totals["files"] += done.downloaded
                        totals["already"] += done.already_have
                        totals["rows"] += done.rows if done.downloaded else 0
                        totals["bytes"] += done.zip_bytes
                        totals["gaps"] += done.gaps
                        totals["failed"] += len(done.failed)
                        for url, why in done.failed:
                            log(f"FAILED {url}: {why}")
                        if done.downloaded and kind in held:
                            held[kind].add((source_sym, month))
                    if not pilot and totals["failed"] == failed_before and (by_month or not wants[sym]):
                        # every listed month of this symbol is held now: not listed again for a week, and the
                        # wanted months the source does not publish are recorded (they leave the coverage).
                        # A symbol with no listed month at all is not marked: asking again costs one request, and
                        # an empty answer must not turn every wanted month into "unavailable".
                        checked.mark(
                            kind,
                            sym,
                            wanted=wants[sym],
                            unavailable=unavailable_months(wants[sym], listed=by_month),
                        )
                        checked.save()
                    if processed % 10 == 0 or n == len(targets):
                        current_bulk = bulk_bytes()
                    progress("running", kind, n, len(targets), sym)
                    _s, mem = sample(current_bulk)
                    log(
                        f"[{kind} {n}/{len(targets)}] {source_sym} months={len(chosen)} totals={totals} "
                        f"elapsed={time.time() - started:.0f}s private_mb={mem['private'] // 2**20} "
                        f"avail_mb={mem['available_ram'] // 2**20} extras_gb={current_bulk / 2**30:.2f}"
                    )
                # also when every symbol was skipped: the lake size and the coverage of this kind are current
                current_bulk = bulk_bytes()
                progress("running", kind, len(targets), len(targets), "")
                log(f"[{kind}] finished: coverage={last_coverage} skipped={totals['symbols_skipped']}")
        exit_code = EXIT_FAILED_FILES if totals["failed"] else EXIT_OK
        progress("done" if exit_code == EXIT_OK else "done_with_failures", "", 0, 0, "")
        if full_run and exit_code == EXIT_OK:
            write_json(
                marker,
                {
                    "completed_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                    "universe": str(a.universe),
                    "kinds": a.kinds,
                    "book_depth_symbols": depth_symbols,
                    "totals": totals,
                    "extras_gb": round(current_bulk / 2**30, 2),
                },
            )
            log(f"COMPLETE marker written: {marker}")
        return exit_code
    except (httpx.HTTPError, OSError) as e:
        log(f"ERROR {type(e).__name__}: {e}; rerun resumes from the catalog")
        progress("error", "", 0, 0, "", f"{type(e).__name__}: {e}")
        exit_code = EXIT_ERROR
        return exit_code
    except Exception:
        log(f"CRASH {traceback.format_exc()}")  # pythonw has no console
        raise
    finally:
        catalog.close()
        log(f"END exit={exit_code} totals={totals} elapsed={time.time() - started:.0f}s")
        log.close()
        lock.__exit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(main())
