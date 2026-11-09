"""Step 8 runner: download PIT-universe 1m klines (4 kinds) into the lake. Resumable and idempotent.

    uv run python tools/bulk_download.py --universe C:/dev/qr-data/bulk/universe-20261109.json \
        [--symbols BTCUSDT] [--kinds klines] [--limit-months 3] [--pause 0.2]

Months = membership spans (+ warm-up) from the universe report. Each file's LastModified comes from the
S3 listing so regenerated upstream files are fetched as new versions. Heavy-work thermal gate is read
from <data_root>/run/ssd_state.json before every file (ADR-012).
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import httpx

from quant_research.bulk.download import BulkTarget, download_targets
from quant_research.capture.thermal import heavy_work_allowed, read_state
from quant_research.catalog.bootstrap import bootstrap
from quant_research.catalog.store import Catalog
from quant_research.config import load_settings

S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
KINDS = {
    "klines": "binance.um.bulk.klines_1m",
    "markPriceKlines": "binance.um.bulk.mark_klines_1m",
    "indexPriceKlines": "binance.um.bulk.index_klines_1m",
    "premiumIndexKlines": "binance.um.bulk.premium_klines_1m",
}


def listing(client: httpx.Client, prefix: str) -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    marker = ""
    while True:
        r = client.get(S3, params={"prefix": prefix, "marker": marker})
        r.raise_for_status()
        for block in re.findall(r"<Contents>(.*?)</Contents>", r.text, flags=re.S):
            key = re.search(r"<Key>([^<]+)</Key>", block)
            size = re.search(r"<Size>(\d+)</Size>", block)
            mod = re.search(r"<LastModified>([^<]+)</LastModified>", block)
            if key and size and mod and key.group(1).endswith(".zip"):
                out.append((key.group(1), int(size.group(1)), mod.group(1)))
        if "<IsTruncated>true</IsTruncated>" not in r.text:
            return out
        marker = out[-1][0]


def wanted_months(spans: list[list[str | None]], months: list[str], warmup: int) -> set[str]:
    wanted: set[str] = set()
    for start, end in spans:
        lo = months.index(str(start)) - warmup
        hi = months.index(end) if end is not None else len(months)
        wanted |= set(months[max(0, lo) : hi])
    return wanted


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", type=Path, required=True)
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--kinds", nargs="*", default=list(KINDS))
    ap.add_argument("--limit-months", type=int, default=None, help="pilot: newest N wanted months per symbol")
    ap.add_argument("--pause", type=float, default=0.2)
    a = ap.parse_args()
    settings = load_settings()
    report = json.loads(a.universe.read_text(encoding="utf-8"))
    months: list[str] = report["months"]
    spans: dict[str, list[list[str | None]]] = report["spans"]
    symbols = a.symbols or sorted(spans)
    catalog = Catalog.open(settings.data_root / "meta" / "catalog.db")
    bootstrap(catalog)
    state_path = settings.data_root / "run" / "ssd_state.json"

    def gate() -> tuple[bool, str]:
        return heavy_work_allowed(read_state(state_path), now_ns=time.time_ns())

    totals = {"downloaded": 0, "already": 0, "rows": 0, "bytes": 0, "gaps": 0, "failed": 0}
    started = time.time()
    try:
        with httpx.Client(timeout=60, headers={"User-Agent": "quant-research-bulk"}) as client:
            for n, sym in enumerate(symbols, 1):
                want = wanted_months(spans[sym], months, report.get("warmup_months", 3))
                for kind in a.kinds:
                    files = [
                        f for f in listing(client, f"data/futures/um/monthly/{kind}/{sym}/1m/") if f[0][-11:-4] in want
                    ]
                    if a.limit_months:
                        files = sorted(files)[-a.limit_months :]
                    targets = [
                        BulkTarget(KINDS[kind], kind, sym, key[-11:-4], f"https://data.binance.vision/{key}", size, mod)
                        for key, size, mod in files
                    ]
                    done = download_targets(
                        targets,
                        data_root=settings.data_root,
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
                        print(f"FAILED {url}: {why}", flush=True)
                    if done.paused_reason:
                        print(f"PAUSED (thermal): {done.paused_reason}; rerun later to resume", flush=True)
                        return
                print(f"[{n}/{len(symbols)}] {sym} totals={totals} elapsed={time.time() - started:.0f}s", flush=True)
    finally:
        catalog.close()
        print(f"DONE totals={totals} elapsed={time.time() - started:.0f}s", flush=True)


if __name__ == "__main__":
    main()
