"""Phase 5 step 10: end-to-end null validation and power measurement (design v2.1 §9-9).

    uv run python tools/run_null_pipeline.py [--reps 100] [--arms random,tpe] [--alphas none,random,mechanism]
        [--s1-count 40] [--top-k 3] [--seed 7] [--out docs/measurements/phase5-null-pipeline.md]

Synthetic markets are rebuilt from the real research-split panel (8 symbols, 1h) by block-bootstrapping
returns; every replication runs the reduced search on its own TEMPORARY research.db and catalog (nothing is
written to C:/dev/qr-data), then the validator's rule judges the best candidate on the synthetic holdout.
Reports the end-to-end FWER (share of null replications with a pass) with a Wilson 95 % interval and the
recovery rate on planted alphas, per searcher arm. The Director (fake LLM) arm is run with --arms director.
Makes no claim about any real strategy."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT"]
ENV_ID = "ENV-2026.11.09"


def month_ns(y: int, m: int) -> int:
    return int(datetime(y, m, 1, tzinfo=UTC).timestamp()) * 10**9


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=100)
    ap.add_argument("--arms", default="random,tpe")
    ap.add_argument("--alphas", default="none,random,mechanism")
    ap.add_argument(
        "--alpha-strength", type=float, default=0.0015, help="random drift per hour / mean-reversion coefficient x0.1"
    )
    ap.add_argument("--s1-count", type=int, default=40)
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--families", default="trend,mean_reversion,momentum,volatility")
    ap.add_argument("--out", type=Path, default=Path("docs/measurements/phase5-null-pipeline.md"))
    a = ap.parse_args()
    os.environ.setdefault("POLARS_MAX_THREADS", "2")
    os.environ.setdefault("NUMBA_NUM_THREADS", "2")

    from quant_research.adapters.jobhost import memory_sample
    from quant_research.backtest.panel import PanelConfig, load_panel, slice_panel
    from quant_research.catalog.bootstrap import bootstrap
    from quant_research.catalog.store import Catalog
    from quant_research.config import load_settings
    from quant_research.data.access import PointInTime
    from quant_research.research.ledger import TrialLedger
    from quant_research.research.store import ResearchStore
    from quant_research.search.pipeline import SearchBudget
    from quant_research.strategy.ops import KLINES, MARK
    from quant_research.validator.null_pipeline import (
        HarnessResult,
        Replication,
        SyntheticSpec,
        families_round_robin,
        random_factory,
        replicate,
        synthetic_splits,
        tpe_factory,
    )
    from quant_research.validator.store import ValidatorStore
    from quant_research.validator.verdict import DecisionRule

    root = load_settings().data_root
    hour = 3_600 * 10**9
    arms = [s for s in a.arms.split(",") if s]
    alphas = [s for s in a.alphas.split(",") if s]
    families = [f for f in a.families.split(",") if f]
    rule = DecisionRule()  # ADR-022: alpha 0.05, k 20, c 0.5
    t0 = time.monotonic()
    catalog = Catalog.open(root / "meta" / "catalog.db")
    try:
        base = load_panel(
            PointInTime(catalog),
            root,
            SYMBOLS,
            first_bar_ns=month_ns(2024, 12),
            last_bar_ns=month_ns(2025, 7) - hour,
            config=PanelConfig(hour),
            datasets=(KLINES, MARK),
        )
    finally:
        catalog.close()
    base = slice_panel(base, month_ns(2025, 1), month_ns(2025, 7) - 2 * hour, warmup_ns=0)
    load_s = time.monotonic() - t0
    factories = {"random": random_factory, "tpe": tpe_factory}
    results: list[dict[str, object]] = []
    peak = memory_sample()["private"]
    work = Path(tempfile.mkdtemp(prefix="qr-null-"))
    try:
        for alpha in alphas:
            spec = SyntheticSpec(
                alpha=cast("str", alpha),  # type: ignore[arg-type]
                strength=(
                    a.alpha_strength
                    if alpha == "random"
                    else 0.1 * a.alpha_strength * 100
                    if alpha == "mechanism"
                    else 0.0
                ),
            )
            for arm in arms:
                if arm not in factories:
                    print(f"arm {arm} is not implemented here (director arm: run_director.py on synthetic panels)")
                    continue
                reps: list[Replication] = []
                fams = families_round_robin(families, a.reps)
                for i in range(a.reps):
                    seed = a.seed * 10_000 + i
                    splits = synthetic_splits(base, spec, seed=seed, warmup_days=30, sel_days=90, conf_days=30)
                    rep_dir = work / f"{alpha}-{arm}-{i}"
                    rep_dir.mkdir(parents=True, exist_ok=True)
                    cat = Catalog.open(rep_dir / "c.db")
                    bootstrap(cat)
                    store = ResearchStore.open(rep_dir / "r.db")
                    vstore = ValidatorStore.open(rep_dir / "v.db")
                    try:
                        ledger = TrialLedger(store, env_id=ENV_ID, code_sha="null-pipeline")
                        r = replicate(
                            splits,
                            cat,
                            ledger,
                            family=fams[i],
                            factory=factories[arm],
                            budget=SearchBudget(s1_count=a.s1_count, top_k=a.top_k, promote_rate=0.05, s3_top=1),
                            rule=rule,
                            seed=seed,
                        )
                        reps.append(r)
                        vstore.ensure_budget(1, rule.k, rule_hash=rule.hash())
                        peak = max(peak, memory_sample()["private"])
                        print(
                            json.dumps(
                                {
                                    "alpha": alpha,
                                    "arm": arm,
                                    "rep": i,
                                    "family": r.family,
                                    "passed": r.passed,
                                    "s3_sr": r.s3_sr,
                                }
                            )
                        )
                    finally:
                        vstore.close()
                        store.close()
                        cat.close()
                    shutil.rmtree(rep_dir, ignore_errors=True)
                hr = HarnessResult(f"{alpha}/{arm}", tuple(reps))
                results.append({**hr.summary(), "alpha": alpha, "arm": arm, "strength": spec.strength})
    finally:
        shutil.rmtree(work, ignore_errors=True)
    totals: dict[str, object] = {
        "reps": a.reps,
        "arms": arms,
        "alphas": alphas,
        "rule_hash": rule.hash(),
        "alpha_per_query": rule.alpha_per_query,
        "panel_load_s": round(load_s, 1),
        "wall_s": round(time.monotonic() - t0, 1),
        "peak_private_mb": peak // 2**20,
        "results": results,
    }
    _report(a.out, totals)
    print(json.dumps({k: v for k, v in totals.items() if k != "results"}, indent=1))
    for r in results:
        print(json.dumps(r, default=str))
    return 0


def _report(path: Path, totals: dict[str, object]) -> None:
    results = cast(list[dict[str, object]], totals["results"])
    lines = [
        "# Phase 5 파이프라인 귀무 검증·검정력 (tools/run_null_pipeline.py)",
        "",
        "> 신호 없는 합성 시장(실데이터 수익률의 블록 부트스트랩)에서 탐색(S0~S3)부터 holdout 판정까지 돌린 결과.",
        "> `none` 행의 통과율 = 종단 FWER 추정치(목표 0.05, ADR-022). `random`·`mechanism` 행 = 심은 알파의 회수율.",
        "> 모든 trial은 임시 research.db에 기록됐고 운영 research.db에는 쓰지 않았습니다.",
        "> 실전 전략의 성과 증거가 아닙니다.",
        "",
        f"- 설정: `{json.dumps({k: v for k, v in totals.items() if k != 'results'}, ensure_ascii=False)}`",
        "",
        "| 알파 | 강도 | 탐색기 | 반복 | 통과율 | Wilson 95% | 탐색기별 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        ci = cast(list[float], r["wilson_95"])
        lines.append(
            f"| {r['alpha']} | {r['strength']} | {r['arm']} | {r['replications']} | "
            f"{float(cast(float, r['pass_rate'])):.3f} | [{ci[0]:.3f}, {ci[1]:.3f}] | {json.dumps(r['by_searcher'])} |"
        )
    lines += [
        "",
        "## 판정",
        "- FWER(`none` 행)의 Wilson 95% 상한이 0.05 아래면 목표 충족, 아니면 **미충족으로 그대로 보고**.",
        "  기준 조정은 사전 등록으로만 합니다.",
        "- 검정력(`random`·`mechanism` 행)은 8심볼·6개월 합성 시장 기준이라 낮을 수 있습니다. 표본이 늘면 다시 측정.",
        "- Director(가짜 LLM) 비교는 같은 합성 패널에서 별도 측정합니다(`--arms director`는 이 도구에서 미구현 표시).",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
