"""v2.4 step 8 (ADR-055 decision 4): false candidates of the campaign's combined confirmation on null markets.

    uv run python tools/measure_combined_null.py [--reps 100] [--nulls permute,signflip] [--max-minutes 18]
        [--state C:/dev/qr-data/run/combined-null.jsonl] [--report docs/measurements/v24-combined-null.md]

One replication = two independent null markets (the windows w2 and w1), rebuilt from the real research panel's
hourly returns (block permutation / block sign flip, ADR-043) with premium and funding inputs that are independent
of the returns. Each cell of the list below runs ONE campaign batch on w2 with the real runner (space version 3:
lookback caps, funding required, model cells), which includes the own S3 and, when that is positive, the same spec
on w1's confirmation split and the pooled best-of-N bar. The bar uses the N a real campaign starts with: the S3
verdicts of campaign c1 (`--prior-run`), so the measured rate is the one c2 will have at its first batches.

Every replication uses its own temporary catalog / research.db; nothing is written to the research databases.
Finished replications are appended to `--state` (JSON lines) and skipped on the next call, so the measurement can
run in several foreground calls. A measurement, not a performance claim."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT"]
FAMILIES = ("momentum", "xs_reversal", "low_vol", "premium_basis", "funding_carry", "combo", "ridge")
ENV_ID = "ENV-2026.11.09"
HOURS = 24 * 240  # warm-up 30 d, selection 150 d, confirmation 60 d
WARMUP_DAYS, SELECTION_DAYS = 30, 150
DAY_NS = 86_400 * 10**9


def month_ns(y: int, m: int) -> int:
    return int(datetime(y, m, 1, tzinfo=UTC).timestamp()) * 10**9


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def render(rows: Sequence[Mapping[str, object]], prior: Mapping[str, int], reps_per_null: Mapping[str, int]) -> str:
    lines = [
        "# 결합 확인 단계의 거짓 후보 비율: 귀무 시장 (ADR-055 결정 4, v2.4 단계 8)",
        "",
        "> 실제 캠페인 러너(공간 v3, 펀딩 필수, 모델 셀)를 예측 구조가 없는 합성 시장에서 돌린 측정이다.",
        "> 한 반복 = 독립인 귀무 시장 2개(w2, w1). 셀마다 w2에서 배치 1개를 돌리고, 자기 S3가 양수면 같은 spec을",
        "> w1 확인 구간에서 재서 결합 판정한다. 기준의 N은 c2가 시작할 때의 값(c1의 S3 판정 수)이다.",
        "> 이 표는 측정이고 성과 주장이 아니다.",
        "",
        f"- 시작 N(이전 캠페인의 S3 판정 수): {dict(prior)}",
        f"- 반복 수: {dict(reps_per_null)} (귀무 종류별)",
        f"- 셀: {', '.join(FAMILIES)} @1h",
        f"- 시장 길이 {HOURS // 24}일: 워밍업 {WARMUP_DAYS}, 선택 {SELECTION_DAYS}, 확인은 나머지",
        "",
        "## 배치(판정) 단위",
        "",
        "| 귀무 | 계열 | 배치 | S3 판정 | 자기 S3 > 0 | 교차 측정 | 후보 | 후보 비율 | Wilson 95% |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    nulls = sorted({str(r["null"]) for r in rows})
    for null in nulls:
        for family in (*FAMILIES, "전체"):
            sub = [r for r in rows if r["null"] == null and (family == "전체" or r["family"] == family)]
            if not sub:
                continue
            judged = sum(1 for r in sub if r["s3_sr"] is not None)
            positive = sum(1 for r in sub if isinstance(r["s3_sr"], int | float) and r["s3_sr"] > 0)
            crossed = sum(1 for r in sub if r["cross_sr"] is not None)
            cands = sum(1 for r in sub if r["cause"] == "candidate")
            lo, hi = wilson(cands, len(sub))
            lines.append(
                f"| {null} | {family} | {len(sub)} | {judged} | {positive} | {crossed} | {cands} "
                f"| {cands / len(sub):.4f} | [{lo:.4f}, {hi:.4f}] |"
            )
    lines += ["", "## 원인 분포", "", "| 귀무 | 원인 | 배치 |", "|---|---|---|"]
    for null in nulls:
        causes = Counter(str(r["cause"]) for r in rows if r["null"] == null)
        lines += [f"| {null} | {cause} | {n} |" for cause, n in sorted(causes.items())]
    lines += [
        "",
        "## 반복 단위 (한 반복에서 후보가 1개 이상 나온 비율)",
        "",
        "| 귀무 | 반복 | 후보 있는 반복 | 비율 | Wilson 95% |",
        "|---|---|---|---|---|",
    ]
    for null in nulls:
        reps = sorted({int(cast(int, r["rep"])) for r in rows if r["null"] == null})
        hit = sum(
            1 for i in reps if any(r["cause"] == "candidate" for r in rows if r["null"] == null and r["rep"] == i)
        )
        lo, hi = wilson(hit, len(reps))
        lines.append(f"| {null} | {len(reps)} | {hit} | {hit / max(len(reps), 1):.4f} | [{lo:.4f}, {hi:.4f}] |")
    total = len(rows)
    cands = sum(1 for r in rows if r["cause"] == "candidate")
    judged = sum(1 for r in rows if r["s3_sr"] is not None)
    broken = sum(1 for r in rows if r["cause"] in ("error", "data_missing", "infeasible"))
    lo, hi = wilson(cands, total)
    lo_j, hi_j = wilson(cands, judged)
    # review (step 8): a batch that never reached S3 cannot be a candidate; judged on the stricter denominator,
    # and never as met while a cell kind failed to run at all
    rate = cands / judged if judged else 1.0
    verdict = "판정 불가(error·data_missing 배치가 있음)" if broken else ("충족" if rate <= 0.05 else "미충족")
    lines += [
        "",
        "## 판정",
        "",
        f"- 전체 배치 {total}개 중 후보 {cands}개: 비율 {cands / max(total, 1):.4f}, Wilson 95% [{lo:.4f}, {hi:.4f}].",
        f"- S3 판정까지 간 배치 {judged}개 기준: 비율 {rate:.4f}, Wilson 95% [{lo_j:.4f}, {hi_j:.4f}].",
        f"- 실행되지 못한 배치(error·data_missing·infeasible): {broken}개.",
        "- 기준(ADR-055 결정 4, 보강): 시작 N에서 배치(판정)당 거짓 후보 비율의 점추정 ≤ 0.05. "
        "분모는 S3 판정까지 간 배치로 본다(더 엄격한 쪽).",
        f"- 결과: {verdict}.",
        "- 후보는 검증기 제출 대상일 뿐이다. 최종 판정은 봉인 구간에서 유의수준 0.05 / 20 = 0.0025로 따로 한다.",
        "",
        "## 한계",
        "",
        "- 축소 예산이다: 배치당 S1 12개, S2 3개(c2는 60개, 5개 + 5%). S2가 적으면 선택 구간 최선 SR이 양수일 확률이 "
        "낮아 교차 확인까지 가는 배치가 줄어든다. 실제 c2의 배치당 비율은 이 값보다 높을 수 있다.",
        "- 셀 구성이 한 가지다: 8종목, 1h, 상위 30. c2의 첫 라운드는 4h·1d, 상위 100이다.",
        "- 순열 귀무의 두 시장은 같은 실데이터 구간의 수익률을 시드만 달리해 재배열한 것이다. 종목별 평균 수익률이 "
        "두 시장에 공통으로 남아 거짓 후보를 늘리는 쪽으로 작용한다(부호 뒤집기 귀무에는 없는 효과).",
        "- 반복 수는 귀무 종류별 30회다(계획의 200회 이상은 계산 시간 때문에 채우지 못했다).",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reps", type=int, default=100, help="replications per null kind")
    ap.add_argument("--nulls", default="permute,signflip")
    ap.add_argument("--s1-count", type=int, default=12)
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--prior-run", default="c1")
    ap.add_argument("--max-minutes", type=float, default=18.0)
    ap.add_argument("--state", type=Path, default=None)
    ap.add_argument("--report", type=Path, default=None)
    a = ap.parse_args()
    os.environ.setdefault("POLARS_MAX_THREADS", "2")
    os.environ.setdefault("NUMBA_NUM_THREADS", "2")

    from quant_research.backtest.panel import Panel, PanelConfig, load_panel, slice_panel
    from quant_research.capture.run import repo_root
    from quant_research.catalog.bootstrap import bootstrap
    from quant_research.catalog.store import Catalog
    from quant_research.config import load_settings
    from quant_research.data.access import PointInTime
    from quant_research.research.ledger import TrialLedger
    from quant_research.research.store import ResearchStore
    from quant_research.search.bandit import BanditPrior, FamilyBandit
    from quant_research.search.campaign import CampaignState, Cell
    from quant_research.search.campaign_runner import CampaignConfig, judged_s3_counts, run_campaign
    from quant_research.strategy.ops import KLINES, MARK
    from quant_research.validator.null_pipeline import NullKind, SyntheticSpec, parse_null, synthetic_panel

    root = load_settings().data_root
    state_path = a.state or root / "run" / "combined-null.jsonl"
    report = a.report or repo_root() / "docs" / "measurements" / "v24-combined-null.md"
    hour = 3_600 * 10**9
    prior_file = root / "run" / f"campaign-{a.prior_run}.json"
    prior_state = CampaignState.from_json(json.loads(prior_file.read_text(encoding="utf-8"))["state"])
    prior = judged_s3_counts([prior_state.history])
    rows: list[dict[str, object]] = []
    if state_path.exists():
        rows = [json.loads(line) for line in state_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    done = {(str(r["null"]), int(cast(int, r["rep"]))) for r in rows}
    nulls: list[NullKind] = [parse_null(n) for n in a.nulls.split(",") if n]
    deadline = time.monotonic() + a.max_minutes * 60.0

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

    def split(panel: Panel) -> tuple[Panel, Panel]:
        grid = sorted(set(panel.frame["decision_ns"].to_list()))
        t0 = int(grid[0]) + WARMUP_DAYS * DAY_NS
        s1 = t0 + SELECTION_DAYS * DAY_NS
        warm = WARMUP_DAYS * DAY_NS
        return slice_panel(panel, t0, s1 - hour, warmup_ns=warm), slice_panel(
            panel, s1, int(grid[-1]) - hour, warmup_ns=warm
        )

    def provider(markets: Mapping[str, tuple[Panel, Panel]]) -> Callable[[Cell], tuple[Panel, Panel]]:
        return lambda cell: markets[cell.window]

    config = CampaignConfig(
        seed=a.seed,
        s1_count=a.s1_count,
        top_k=a.top_k,
        promote_rate=0.0,
        s3_top=1,
        s3_random_rate=0.0,
        space_version=3,
        max_batches=1,  # one batch per cell: a transition target (wider universe, other window) is not followed
    )
    ran = 0
    work = Path(tempfile.mkdtemp(prefix="qr-combined-null-"))
    try:
        for null in nulls:
            for i in range(a.reps):
                if (str(null), i) in done:
                    continue
                if time.monotonic() > deadline:
                    break
                spec = SyntheticSpec(null=null, hours=HOURS)
                seed = a.seed * 100_000 + i
                markets = {
                    "w2": split(synthetic_panel(base, spec, seed=seed, inputs=True)),
                    "w1": split(synthetic_panel(base, spec, seed=seed + 50_000, inputs=True)),
                }
                rep_dir = work / f"{null}-{i}"
                rep_dir.mkdir(parents=True, exist_ok=True)
                cat = Catalog.open(rep_dir / "c.db")
                bootstrap(cat)
                store = ResearchStore.open(rep_dir / "r.db")
                batch_rows: list[dict[str, object]] = []
                try:
                    ledger = TrialLedger(store, env_id=ENV_ID, code_sha="combined-null")
                    for family in FAMILIES:
                        cell = Cell(family, "1h", 30, "w2")
                        state = CampaignState.new(f"null-{null}-{i}-{family}", [cell], max_pulls=1)
                        run_campaign(
                            state,
                            FamilyBandit(tuple(c.key for c in state.all_cells()), BanditPrior(min_pulls=0.0)),
                            ledger=ledger,
                            catalog=cat,
                            panels=provider(markets),
                            config=config,
                            gate=lambda: (True, ""),
                            stop=lambda: False,
                            persist=lambda _s, _b: None,
                            prior_s3=prior,
                        )
                        h = state.history[0]
                        sm = cast(Mapping[str, object], h["summary"])
                        batch_rows.append(
                            {
                                "null": null,
                                "rep": i,
                                "family": family,
                                "cause": h["cause"],
                                "s3_sr": sm.get("s3_sr"),
                                "cross_sr": sm.get("cross_sr"),
                                "pooled_sr": sm.get("pooled_sr"),
                                "pooled_threshold": sm.get("pooled_threshold"),
                                "error": sm.get("error", ""),
                            }
                        )
                finally:
                    store.close()
                    cat.close()
                    shutil.rmtree(rep_dir, ignore_errors=True)
                with state_path.open("a", encoding="utf-8", newline="\n") as f:
                    for r in batch_rows:
                        f.write(json.dumps(r) + "\n")
                rows.extend(batch_rows)
                ran += 1
    finally:
        shutil.rmtree(work, ignore_errors=True)
    reps_per_null = {n: len({int(cast(int, r["rep"])) for r in rows if r["null"] == n}) for n in nulls}
    report.write_bytes(render(rows, prior, reps_per_null).encode("utf-8"))
    print(
        json.dumps(
            {
                "ran": ran,
                "reps": reps_per_null,
                "batches": len(rows),
                "candidates": sum(1 for r in rows if r["cause"] == "candidate"),
                "errors": sum(1 for r in rows if r["cause"] == "error"),
                "report": str(report),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
