"""Phase 2 step 9: real-data smoke run of the five templates on both engines (research split only).

    uv run python tools/run_backtest.py [--symbols BTCUSDT ETHUSDT ...] [--from 2025-01] [--to 2025-06]
        [--n 5] [--stress 1 2 3] [--out docs/measurements/phase2-smoke.md]

Every evaluation is a trial in <data_root>/meta/research.db (stress runs too, §8). The report states the
engine cross-check, trial accounting (evaluations attempted = trials = backtest runs + refusals) and the
flags of what is not modelled. It makes NO performance claim: templates are fixed, not searched, and the
period is short. Safety: single research process (run/research.lock), resource + thermal gate before
every evaluation, POLARS_MAX_THREADS=2.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

ENV_ID = "ENV-2026.11.09"  # docs/environment.md
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT"]


def month_start_ns(month: str) -> int:
    y, m = (int(x) for x in month.split("-"))
    return int(datetime(y, m, 1, tzinfo=UTC).timestamp()) * 10**9


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=DEFAULT_SYMBOLS)
    ap.add_argument("--from", dest="start", default="2025-01")
    ap.add_argument("--to", dest="end", default="2025-06", help="last month (inclusive)")
    ap.add_argument("--n", type=int, default=5, help="universe size of the templates for this run")
    ap.add_argument("--stress", nargs="*", type=float, default=[1.0, 2.0, 3.0])
    ap.add_argument("--out", type=Path, default=Path("docs/measurements/phase2-smoke.md"))
    a = ap.parse_args()
    os.environ.setdefault("POLARS_MAX_THREADS", "2")

    import numpy as np

    from quant_research.adapters.jobhost import memory_sample
    from quant_research.backtest.costs import CostModel
    from quant_research.backtest.panel import PanelConfig, load_panel
    from quant_research.backtest.run import RunConfig, run_backtest
    from quant_research.bulk.guard import GB, Limits, Sample, combine, resource_gate
    from quant_research.capture.lock import InstanceLock
    from quant_research.capture.run import git_sha, repo_root
    from quant_research.capture.thermal import heavy_work_allowed, read_state
    from quant_research.catalog.store import Catalog
    from quant_research.config import load_settings
    from quant_research.data.access import PointInTime
    from quant_research.research.ledger import TrialLedger
    from quant_research.research.store import ResearchStore
    from quant_research.strategy.ops import FUNDING, KLINES, MARK
    from quant_research.strategy.spec import StrategySpec
    from quant_research.strategy.templates import templates

    root = load_settings().data_root
    limits = Limits(max_process_mem=2 * GB)  # plan §8: one research worker <= 2 GB

    def gate() -> tuple[bool, str]:
        mem = memory_sample()
        sample = Sample(shutil.disk_usage(root).free, 0, mem["available_ram"], mem["private"])
        return combine(
            heavy_work_allowed(read_state(root / "run" / "ssd_state.json"), now_ns=time.time_ns()),
            resource_gate(sample, limits),
        )

    with InstanceLock(root / "run" / "research.lock"):
        catalog = Catalog.open(root / "meta" / "catalog.db")
        store = ResearchStore.open(root / "meta" / "research.db")
        ledger = TrialLedger(store, env_id=ENV_ID, code_sha=git_sha(repo_root()))
        abandoned = ledger.close_abandoned()
        before = int(store.conn.execute("select count(*) from trial").fetchone()[0])
        first_new = int(store.conn.execute("select coalesce(max(trial_id), 0) from trial").fetchone()[0]) + 1
        t0 = time.monotonic()
        ok, why = gate()
        if not ok:
            print(f"PAUSED before loading: {why}")  # CLI output
            return 3
        hour = 3_600 * 10**9
        start = month_start_ns(a.start)
        y, m = (int(x) for x in a.end.split("-"))
        end = month_start_ns(f"{y + (m == 12)}-{m % 12 + 1:02d}") - hour  # last bar start of the last month
        panel = load_panel(
            PointInTime(catalog),
            root,
            a.symbols,
            first_bar_ns=start,
            last_bar_ns=end,
            config=PanelConfig(hour),
            datasets=(KLINES, MARK, FUNDING),  # the templates read no premium index
        )
        load_s = time.monotonic() - t0
        peak_mb = memory_sample()["private"] // 2**20
        rows: list[dict[str, object]] = []
        attempted = 0
        cross: dict[str, float] = {}
        for name, template in templates().items():
            body = template.model_dump(mode="json")
            body["universe"] = {"selector": "pit_top_liquidity", "n": a.n, "rebalance": "1d"}
            spec = StrategySpec.model_validate(body)
            equities: dict[str, np.ndarray] = {}
            for engine in ("event", "vector"):
                for stress in a.stress if engine == "event" else [1.0]:
                    ok, why = gate()
                    if not ok:
                        print(f"PAUSED at {name} {engine} x{stress}: {why}")  # CLI output
                        return 3
                    attempted += 1
                    t = time.monotonic()
                    cfg = RunConfig(engine=engine, cost=CostModel().stressed(stress))
                    out = run_backtest(spec, panel, catalog, ledger, cfg, stage="smoke", fold=f"x{stress:g}")
                    if out.result is not None and stress == 1.0:
                        equities[engine] = out.result.equity
                    met = out.metrics
                    rows.append(
                        {
                            "template": name,
                            "engine": engine,
                            "stress": stress,
                            "trial": out.trial_id,
                            "status": out.status,
                            "reason": out.reason,
                            "total_return": met.get("total_return"),
                            "worst_case_total_return": met.get("worst_case_total_return"),
                            "sr_daily": met.get("sr_daily"),
                            "max_drawdown": met.get("max_drawdown"),
                            "trades": met.get("trades"),
                            "costs": met.get("costs"),
                            "funding": met.get("funding"),
                            "avg_holding_hours": met.get("avg_holding_hours"),
                            "flags": met.get("flags", []),
                            "seconds": round(time.monotonic() - t, 2),
                        }
                    )
                    peak_mb = max(peak_mb, memory_sample()["private"] // 2**20)
            if len(equities) == 2:
                ev, vec = equities["event"], equities["vector"]
                cross[name] = float(np.max(np.abs(ev - vec) / np.maximum(np.abs(ev), 1e-12)))
        after = int(store.conn.execute("select count(*) from trial").fetchone()[0])
        new_runs = int(
            store.conn.execute("select count(*) from backtest_run where trial_id > ?", (first_new - 1,)).fetchone()[0]
        )
        refused = sum(1 for r in rows if r["status"] == "rejected" and r["trades"] is None)
        store.close()
        catalog.close()
    report: dict[str, object] = {
        "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "symbols": a.symbols,
        "window": [a.start, a.end],
        "decisions": panel.frame["decision_ns"].n_unique(),
        "lake_files": len(panel.files),
        "load_seconds": round(load_s, 1),
        "peak_private_mb": peak_mb,
        "abandoned_trials_closed": abandoned,
        "accounting": {
            "evaluations_attempted": attempted,
            "new_trials": after - before,
            "new_backtest_runs": new_runs,
            "refused_before_running": refused,
        },
        "cross_check_max_rel_diff": cross,
        "rows": rows,
    }
    _write_report(a.out, report, rows)
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=1))  # CLI output
    return 0


def _fmt(v: object) -> str:
    if isinstance(v, float):
        return f"{v:.4g}"
    return "-" if v is None else str(v)


def _write_report(path: Path, report: dict[str, object], rows: list[dict[str, object]]) -> None:
    acc = report["accounting"]
    lines = [
        "# Phase 2 실데이터 스모크 (tools/run_backtest.py)",
        "",
        "> 템플릿은 고정 파라미터이며 탐색하지 않았습니다. 기간이 짧고 비용 모델은 보정 전입니다(ADR-021).",
        "> **이 결과는 전략 성과의 증거가 아닙니다.** 목적은 엔진 교차 일치, 시도 회계, 미모델 항목 확인입니다.",
        "",
        f"- 생성: {report['generated_utc']}",
        f"- 심볼: {report['symbols']}",
        f"- 기간(연구 구간): {report['window']}, 결정 시점 {report['decisions']}개",
        f"- 레이크 파일 {report['lake_files']}개, 패널 적재 {report['load_seconds']}초",
        f"- 최대 private 메모리 {report['peak_private_mb']}MB",
        f"- 시도 회계: {json.dumps(acc, ensure_ascii=False)}",
        f"- 엔진 교차(이벤트 대 벡터, 자산 곡선 최대 상대 차이): {json.dumps(report['cross_check_max_rel_diff'])}",
        "",
        "| 템플릿 | 엔진 | 비용 배수 | trial | 상태 | 총수익 | 최악 가정 | SR(일) | MDD | 거래 | 비용 | 펀딩 "
        "| 평균 보유(h) | 표시 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        flags = r.get("flags")
        note = "; ".join(str(f) for f in cast(list[object], flags)) if isinstance(flags, list) else ""
        if r.get("reason"):
            note = f"{r['reason']} {note}".strip()
        lines.append(
            "| "
            + " | ".join(
                _fmt(r.get(k))
                for k in (
                    "template",
                    "engine",
                    "stress",
                    "trial",
                    "status",
                    "total_return",
                    "worst_case_total_return",
                    "sr_daily",
                    "max_drawdown",
                    "trades",
                    "costs",
                    "funding",
                    "avg_holding_hours",
                )
            )
            + f" | {note or '-'} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
