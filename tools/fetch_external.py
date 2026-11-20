"""v2.2 B4 tier T7: fetch the key-less external sources into the lake (docs/measurements/b4-t7-sources.md).

    uv run python tools/fetch_external.py [--sources fng coinmetrics dvol] [--assets btc eth] [--start 2020-01-01]

Idempotent: a month is written again only when its content changed (bulk/external.py `write_monthly`).
Attribution (Fear & Greed: Alternative.me; Coin Metrics Community data, CC BY-NC 4.0) goes with any shared result.
Exit codes: 0 done, 1 a source failed (the others still ran)."""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime

SOURCES = ("fng", "coinmetrics", "dvol")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", nargs="*", default=list(SOURCES), choices=SOURCES)
    ap.add_argument("--assets", nargs="*", default=["btc", "eth"])
    ap.add_argument("--start", default="2020-01-01")
    a = ap.parse_args()
    import httpx

    from quant_research.bulk.external import (
        COINMETRICS_DATASET,
        COINMETRICS_URL,
        DVOL_DATASET,
        DVOL_URL,
        FNG_DATASET,
        FNG_URL,
        ExternalSourceError,
        fetch_coinmetrics,
        fetch_dvol,
        fetch_fng,
        write_monthly,
    )
    from quant_research.catalog.bootstrap import bootstrap
    from quant_research.catalog.store import Catalog
    from quant_research.config import load_settings

    root = load_settings().data_root
    catalog = Catalog.open(root / "meta" / "catalog.db")
    report: dict[str, object] = {}
    failed = False
    try:
        bootstrap(catalog)  # registers the datasets, their conservative lags and the epoch-2 seal
        start_ms = int(datetime.fromisoformat(a.start).replace(tzinfo=UTC).timestamp()) * 1000
        with httpx.Client(timeout=30, headers={"User-Agent": "quant-research-external"}) as client:
            for source in a.sources:
                t0 = time.monotonic()
                try:
                    written = 0
                    if source == "fng":
                        frame = fetch_fng(client)
                        written = write_monthly(
                            frame,
                            data_root=root,
                            catalog=catalog,
                            dataset_id=FNG_DATASET,
                            symbol="MARKET",
                            source_url=FNG_URL,
                        )
                        rows = frame.height
                    elif source == "coinmetrics":
                        frame = fetch_coinmetrics(client, a.assets, start=a.start)
                        rows = frame.height
                        for (symbol,), part in frame.group_by("symbol"):
                            written += write_monthly(
                                part,
                                data_root=root,
                                catalog=catalog,
                                dataset_id=COINMETRICS_DATASET,
                                symbol=str(symbol),
                                source_url=COINMETRICS_URL,
                            )
                    else:
                        rows = 0
                        for asset in a.assets:
                            frame = fetch_dvol(client, asset, start_ms=start_ms, end_ms=int(time.time() * 1000))
                            rows += frame.height
                            written += write_monthly(
                                frame,
                                data_root=root,
                                catalog=catalog,
                                dataset_id=DVOL_DATASET,
                                symbol=asset.upper(),
                                source_url=DVOL_URL,
                            )
                    report[source] = {"rows": rows, "months_written": written, "s": round(time.monotonic() - t0, 1)}
                except (httpx.HTTPError, ExternalSourceError) as e:
                    failed = True
                    report[source] = {"error": f"{type(e).__name__}: {e}"}
    finally:
        catalog.close()
    print(json.dumps(report, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
