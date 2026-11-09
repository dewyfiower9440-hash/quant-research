"""Plan step 11: 7-day continuous-operation validation report (interim until the window reaches 7 days).

    uv run python tools/validate_week.py [--out docs/measurements/validate-week.md]

Window: T0 start (ADR-011) .. now. Per live source, coverage = 1 - (recorded gap time) / window, where
- bookTicker and Upbit orderbook: their receive gaps + rest-process restarts,
- forceOrder (event-driven, no cadence): ws restarts + WS reconnects + host-wide silences (time missing in
  BOTH 1 Hz sources, e.g. standby), which is the plan's "liquidations vs heartbeat gaps" cross-check.
OI / ratio datasets: rows, keys and the share of keys whose value changed in a later 30-day refetch
(bitemporal versions). Bulk klines vs REST: the newest tools/validate_bulk_klines.py reports.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

LIVE = {
    "binance.um.book_ticker_1s": ("binance.um.book_ticker_1s", "process.rest"),
    "upbit.orderbook_l1_1s": ("upbit.orderbook_l1_1s", "process.rest"),
}
METRICS = (
    "binance.um.oi_hist",
    "binance.um.top_ls_account",
    "binance.um.top_ls_position",
    "binance.um.global_ls_account",
    "binance.um.taker_ls",
    "binance.um.basis",
)
DAY_NS = 86_400 * 10**9


def utc(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, UTC).strftime("%Y-%m-%d %H:%MZ")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("docs/measurements/validate-week.md"))
    a = ap.parse_args()

    import polars as pl

    from quant_research.catalog.bootstrap import T0_START_NS
    from quant_research.catalog.store import Catalog
    from quant_research.config import load_settings
    from quant_research.data.lake import load_dataset
    from quant_research.monitor.validate import covered_fraction, intersect, merge_spans
    from quant_research.normalize.rows import VERSION_KEYS

    root = load_settings().data_root
    lo, hi = T0_START_NS, time.time_ns()
    days = (hi - lo) / DAY_NS
    catalog = Catalog.open(root / "meta" / "catalog.db")
    try:
        window_gaps = catalog.conn.execute(
            "select source, start_ns, end_ns, detail from gap where end_ns > ?", (lo,)
        ).fetchall()

        def spans(*sources: str, detail: str | None = None) -> list[tuple[int | None, int]]:
            return [(r[1], int(r[2])) for r in window_gaps if r[0] in sources and (detail is None or r[3] == detail)]

        causes = catalog.conn.execute(
            "select source, cause, coalesce(detail, ''), count(*), sum(end_ns - coalesce(start_ns, end_ns)) "
            "from gap where end_ns > ? and source not like 'binance.um.bulk.%' and source not like 'binance.spot.%' "
            "and source <> 'upbit.candles_1m' group by 1, 2, 3 order by 1, 2",
            (lo,),
        ).fetchall()
        live = {name: merge_spans(spans(*src), lo, hi) for name, src in LIVE.items()}
        receive = [merge_spans(spans(s, detail="receive_gap"), lo, hi) for s in LIVE]
        host = intersect(receive[0], receive[1])
        force = merge_spans([*spans("binance.um.force_order", "process.ws"), *host], lo, hi)
        live["binance.um.force_order"] = force
        incidents = catalog.conn.execute(
            "select key, severity, opened_ns, closed_ns from incident order by opened_ns"
        ).fetchall()
    finally:
        catalog.close()

    lines = [
        "# 7일 연속 운영 검증 (plan step 11)",
        "",
        f"- 생성: {utc(hi)} (`tools/validate_week.py`)",
        f"- 창: {utc(lo)} ~ {utc(hi)} = **{days:.2f}일**. "
        + ("**최종 판정 가능(7일 이상)**" if days >= 7 else "**중간 보고(7일 미만, 2026-11-15 이후 최종)**"),
        "",
        "## 1. 실시간 소스 커버리지 (기록된 gap 기준)",
        "| 소스 | 커버리지 | 결측 시간(분) | 결측 구간 수 | 가장 긴 구간 |",
        "|---|---|---|---|---|",
    ]
    for name, missing in live.items():
        longest = max(missing, key=lambda s: s[1] - s[0], default=None)
        lines.append(
            f"| `{name}` | {covered_fraction(lo, hi, missing):.4%} | {sum(e - s for s, e in missing) / 6e10:.1f} | "
            f"{len(missing)} | "
            + ("-" if longest is None else f"{utc(longest[0])} {(longest[1] - longest[0]) / 6e10:.1f}분")
            + " |"
        )
    lines += [
        "",
        f"- 양쪽 1초 소스에 함께 있는 공백(호스트 전체, 절전 등): {len(host)}건, "
        f"{sum(e - s for s, e in host) / 6e10:.1f}분. 강제청산 WS 결측에 포함했습니다.",
        "",
        "## 2. gap 원인별 (창 안, 벌크·백필 제외)",
        "| source | cause | detail | 건수 | 합계(분) |",
        "|---|---|---|---|---|",
        *[f"| `{s}` | {c} | {d or '-'} | {n} | {(t or 0) / 6e10:.1f} |" for s, c, d, n, t in causes],
        "",
        "## 3. OI·비율 30일 재수집 대조 (바이템포럴 버전)",
        "| 데이터셋 | 행(버전 포함) | 키 | 값이 바뀐 키 | 수정률 |",
        "|---|---|---|---|---|",
    ]
    for dataset_id in METRICS:
        frame = load_dataset(root, dataset_id)
        if frame.height == 0:
            lines.append(f"| `{dataset_id}` | 0 | 0 | 0 | - |")
            continue
        per_key = frame.group_by(list(VERSION_KEYS[dataset_id])).len()
        revised = per_key.filter(pl.col("len") > 1).height
        lines.append(
            f"| `{dataset_id}` | {frame.height:,} | {per_key.height:,} | {revised:,} | {revised / per_key.height:.3%} |"
        )
    reports = sorted((root / "bulk").glob("rest-compare-*.json"))
    lines += ["", "## 4. 벌크 1분봉 vs REST (`tools/validate_bulk_klines.py`)"]
    for path in reports:
        body = json.loads(path.read_text(encoding="utf-8"))
        lines.append(
            f"- {path.name}: 표본 {body['ok']}/{body['samples']} 일치, "
            f"비교 봉 {body['bars_compared']:,} (seed {body['seed']})"
        )
    lines += ["", "## 5. 모니터 incident", "| key | 심각도 | 열림 | 닫힘 |", "|---|---|---|---|"]
    lines += [f"| `{k}` | {s} | {utc(o)} | {'-' if c is None else utc(c)} |" for k, s, o, c in incidents]
    a.out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {a.out} window_days={days:.2f}")  # CLI output
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
