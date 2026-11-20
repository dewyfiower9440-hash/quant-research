"""v2.2 B3: build (or reuse) the cached panel shards of one research window and measure the universe panel.

    uv run python tools/build_panels.py --window w2 --timeframe 1h --top-m 100 --out docs/measurements/b3-w2-1h.md

Research role, epoch 2 windows (ADR-040): w1 = [2020-01, 2023-01), w2 = [2024-12, 2026-07). Every shard goes through
`load_panel` (seal and point-in-time rules). Shards are cached under qr-data/cache/panels; a rerun reuses them,
so a run stopped by the research gate (memory, disk, SSD heat) resumes where it stopped (exit code 3)."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

WINDOWS = {"w1": ((2020, 1), (2023, 1)), "w2": ((2024, 12), (2026, 7))}
TIMEFRAMES = {"1h": 3_600, "4h": 4 * 3_600, "1d": 86_400}
MB = 1024 * 1024


def month_ns(y: int, m: int) -> int:
    return int(datetime(y, m, 1, tzinfo=UTC).timestamp()) * 10**9


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", choices=sorted(WINDOWS), required=True)
    ap.add_argument("--timeframe", choices=sorted(TIMEFRAMES), default="1h")
    ap.add_argument("--top-m", type=int, default=100, help="0 = the rule's members as they are")
    ap.add_argument("--limit", type=int, default=0, help="only the first N member symbols (pilot)")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    from quant_research.adapters.jobhost import memory_sample
    from quant_research.backtest.panel import PanelConfig
    from quant_research.backtest.panel_store import PanelStore, Window, load_universe_panel, window_symbols
    from quant_research.catalog.store import Catalog
    from quant_research.catalog.universe_load import PIT_RULE
    from quant_research.config import load_settings
    from quant_research.data.access import PointInTime
    from quant_research.research.guard import research_gate

    root = load_settings().data_root
    (y0, m0), (y1, m1) = WINDOWS[a.window]
    window = Window(month_ns(y0, m0), month_ns(y1, m1))
    config = PanelConfig(TIMEFRAMES[a.timeframe] * 10**9)
    store = PanelStore(root / "cache" / "panels")
    gate = research_gate(root)
    catalog = Catalog.open(root / "meta" / "catalog.db")
    status = "complete"
    try:
        pit = PointInTime(catalog)
        symbols = window_symbols(pit, PIT_RULE, window)
        if a.limit:
            symbols = symbols[: a.limit]
        t0 = time.monotonic()
        built = reused = 0
        missing: list[str] = []
        peak = memory_sample()["private"]
        slowest = (0.0, "")
        for i, symbol in enumerate(symbols):
            ok, why = gate()
            if not ok:
                status = f"stopped by the research gate before {symbol} ({i}/{len(symbols)}): {why}"
                break
            s0 = time.monotonic()
            try:
                _meta, fresh = store.shard(pit, root, symbol, window, config, rule=PIT_RULE)
            except FileNotFoundError:
                missing.append(symbol)
                continue
            built, reused = built + fresh, reused + (not fresh)
            slowest = max(slowest, (time.monotonic() - s0, symbol))
            peak = max(peak, memory_sample()["private"])
            if (i + 1) % 25 == 0:
                print(json.dumps({"done": i + 1, "of": len(symbols), "elapsed_s": round(time.monotonic() - t0, 1)}))
                sys.stdout.flush()
        build_s = time.monotonic() - t0
        report: dict[str, object] = {
            "window": a.window,
            "timeframe": a.timeframe,
            "top_m": a.top_m or None,
            "symbols": len(symbols),
            "built": built,
            "reused": reused,
            "missing_klines": missing,
            "build_s": round(build_s, 1),
            "slowest_shard": [round(slowest[0], 1), slowest[1]],
            "peak_private_mb_build": round(peak / MB),
            "status": status,
        }
        if status == "complete":
            t1 = time.monotonic()
            universe = load_universe_panel(
                pit, root, store, window, config, top_m=a.top_m or None, rule=PIT_RULE, symbols=symbols
            )
            frame = universe.panel.frame
            report |= {
                "load_s": round(time.monotonic() - t1, 1),
                "peak_private_mb_load": round(memory_sample()["private"] / MB),
                "panel_rows": frame.height,
                "panel_symbols": frame["symbol"].n_unique(),
                "panel_decisions": frame["decision_ns"].n_unique(),
                "member_rows": int(frame["member"].sum()),
                "panel_mb_in_memory": round(frame.estimated_size() / MB),
                "lake_files": len(universe.panel.files),
            }
        cache_bytes = sum(p.stat().st_size for p in store.root.glob("*.parquet")) if store.root.exists() else 0
        report["cache_mb_total"] = round(cache_bytes / MB)
    finally:
        catalog.close()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# B3 패널 저장소 실측: {a.window} {a.timeframe} top-{a.top_m or 'all'} (tools/build_panels.py)",
        "",
        "> 연구 역할, 봉인 epoch 2 창 안. 측정값이며 성과 주장이 아니다.",
        "",
        "| 항목 | 값 |",
        "|---|---|",
        *(f"| {k} | {json.dumps(v, ensure_ascii=False)} |" for k, v in report.items()),
        "",
    ]
    a.out.write_bytes("\n".join(lines).encode("utf-8"))
    print(json.dumps(report, ensure_ascii=False))
    return 0 if status == "complete" else 3


if __name__ == "__main__":
    raise SystemExit(main())
