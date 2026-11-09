"""Plan step 8 done-criterion: random bulk 1m-kline samples must equal the REST API.

    uv run python tools/validate_bulk_klines.py [--samples 40] [--window 500] [--seed 20261109]

Draws `--samples` downloaded bulk files (all four kinds) of symbols still trading, picks a random
`--window`-minute range inside each month, fetches the same range from REST (klines, markPriceKlines,
indexPriceKlines, premiumIndexKlines) and compares open/high/low/close (+ volume for klines). Writes the
report to <data_root>/bulk/rest-compare-<utc>.json. Exit 0 = every sample matched, 1 = any mismatch.
Public endpoints only; ~1 request per second (weight 2-5 each).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx

FAPI = "https://fapi.binance.com"
ENDPOINT = {
    "binance.um.bulk.klines_1m": ("/fapi/v1/klines", "symbol"),
    "binance.um.bulk.mark_klines_1m": ("/fapi/v1/markPriceKlines", "symbol"),
    "binance.um.bulk.index_klines_1m": ("/fapi/v1/indexPriceKlines", "pair"),
    "binance.um.bulk.premium_klines_1m": ("/fapi/v1/premiumIndexKlines", "symbol"),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=40)
    ap.add_argument("--window", type=int, default=500)
    ap.add_argument("--seed", type=int, default=20261109)
    a = ap.parse_args()

    import polars as pl

    from quant_research.bulk.compare import compare_bars
    from quant_research.catalog.store import Catalog
    from quant_research.config import load_settings
    from quant_research.data.lake import read_lake_file

    root = load_settings().data_root
    catalog = Catalog.open(root / "meta" / "catalog.db")
    try:
        rows = catalog.conn.execute(
            "select b.dataset_id, b.symbol, b.month, b.lake_path from bulk_file b join instrument i "
            "on i.venue = 'binance' and i.venue_symbol = b.symbol and i.market_type = 'usdm_perp' "
            "where b.dataset_id in (?,?,?,?) and i.onboard_ns is not null and i.delist_ns is null "
            "order by b.dataset_id, b.symbol, b.month",
            tuple(ENDPOINT),
        ).fetchall()
    finally:
        catalog.close()

    def draw(*parts: object) -> int:
        """Deterministic pseudo-random integer from the seed (reproducible sample, no RNG state)."""
        text = ":".join(str(p) for p in (a.seed, *parts))
        return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")

    picked = sorted(rows, key=lambda row: draw(*row))[: a.samples]
    results: list[dict[str, object]] = []
    with httpx.Client(timeout=20, headers={"User-Agent": "quant-research-validate"}) as client:
        for dataset_id, symbol, month, lake_path in picked:
            whole = read_lake_file(Path(str(lake_path)))
            # window inside the bars the file really has (a month can start before the listing)
            start_ns, end_ns = int(cast(int, whole["event_ns"].min())), int(cast(int, whole["event_ns"].max()))
            minutes = (end_ns - start_ns) // 60_000_000_000 + 1
            first = start_ns + draw(lake_path, "offset") % max(1, minutes - a.window) * 60_000_000_000
            last = first + (a.window - 1) * 60_000_000_000
            frame = whole.filter(pl.col("event_ns").is_between(first, last))
            path, key = ENDPOINT[str(dataset_id)]
            r = client.get(
                FAPI + path,
                params={
                    key: str(symbol),
                    "interval": "1m",
                    "startTime": first // 1_000_000,
                    "endTime": last // 1_000_000,
                    "limit": a.window,
                },
            )
            r.raise_for_status()
            rest = cast(list[list[object]], r.json())
            c = compare_bars(frame.to_dicts(), rest, with_volume=dataset_id == "binance.um.bulk.klines_1m")
            results.append(
                {
                    "dataset": dataset_id,
                    "symbol": symbol,
                    "month": month,
                    "from_utc": datetime.fromtimestamp(first / 1e9, UTC).isoformat(timespec="minutes"),
                    "compared": c.compared,
                    "only_bulk": c.only_bulk,
                    "only_rest": c.only_rest,
                    "mismatches": c.mismatches[:5],
                    "ok": c.ok,
                }
            )
            time.sleep(1.0)
    report = {
        "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "seed": a.seed,
        "samples": len(results),
        "ok": sum(bool(x["ok"]) for x in results),
        "bars_compared": sum(cast(int, x["compared"]) for x in results),
        "results": results,
    }
    out = root / "bulk" / f"rest-compare-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}), out)  # CLI output
    for x in results:
        if not x["ok"]:
            print("NOT OK", json.dumps(x))  # CLI output
    return 0 if report["ok"] == report["samples"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
