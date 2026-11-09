"""Step 8 runner: download PIT-universe 1m klines (4 kinds) into the lake. Resumable and idempotent.

    uv run python tools/bulk_download.py --universe C:/dev/qr-data/bulk/universe-20261109.json \
        [--symbols BTCUSDT] [--kinds klines] [--limit-months 3] [--pause 0.2] [--log-file PATH]

Months = membership spans (+ warm-up) from the universe report. Each file's LastModified comes from the
S3 listing so regenerated upstream files are fetched as new versions; files already in the catalog
(`bulk_file`) are skipped, so a rerun resumes where the last one stopped.

Safety (2026-11-09 decision on the resumed run):
- one file at a time (no concurrency), Polars limited to POLARS_MAX_THREADS (default 2);
- single instance (`run/bulk.lock`); a second start exits 2;
- before every file: SSD thermal state (ADR-012) + free disk, bulk lake cap, available RAM and this
  process's private memory (`quant_research.bulk.guard`); a blocked gate stops the run with exit 3 and
  a rerun resumes;
- progress in `run/bulk.progress.json`, timestamped log lines in --log-file;
- a full run without failures writes `bulk/complete-<universe>.json`; later starts exit 0 at once.
Exit codes: 0 complete (or already complete), 1 error, 2 already running, 3 paused by a gate,
4 finished with failed files (retried on the next start).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

import httpx

KINDS = {
    "klines": "binance.um.bulk.klines_1m",
    "markPriceKlines": "binance.um.bulk.mark_klines_1m",
    "indexPriceKlines": "binance.um.bulk.index_klines_1m",
    "premiumIndexKlines": "binance.um.bulk.premium_klines_1m",
}
EXIT_OK, EXIT_ERROR, EXIT_RUNNING, EXIT_PAUSED, EXIT_FAILED_FILES = 0, 1, 2, 3, 4


def dir_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*.parquet")) if root.exists() else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", type=Path, required=True)
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--kinds", nargs="*", default=list(KINDS))
    ap.add_argument("--limit-months", type=int, default=None, help="pilot: newest N wanted months per symbol")
    ap.add_argument("--pause", type=float, default=0.2)
    ap.add_argument("--log-file", type=Path, default=None)
    ap.add_argument("--polars-threads", type=int, default=2)
    a = ap.parse_args()
    # Must be set before Polars is imported (it sizes its thread pool on import).
    os.environ.setdefault("POLARS_MAX_THREADS", str(a.polars_threads))

    from quant_research.adapters.jobhost import Log, memory_sample, write_json
    from quant_research.bulk.download import BulkTarget, download_targets
    from quant_research.bulk.guard import Limits, Sample, combine, resource_gate
    from quant_research.bulk.s3 import listing, wanted_months
    from quant_research.capture.lock import AlreadyRunningError, InstanceLock
    from quant_research.capture.thermal import heavy_work_allowed, read_state
    from quant_research.catalog.bootstrap import bootstrap
    from quant_research.catalog.store import Catalog
    from quant_research.config import load_settings

    log = Log(a.log_file)
    settings = load_settings()
    root = settings.data_root
    full_run = a.symbols is None and a.limit_months is None and sorted(a.kinds) == sorted(KINDS)
    marker = root / "bulk" / f"complete-{a.universe.stem}.json"
    progress_path = root / "run" / "bulk.progress.json"
    if full_run and marker.exists():
        log(f"ALREADY COMPLETE ({marker}); nothing to do")
        log.close()
        return EXIT_OK
    try:
        lock = InstanceLock(root / "run" / "bulk.lock").__enter__()
    except AlreadyRunningError as e:
        log(f"REFUSED: {e}")
        log.close()
        return EXIT_RUNNING

    report = json.loads(a.universe.read_text(encoding="utf-8"))
    months: list[str] = report["months"]
    spans: dict[str, list[list[str | None]]] = report["spans"]
    sizes: dict[str, dict[str, int]] = report["per_symbol_month_sizes"]
    symbols: list[str] = a.symbols or sorted(spans)
    warmup = int(report.get("warmup_months", 3))
    wants = {s: wanted_months(spans[s], months, warmup) for s in symbols}
    # Progress weight: 1m kline zip bytes of the wanted months (the other kinds scale with it).
    weight = {s: sum(sizes.get(s, {}).get(m, 0) for m in wants[s]) or 1 for s in symbols}
    total_weight = sum(weight.values())
    limits = Limits()
    lake_dirs = [root / "lake" / d for d in KINDS.values()]
    bulk_bytes = sum(dir_bytes(d) for d in lake_dirs)
    state_path = root / "run" / "ssd_state.json"

    def sample() -> tuple[Sample, dict[str, int]]:
        mem = memory_sample()
        free = shutil.disk_usage(root).free
        return Sample(free, bulk_bytes, mem["available_ram"], mem["private"]), mem

    def gate() -> tuple[bool, str]:
        s, _mem = sample()
        return combine(heavy_work_allowed(read_state(state_path), now_ns=time.time_ns()), resource_gate(s, limits))

    totals = {"downloaded": 0, "already": 0, "rows": 0, "bytes": 0, "gaps": 0, "failed": 0}
    started = time.time()
    done_weight = 0
    run_weight = 0  # weight of symbols that needed downloads in this run (ETA basis)
    run_seconds = 0.0
    exit_code = EXIT_ERROR  # anything that escapes below is reported as an error
    catalog = Catalog.open(root / "meta" / "catalog.db")
    bootstrap(catalog)

    def progress(state: str, n: int, sym: str, note: str = "") -> None:
        frac = done_weight / total_weight
        rate = run_weight / run_seconds if run_seconds > 0 else 0.0
        eta = (total_weight - done_weight) / rate if rate > 0 else None
        s, mem = sample()
        write_json(
            progress_path,
            {
                "state": state,
                "updated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                "pid": os.getpid(),
                "symbol_index": n,
                "symbols": len(symbols),
                "symbol": sym,
                "weighted_progress": round(frac, 4),
                "eta_s": None if eta is None else int(eta),
                "elapsed_s": int(time.time() - started),
                "totals": totals,
                "memory_mb": {k: v // 2**20 for k, v in mem.items()},
                "free_disk_gb": round(s.free_disk / 2**30, 1),
                "bulk_gb": round(bulk_bytes / 2**30, 2),
                "note": note,
            },
        )

    try:
        s0, mem0 = sample()
        log(
            f"START pid={os.getpid()} symbols={len(symbols)} kinds={a.kinds} full_run={full_run} "
            f"POLARS_MAX_THREADS={os.environ['POLARS_MAX_THREADS']} free_disk_gb={s0.free_disk / 2**30:.1f} "
            f"bulk_gb={bulk_bytes / 2**30:.2f} available_ram_mb={mem0['available_ram'] // 2**20} limits={limits}"
        )
        with httpx.Client(timeout=60, headers={"User-Agent": "quant-research-bulk"}) as client:
            for n, sym in enumerate(symbols, 1):
                t_sym = time.time()
                before = totals["downloaded"]
                for kind in a.kinds:
                    files = [
                        f
                        for f in listing(client, f"data/futures/um/monthly/{kind}/{sym}/1m/")
                        if f[0][-11:-4] in wants[sym]
                    ]
                    if a.limit_months:
                        files = sorted(files)[-a.limit_months :]
                    targets = [
                        BulkTarget(KINDS[kind], kind, sym, key[-11:-4], f"https://data.binance.vision/{key}", size, mod)
                        for key, size, mod in files
                    ]
                    done = download_targets(
                        targets,
                        data_root=root,
                        catalog=catalog,
                        client=client,
                        pause_s=a.pause,
                        heavy_allowed=gate,
                    )
                    totals["downloaded"] += done.downloaded
                    totals["already"] += done.already_have
                    totals["rows"] += done.rows
                    totals["bytes"] += done.zip_bytes
                    totals["gaps"] += done.gaps
                    totals["failed"] += len(done.failed)
                    for url, why in done.failed:
                        log(f"FAILED {url}: {why}")
                    if done.paused_reason:
                        log(f"PAUSED at {sym} {kind}: {done.paused_reason}; rerun resumes")
                        progress("paused", n, sym, done.paused_reason)
                        exit_code = EXIT_PAUSED
                        return exit_code
                done_weight += weight[sym]
                if totals["downloaded"] > before:
                    run_weight += weight[sym]
                    run_seconds += time.time() - t_sym
                bulk_bytes = sum(dir_bytes(d) for d in lake_dirs)
                progress("running", n, sym)
                _s, mem = sample()
                log(
                    f"[{n}/{len(symbols)}] {sym} totals={totals} elapsed={time.time() - started:.0f}s "
                    f"private_mb={mem['private'] // 2**20} peak_ws_mb={mem['peak_working_set'] // 2**20} "
                    f"avail_mb={mem['available_ram'] // 2**20} bulk_gb={bulk_bytes / 2**30:.2f}"
                )
        exit_code = EXIT_FAILED_FILES if totals["failed"] else EXIT_OK
        progress("done" if exit_code == EXIT_OK else "done_with_failures", len(symbols), "")
        if full_run and exit_code == EXIT_OK:
            count = catalog.conn.execute("select count(*) from bulk_file").fetchone()[0]
            write_json(
                marker,
                {
                    "completed_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                    "universe": str(a.universe),
                    "symbols": len(symbols),
                    "kinds": a.kinds,
                    "catalog_bulk_files": count,
                    "bulk_gb": round(bulk_bytes / 2**30, 2),
                },
            )
            log(f"COMPLETE marker written: {marker}")
        return exit_code
    except (httpx.HTTPError, OSError) as e:
        log(f"ERROR {type(e).__name__}: {e}; rerun resumes from the catalog")
        progress("error", 0, "", f"{type(e).__name__}: {e}")
        exit_code = EXIT_ERROR
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
