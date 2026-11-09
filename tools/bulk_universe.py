"""Step 8 preparation: point-in-time top-K universe and exact download size (no data download).

    uv run python tools/bulk_universe.py --k 100

1. Every symbol folder under data/futures/um/monthly/klines/ on data.binance.vision (includes delisted).
2. Monthly quote volume per USDT perpetual from REST /fapi/v1/klines interval=1M (public, weight 2 each;
   it also returns delisted symbols' history, verified 2026-11-09 with LUNAUSDT).
3. PIT top-K union (quant_research.bulk.universe) and exact 1m-kline zip sizes from the S3 listing
   (Size field), both for full history and for member months only (+ warm-up months).
Writes C:/dev/qr-data/bulk/universe-<UTC date>.json and prints a summary.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx

from quant_research.bulk.universe import is_usdt_perpetual, membership_spans, pit_top_k

S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
FAPI = "https://fapi.binance.com"
OUT = Path("C:/dev/qr-data/bulk")


def list_s3(client: httpx.Client, prefix: str, *, delimiter: bool) -> tuple[list[str], list[tuple[str, int]]]:
    """(common prefixes, [(key, size)]) with marker pagination."""
    prefixes: list[str] = []
    keys: list[tuple[str, int]] = []
    marker = ""
    while True:
        params = {"prefix": prefix, "marker": marker}
        if delimiter:
            params["delimiter"] = "/"
        r = client.get(S3, params=params)
        r.raise_for_status()
        prefixes += re.findall(r"<Prefix>([^<]+/)</Prefix>", r.text)[1:] if delimiter else []
        for key, size in re.findall(r"<Key>([^<]+)</Key>.*?<Size>(\d+)</Size>", r.text, flags=re.S):
            keys.append((key, int(size)))
        if re.search(r"<IsTruncated>true</IsTruncated>", r.text) is None:
            return prefixes, keys
        nxt = re.search(r"<NextMarker>([^<]+)</NextMarker>", r.text)
        marker = nxt.group(1) if nxt else (keys[-1][0] if keys else prefixes[-1])
        time.sleep(0.05)


def month_start(ms: int) -> int:
    d = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return int(datetime(d.year, d.month, 1, tzinfo=UTC).timestamp() * 1000)


def ym(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=100)
    ap.add_argument("--warmup-months", type=int, default=3)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with httpx.Client(timeout=30, headers={"User-Agent": "quant-research-bulk-universe"}) as client:
        prefixes, _ = list_s3(client, "data/futures/um/monthly/klines/", delimiter=True)
        symbols = sorted({p.rstrip("/").rsplit("/", 1)[-1] for p in prefixes})
        perps = [s for s in symbols if is_usdt_perpetual(s)]
        volumes: dict[str, dict[int, float]] = {}
        failures: dict[str, str] = {}
        for i, sym in enumerate(perps):
            r = client.get(f"{FAPI}/fapi/v1/klines", params={"symbol": sym, "interval": "1M", "limit": 200})
            if r.status_code == 200:
                volumes[sym] = {month_start(int(row[0])): float(row[7]) for row in r.json()}
            else:
                failures[sym] = f"HTTP {r.status_code}: {r.text[:120]}"
            used = int(r.headers.get("x-mbx-used-weight-1m", "0") or 0)
            time.sleep(1.0 if used > 1500 else 0.12)
            if i % 100 == 0:
                print(f"  volumes {i}/{len(perps)} weight={used}", flush=True)
        # Symbols REST no longer knows (renamed/removed): monthly quote volume from bulk 1d klines, so
        # they still compete for membership in the months they traded (no survivorship bias).
        recovered: dict[str, int] = {}
        for sym in sorted(failures):
            _, keys = list_s3(client, f"data/futures/um/monthly/klines/{sym}/1d/", delimiter=False)
            monthly: dict[int, float] = {}
            for key, _size in keys:
                if not key.endswith(".zip"):
                    continue
                blob = client.get(f"https://data.binance.vision/{key}")
                blob.raise_for_status()
                with zipfile.ZipFile(io.BytesIO(blob.content)) as z:
                    text = z.read(z.namelist()[0]).decode("utf-8")
                for line in text.splitlines():
                    cells = line.split(",")
                    if not cells[0].isdigit():
                        continue  # header row (newer files)
                    m = month_start(int(cells[0]) // (1000 if len(cells[0]) > 13 else 1))
                    monthly[m] = monthly.get(m, 0.0) + float(cells[7])
                time.sleep(0.05)
            if monthly:
                volumes[sym] = monthly
                recovered[sym] = len(monthly)
        current = month_start(int(time.time() * 1000))
        months = sorted({m for v in volumes.values() for m in v if m <= current})
        top = pit_top_k(volumes, k=a.k, months=months)
        spans = membership_spans(top, months=months)
        union = sorted(spans)
        sizes: dict[str, dict[str, int]] = {}
        member_bytes = full_bytes = 0
        for sym in union:
            _, keys = list_s3(client, f"data/futures/um/monthly/klines/{sym}/1m/", delimiter=False)
            files = {k.rsplit("-", 2)[-2] + "-" + k.rsplit("-", 1)[-1][:2]: s for k, s in keys if k.endswith(".zip")}
            sizes[sym] = files
            full_bytes += sum(files.values())
            wanted: set[str] = set()
            for start, end in spans[sym]:
                lo = months.index(start) - a.warmup_months
                hi = months.index(end) if end is not None else len(months)
                wanted |= {ym(m) for m in months[max(0, lo) : hi]}
            member_bytes += sum(s for month, s in files.items() if month in wanted)
    report = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "k": a.k,
        "warmup_months": a.warmup_months,
        "symbols_listed": len(symbols),
        "usdt_perpetuals": len(perps),
        "volume_failures": failures,
        "volume_recovered_from_bulk_1d": recovered,
        "months": [ym(m) for m in months],
        "union_size": len(union),
        "spans": {s: [[ym(f), ym(t) if t else None] for f, t in v] for s, v in spans.items()},
        "kline_1m_zip_bytes_full_history": full_bytes,
        "kline_1m_zip_bytes_member_months": member_bytes,
        "per_symbol_month_sizes": sizes,
        "elapsed_s": round(time.time() - started, 1),
    }
    path = OUT / f"universe-{datetime.now(UTC).strftime('%Y%m%d')}.json"
    path.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(
        f"{path}\nlisted={len(symbols)} usdt_perps={len(perps)} failures={len(failures)} months={len(months)} "
        f"union(k={a.k})={len(union)} 1m-zip full={full_bytes / 1e9:.2f} GB member+warmup={member_bytes / 1e9:.2f} GB "
        f"elapsed={report['elapsed_s']} s"
    )


if __name__ == "__main__":
    main()
