"""v2.2 A3: end-to-end FWER and power of the whole pipeline on long synthetic markets (ADR-042, ADR-043).

    uv run python tools/run_null_v2.py [--nulls bootstrap,permute,signflip] [--z 0] [--alpha none]
        [--arm random] [--reps 50] [--holdout-days 700] [--k 20] [--families trend,mean_reversion,momentum,volatility]
        [--s1-count 30] [--top-k 3] [--s3-top 5] [--seed 7] [--out docs/measurements/phase5-null-v2.md]
        [--jsonl PATH]

Each replication is ONE synthetic epoch: a market of warm-up 30 + selection 90 + confirmation 30 + holdout
`--holdout-days` days is rebuilt from the real research panel (8 symbols, 1h, 2025-01..06, research role) with
the chosen null (centred simple returns); every family is searched on its own TEMPORARY research.db / catalog /
validator.db; the positive confirmation survivors are submitted to the verdict rule v2 in SR order until k queries
are spent (insufficient ones spend nothing). FWER = share of null epochs with at least one pass (Wilson 95 %).
With --z > 0 the planted alpha's strength is calibrated per null so that long-all / fade-last-hour reaches that
gross hourly z (power). Every replication is appended to --jsonl as it finishes, so a long run can be stopped
and its partial results read. Makes no claim about any real strategy."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT"]
ENV_ID = "ENV-2026.11.09"
WARMUP, SEL, CONF = 30, 90, 30


def month_ns(y: int, m: int) -> int:
    return int(datetime(y, m, 1, tzinfo=UTC).timestamp()) * 10**9


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nulls", default="signflip", help="comma list of bootstrap, permute, signflip")
    ap.add_argument("--alpha", default="none", choices=["none", "random", "mechanism"])
    ap.add_argument(
        "--z", type=float, default=0.0, help="target gross hourly z of a tradable strategy on the alpha (0 = null)"
    )
    ap.add_argument("--arm", default="random", choices=["random", "tpe"])
    ap.add_argument("--reps", type=int, default=50)
    ap.add_argument("--holdout-days", type=int, default=700)
    ap.add_argument("--k", type=int, default=20)
    ap.add_argument("--families", default="trend,mean_reversion,momentum,volatility")
    ap.add_argument("--s1-count", type=int, default=30)
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--s3-top", type=int, default=5)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path, default=Path("docs/measurements/phase5-null-v2.md"))
    ap.add_argument("--jsonl", type=Path, default=None)
    a = ap.parse_args()
    if a.reps < 1 or a.z < 0:
        ap.error("--reps must be >= 1 and --z >= 0")
    if a.alpha == "none" and a.z > 0:
        ap.error("--z > 0 needs a planted --alpha (random or mechanism)")
    if a.alpha != "none" and a.z <= 0:
        ap.error("a planted --alpha needs --z > 0")
    os.environ.setdefault("POLARS_MAX_THREADS", "2")
    os.environ.setdefault("NUMBA_NUM_THREADS", "2")

    import numpy as np

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
        EpochReplication,
        EpochResult,
        NullKind,
        SyntheticSpec,
        calibrate_strength,
        hourly_closes,
        parse_null,
        random_factory,
        replicate_epoch,
        synthetic_splits,
        tpe_factory,
    )
    from quant_research.validator.store import ValidatorStore
    from quant_research.validator.verdict import DecisionRule

    root = load_settings().data_root
    hour = 3_600 * 10**9
    nulls: list[NullKind] = [parse_null(s) for s in a.nulls.split(",") if s]  # a typo is refused
    families = [f for f in a.families.split(",") if f]
    rule = DecisionRule(k=a.k)  # v2: alpha 0.05, k (20 = ADR-022), preconditions, PSR (ADR-042)
    if a.holdout_days < rule.min_holdout_days:  # every submission would be insufficient: a meaningless FWER 0
        ap.error(f"--holdout-days must be >= the rule's minimum {rule.min_holdout_days}")
    from quant_research.capture.run import git_sha, repo_root

    code_sha = git_sha(repo_root())
    import hashlib

    sources = ["null_pipeline.py", "verdict.py", "store.py"]
    folder = repo_root() / "src" / "quant_research" / "validator"
    code_sha += "+src:" + hashlib.sha256(b"".join((folder / f).read_bytes() for f in sources)).hexdigest()[:12]
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
    _, _, closes = hourly_closes(base)
    rets = np.nan_to_num(np.diff(np.log(closes), axis=0))
    sigma_h = float(np.median(np.std(rets, axis=0)))
    hours = (WARMUP + SEL + CONF + a.holdout_days) * 24 + 24
    strengths = {  # calibrated per null: the same strength means a different z under each null (re-review)
        null: calibrate_strength(a.alpha, a.z, closes, holdout_hours=a.holdout_days * 24, seed=a.seed, null=null)
        for null in nulls
    }
    factory = {"random": random_factory, "tpe": tpe_factory}[a.arm]
    budget = SearchBudget(s1_count=a.s1_count, top_k=a.top_k, promote_rate=0.05, s3_top=a.s3_top)
    load_s = time.monotonic() - t0
    peak = memory_sample()["private"]
    results: list[dict[str, object]] = []
    work = Path(tempfile.mkdtemp(prefix="qr-null-"))
    try:
        for null in nulls:
            strength = strengths[null]
            spec = SyntheticSpec(alpha=a.alpha, strength=strength, null=null, hours=hours)  # type: ignore[arg-type]
            reps: list[EpochReplication] = []
            for i in range(a.reps):
                seed = a.seed * 10_000 + i
                t_rep = time.monotonic()
                splits = synthetic_splits(base, spec, seed=seed, warmup_days=WARMUP, sel_days=SEL, conf_days=CONF)
                rep_dir = work / f"{null}-{i}"
                rep_dir.mkdir(parents=True, exist_ok=True)
                cat = Catalog.open(rep_dir / "c.db")
                bootstrap(cat)
                store = ResearchStore.open(rep_dir / "r.db")
                vstore = ValidatorStore.open(rep_dir / "v.db")
                try:
                    ledger = TrialLedger(store, env_id=ENV_ID, code_sha="null-v2")
                    r = replicate_epoch(
                        splits,
                        cat,
                        ledger,
                        vstore,
                        families=families,
                        factory=factory,
                        budget=budget,
                        rule=rule,
                        seed=seed,
                        k=a.k,
                    )
                finally:
                    vstore.close()
                    store.close()
                    cat.close()
                shutil.rmtree(rep_dir, ignore_errors=True)
                reps.append(r)
                peak = max(peak, memory_sample()["private"])
                line = {
                    "null": null,
                    "alpha": a.alpha,
                    "z": a.z,
                    "strength": strength,
                    "arm": a.arm,
                    "k": a.k,
                    "holdout_days": a.holdout_days,
                    "seed": seed,
                    "rule_hash": rule.hash(),
                    "code_sha": code_sha,
                    "families": families,
                    "budget": {"s1_count": a.s1_count, "top_k": a.top_k, "s3_top": a.s3_top, "promote_rate": 0.05},
                    "block_len": spec.block_len,
                    "rep": i,
                    "candidates": r.candidates,
                    "queries": r.queries,
                    "insufficient": r.insufficient,
                    "passes": r.passes,
                    "s": round(time.monotonic() - t_rep, 1),
                    "records": [rec for rec in r.records if rec.get("passed")],  # passes are kept for inspection
                }
                print(json.dumps(line, default=str), flush=True)
                if a.jsonl is not None:
                    with a.jsonl.open("a", encoding="utf-8", newline="\n") as fh:
                        fh.write(json.dumps(line, default=str) + "\n")
            summary = EpochResult(f"{null}/{a.alpha}/z{a.z:g}/{a.arm}", tuple(reps)).summary()
            results.append({**summary, "null": null})
    finally:
        shutil.rmtree(work, ignore_errors=True)
    totals: dict[str, object] = {
        "reps": a.reps,
        "arm": a.arm,
        "alpha": a.alpha,
        "z": a.z,
        "strength": strengths,
        "z_meaning": "gross hourly z (simple returns) of long-all / fade-last-hour on the synthetic holdout",
        "sigma_h": sigma_h,
        "holdout_days": a.holdout_days,
        "k": a.k,
        "budget": {"s1_count": a.s1_count, "top_k": a.top_k, "s3_top": a.s3_top, "families": families},
        "rule_hash": rule.hash(),
        "alpha_per_query": rule.alpha_per_query,
        "panel_load_s": round(load_s, 1),
        "wall_s": round(time.monotonic() - t0, 1),
        "peak_private_mb": peak // 2**20,
        "results": results,
    }
    _report(a.out, totals)
    print(json.dumps({k: v for k, v in totals.items() if k != "results"}, indent=1, default=str))
    for r in results:
        print(json.dumps(r, default=str))
    return 0


def _report(path: Path, totals: dict[str, object]) -> None:
    results = cast(list[dict[str, object]], totals["results"])
    lines = [
        "# Phase 5 종단 FWER·검정력 v2 (tools/run_null_v2.py)",
        "",
        "> 합성 시장(실데이터 8심볼 1h 수익률에서 재구성) 한 개 = 한 epoch. 4계열 탐색 → 확인 구간 SR 양수 후보를"
        " SR 순으로 판정 규칙 v2에 제출(질의 k회까지, 불충분은 미차감). FWER = 통과가 1건 이상인 epoch 비율.",
        "> 임시 DB만 썼고 운영 DB에는 쓰지 않았다. 실전 전략의 성과 증거가 아니다.",
        "",
        f"- 설정: `{json.dumps({k: v for k, v in totals.items() if k != 'results'}, ensure_ascii=False, default=str)}`",
        "",
        "| 귀무 | 반복 | 측정 epoch | 후보 0 | 측정 불가 | FWER | Wilson 95% | 평균 질의 | 평균 후보 | 불충분 비율 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        ci = cast(list[float], r["wilson_95"])
        fwer = float(cast(float, r["fwer"]))
        lines.append(
            f"| {r['null']} | {r['replications']} | {r['judged_replications']} | {r['no_candidate']} | "
            f"{r['unmeasured']} | "
            f"{'n/a' if math.isnan(fwer) else f'{fwer:.3f}'} | [{ci[0]:.3f}, {ci[1]:.3f}] | "
            f"{float(cast(float, r['mean_queries'])):.1f} | {float(cast(float, r['mean_candidates'])):.1f} | "
            f"{float(cast(float, r['insufficient_share'])):.2f} |"
        )
    lines += [
        "",
        "## 판정 기준(사전 등록, ADR-022·043)",
        "- 귀무(z = 0): FWER의 Wilson 95% 상한이 0.05 이하면 충족.",
        "- 측정 가능 epoch가 0이면 '측정 불가'이지 FWER 0이 아니다.",
        "- 알파(z > 0): 회수율 = 통과 epoch 비율(검정력).",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
