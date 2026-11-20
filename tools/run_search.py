"""Phase 4 step 10: real-data search batches on the research split (8 symbols, 1h) and the measurement table
for the two completion criteria (cost prediction within 0.5-1.5 for >= 90 % of batches; zero accounting gaps).

    uv run python tools/run_search.py [--families trend,momentum,...] [--searchers tpe,random] [--s1-count 200]
        [--top-k 10] [--promote-rate 0.05] [--seed 7] [--out docs/measurements/phase4-search-batch.md]

One worker, BelowNormal priority, POLARS_MAX_THREADS=2; the batch is refused when the research gate (RAM,
disk, SSD thermal) or the collector heartbeat (rest.heartbeat.json older than 120 s) says no. Every stage is
a trial; the cost model is re-fitted from the recorded trials after each batch and its prediction for the
NEXT batch is compared with the measured time. Makes no claim about any strategy's performance.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

ENV_ID = "ENV-2026.11.09"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT"]
BELOW_NORMAL_PRIORITY_CLASS = 0x4000
HEARTBEAT_STALE_S = 120.0
OPS_PRIOR = 20  # typical expanded-tree node count used for the pre-batch prediction


def month_ns(y: int, m: int) -> int:
    return int(datetime(y, m, 1, tzinfo=UTC).timestamp()) * 10**9


def lower_priority() -> bool:
    """BelowNormal process priority on Windows (the collector keeps its share of the CPU)."""
    if sys.platform != "win32":
        return False
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    kernel32.SetPriorityClass.restype = ctypes.c_int
    return bool(kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS))


def heartbeat_age_s(path: Path) -> float | None:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    at = body.get("at_ns")
    return (time.time_ns() - int(at)) / 1e9 if isinstance(at, int) else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--families", default="trend,mean_reversion,momentum,funding_basis,volatility")
    ap.add_argument("--searchers", default="tpe,random")
    ap.add_argument("--s1-count", type=int, default=200)
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--promote-rate", type=float, default=0.05)
    ap.add_argument("--s3-top", type=int, default=5)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--compare-budget-s", type=float, default=30.0, help="synthetic Random-vs-TPE cost budget")
    ap.add_argument("--out", type=Path, default=Path("docs/measurements/phase4-search-batch.md"))
    a = ap.parse_args()
    os.environ.setdefault("POLARS_MAX_THREADS", "2")
    os.environ.setdefault("NUMBA_NUM_THREADS", "2")

    from quant_research.adapters.jobhost import memory_sample
    from quant_research.backtest.panel import PanelConfig, load_panel, slice_panel
    from quant_research.backtest.run import RunConfig, unblob
    from quant_research.capture.lock import InstanceLock
    from quant_research.capture.run import git_sha, repo_root
    from quant_research.catalog.store import Catalog
    from quant_research.config import load_settings
    from quant_research.data.access import PointInTime
    from quant_research.research.guard import research_gate
    from quant_research.research.ledger import TrialLedger
    from quant_research.research.store import ResearchStore
    from quant_research.search.archive import Archive
    from quant_research.search.bandit import BanditPrior, FamilyBandit
    from quant_research.search.compare import compare, planted_landscape
    from quant_research.search.cost_model import BAND, CostSample, LinearCost, fit_cost
    from quant_research.search.multiplicity import dsr_report, sharpe_stats
    from quant_research.search.neff_stage import group_summary, neff_groups
    from quant_research.search.optimizers import RandomSearcher, Searcher, TpeSearcher
    from quant_research.search.pipeline import SearchBudget, run_family_search, split_for_reward
    from quant_research.search.s1 import S1Config
    from quant_research.search.s2_stage import fidelity_by_family
    from quant_research.strategy.ops import FUNDING, KLINES, MARK
    from quant_research.strategy.space import SPACES

    root = load_settings().data_root
    hour, day = 3_600 * 10**9, 86_400 * 10**9
    run_id = a.run_id or f"search-s{a.seed}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
    families = [f for f in a.families.split(",") if f]
    searchers = [s for s in a.searchers.split(",") if s]
    priority = lower_priority()
    gate0 = research_gate(root)
    hb_rest, hb_ws = root / "run" / "rest.heartbeat.json", root / "run" / "ws.heartbeat.json"

    def gate() -> tuple[bool, str]:
        ok, why = gate0()
        if not ok:
            return ok, why
        age = heartbeat_age_s(hb_rest)
        if age is None or age > HEARTBEAT_STALE_S:
            return False, f"collector rest heartbeat age {age} s (stale > {HEARTBEAT_STALE_S} s or missing)"
        return True, "ok"

    def collector_state(catalog: Catalog) -> dict[str, object]:
        return {
            "gaps": int(catalog.conn.execute("select count(*) from gap").fetchone()[0]),
            "rest_hb_age_s": heartbeat_age_s(hb_rest),
            "ws_hb_age_s": heartbeat_age_s(hb_ws),
        }

    def cost_samples(store: ResearchStore) -> list[CostSample]:
        rows = store.conn.execute(
            "select stage, metrics_json, cost_s from trial where status in ('done', 'rejected') and cost_s is not null "
            "and metrics_json is not null and stage in ('S1', 'S2', 'S3')"
        ).fetchall()
        out: list[CostSample] = []
        for stage, m, cost in rows:
            body = json.loads(m)
            r, o = body.get("rows"), body.get("ops")
            if isinstance(r, int) and isinstance(o, int) and r > 0 and o > 0:
                out.append(CostSample(str(stage), r, o, float(cost)))
        return out

    with InstanceLock(root / "run" / "research.lock"):
        catalog = Catalog.open(root / "meta" / "catalog.db")
        store = ResearchStore.open(root / "meta" / "research.db")
        try:
            ledger = TrialLedger(store, env_id=ENV_ID, code_sha=git_sha(repo_root()))
            ledger.close_abandoned()
            before = collector_state(catalog)
            t0, c0 = time.monotonic(), time.process_time()
            panel = load_panel(
                PointInTime(catalog),
                root,
                SYMBOLS,
                first_bar_ns=month_ns(2024, 12),
                last_bar_ns=month_ns(2025, 7) - hour,
                config=PanelConfig(hour),
                datasets=(KLINES, MARK, FUNDING),
            )
            selection = slice_panel(panel, month_ns(2025, 1), month_ns(2025, 5) - hour, warmup_ns=30 * day)
            confirmation = slice_panel(panel, month_ns(2025, 5), month_ns(2025, 7) - 2 * hour, warmup_ns=30 * day)
            load_s = time.monotonic() - t0
            peak = memory_sample()["private"]
            bandit = FamilyBandit(tuple(SPACES), BanditPrior(min_pulls=0.0))
            archive = Archive()
            base_config: dict[str, object] = {"run_id": run_id, "seed": a.seed, "search": "space", "tool": "run_search"}
            batches: list[dict[str, object]] = []
            trials_before = int(store.conn.execute("select count(*) from trial").fetchone()[0])
            # with a bandit (always here) S1/S2 run on the fit part of selection (A4): predict on those rows
            rows = split_for_reward(selection, 0.25)[0].frame.height
            n_s2 = a.top_k + round(a.promote_rate * a.s1_count)
            for family in families:
                for name in searchers:
                    fit = fit_cost(cost_samples(store))  # from everything recorded so far
                    pred: dict[str, float | None] = {
                        "S1": fit["S1"].predict(rows, OPS_PRIOR) * a.s1_count if "S1" in fit else None,
                        "S2": fit["S2"].predict(rows, OPS_PRIOR) * n_s2 if "S2" in fit else None,
                    }
                    seed = a.seed * 1_000 + len(batches)
                    searcher: Searcher
                    if name == "tpe":
                        searcher = TpeSearcher(SPACES[family], seed=seed)
                    else:
                        searcher = RandomSearcher(SPACES[family], seed)
                    ok, why = gate()
                    if not ok:
                        batches.append({"family": family, "searcher": name, "refused": why})
                        print(f"refused {family}/{name}: {why}")
                        break
                    w = time.monotonic()
                    res = run_family_search(
                        ledger,
                        family,
                        searcher,
                        selection=selection,
                        confirmation=confirmation,
                        catalog=catalog,
                        config={**base_config, "family": family, "searcher": name},
                        s1=S1Config(),
                        budget=SearchBudget(
                            s1_count=a.s1_count, top_k=a.top_k, promote_rate=a.promote_rate, s3_top=a.s3_top
                        ),
                        seed=seed,
                        fold=f"{run_id}:{family}:{name}",
                        gate=gate,
                        bandit=bandit,
                        archive=archive,
                        s2_config=RunConfig(engine="vector", seed=a.seed),
                        s3_config=RunConfig(engine="event", seed=a.seed),
                        cost_model=fit or None,
                    )
                    peak = max(peak, memory_sample()["private"])
                    ratios: dict[str, float | None] = {}
                    pred = {**pred, **{k: v for k, v in res.predicted.items() if k in ("S1", "S2")}}  # A7: actual ops
                    for st in ("S1", "S2"):
                        p = pred[st]
                        actual = res.cost_s.get(st, 0.0)
                        ratios[st] = None if p is None or actual <= 0 else p / actual
                    optuna = searcher.internal_evaluations() if isinstance(searcher, TpeSearcher) else None
                    batches.append(
                        {
                            **res.summary(),
                            "predicted_s": pred,
                            "ratio_pred_over_actual": ratios,
                            "optuna_evaluations": optuna,
                            "wall_s": round(time.monotonic() - w, 1),
                            "cost_fit": {k: [v.a, v.b] for k, v in fit.items()},
                        }
                    )
                    line = {"family": family, "searcher": name, **res.counts(), "wall_s": batches[-1]["wall_s"]}
                    print(json.dumps(line))
            trials_after = int(store.conn.execute("select count(*) from trial").fetchone()[0])
            ran = [b for b in batches if "s0_trials" in b]
            expected = sum(
                _i(b, "s0_trials") + _i(b, "s1_trials") + _i(b, "s2_trials") + _i(b, "s3_trials") + _i(b, "val_trials")
                for b in ran
            )
            ratios_all = [
                float(cast(float, r))
                for b in ran
                for r in cast(dict[str, object], b.get("ratio_pred_over_actual", {})).values()
                if isinstance(r, int | float)
            ]
            in_band = sum(1 for r in ratios_all if BAND[0] <= r <= BAND[1])
            groups = [group_summary(g) for g in neff_groups(ledger, seed=a.seed)]
            fidelity = [
                {"family": f.family, "pairs": f.pairs, "rank_corr": f.rank_corr} for f in fidelity_by_family(ledger)
            ]
            dsr_rows: list[dict[str, object]] = []
            for g in groups:
                n_eff, n_total = int(cast(int, g["n_eff"])), int(cast(int, g["n_total"]))
                best = store.conn.execute(
                    "select t.trial_id, b.pnl_daily from trial t join backtest_run b using (trial_id) "
                    "where t.stage = 'S3' and t.status = 'done' and b.window_from_ns = ? and b.window_to_ns = ? "
                    "order by json_extract(t.metrics_json, '$.sr_daily') desc limit 1",
                    (g["window_from_ns"], g["window_to_ns"]),
                ).fetchone()
                if best is None:
                    continue
                daily = unblob(bytes(best[1]))
                if daily.shape[0] < 3 or float(daily.std()) <= 0:
                    continue
                st = sharpe_stats(daily)
                rep = dsr_report(st, n_eff=max(1, n_eff), n_total=max(n_eff, n_total), var_sr_trials=st.variance())
                dsr_rows.append(
                    {
                        "trial": int(best[0]),
                        "sr_daily": rep.sr,
                        "n_eff": rep.n_eff,
                        "n_total": rep.n_total,
                        "sr0_eff": rep.sr0_eff,
                        "sr0_total": rep.sr0_total,
                        "dsr_eff": rep.dsr_eff,
                        "dsr_total": rep.dsr_total,
                        "conservative": rep.dsr_conservative,
                    }
                )
            synthetic: list[dict[str, object]] = []
            s1_cost = fit_cost(cost_samples(store)).get("S1", LinearCost(0.05, 0.0))
            for family in families if a.compare_budget_s > 0 else []:  # 0 skips the synthetic comparison
                land = planted_landscape(SPACES[family], seed=a.seed)
                rep2 = compare(SPACES[family], land, s1_cost, rows=rows, budget_s=a.compare_budget_s, seeds=[1, 2, 3])
                synthetic.append(rep2.summary())
            totals: dict[str, object] = {
                "run_id": run_id,
                "priority_below_normal": priority,
                "panel_load_s": round(load_s, 1),
                "wall_s": round(time.monotonic() - t0, 1),
                "cpu_s": round(time.process_time() - c0, 1),
                "peak_private_mb": peak // 2**20,
                "batches": len(ran),
                "refused": [b for b in batches if "refused" in b],
                "trials_added": trials_after - trials_before,
                "trials_expected_from_counts": expected,
                "accounting_gap": (trials_after - trials_before) - expected,
                "running_left": store.conn.execute("select count(*) from trial where status = 'running'").fetchone()[0],
                "cost_ratios": ratios_all,
                "cost_in_band_share": (in_band / len(ratios_all)) if ratios_all else None,
                "collector_before": before,
                "collector_after": collector_state(catalog),
                "bandit": bandit.snapshot(),
                "archive": archive.diversity(),
                "neff_groups": groups,
                "fidelity_s1_vs_s3": fidelity,
                "dsr_best_s3": dsr_rows,
                "synthetic_random_vs_tpe": synthetic,
                "memory_model": "not fitted (one panel size); the 2 GB research gate guards memory instead",
            }
        finally:
            store.close()
            catalog.close()
    _report(a.out, totals, batches)
    skip = ("bandit", "neff_groups", "synthetic_random_vs_tpe")
    print(json.dumps({k: v for k, v in totals.items() if k not in skip}, indent=1, default=str))
    return 0


def _i(b: dict[str, object], key: str) -> int:
    v = b.get(key, 0)
    return int(v) if isinstance(v, int | float) else 0


def _fmt(v: object) -> str:
    if isinstance(v, float):
        return "nan" if math.isnan(v) else f"{v:.4g}"
    return json.dumps(v, ensure_ascii=False, default=str)


def _row(cells: Sequence[object]) -> str:
    return "| " + " | ".join(c if isinstance(c, str) else _fmt(c) for c in cells) + " |"


def _report(path: Path, totals: dict[str, object], batches: list[dict[str, object]]) -> None:
    head_skip = ("bandit", "neff_groups", "synthetic_random_vs_tpe", "dsr_best_s3", "fidelity_s1_vs_s3", "cost_ratios")
    head = {k: v for k, v in totals.items() if k not in head_skip}
    lines = [
        "# Phase 4 실데이터 탐색 배치 측정 (tools/run_search.py)",
        "",
        "> 완료 기준 2개(비용 예측 대역 비율, 회계 누락 0)를 측정한 표입니다. **어떤 전략의 성과 증거도 아닙니다.**",
        "> 비용 모델은 ADR-021 사전값, 펀딩은 미모델(OPS-006). S3의 '검증 분할'은 확인 구간(2025-05~06)입니다.",
        "",
        f"- 합계: `{json.dumps(head, ensure_ascii=False, default=str)}`",
        f"- 비용 예측/실측 비율: `{_fmt(totals.get('cost_ratios'))}`",
        f"- 대역 [0.5, 1.5] 비율: `{_fmt(totals.get('cost_in_band_share'))}` (기준 0.9 이상)",
        f"- 회계: 추가 trial `{totals.get('trials_added')}`, 단계 합 `{totals.get('trials_expected_from_counts')}`, "
        f"누락 `{totals.get('accounting_gap')}` (기준 0), running 잔류 `{totals.get('running_left')}`",
        "",
        "## 배치",
        _row(
            [
                "계열",
                "탐색기",
                "제안",
                "S0",
                "S0 수락",
                "S0 중복",
                "S0 거부",
                "S1",
                "승격 top+rand",
                "S2",
                "S2 done",
                "S3",
                "Optuna 평가",
                "예측 s S1,S2",
                "실측 s S0,S1,S2,S3",
                "비율 S1,S2",
                "보상",
                "시간 s",
            ]
        ),
        "|" + "---|" * 18,
    ]
    for b in batches:
        if "refused" in b:
            lines.append(_row([b["family"], b["searcher"], f"거부: {b['refused']}", *([""] * 15)]))
            continue
        cost = cast(dict[str, float], b.get("cost_s", {}))
        pred = cast(dict[str, object], b.get("predicted_s", {}))
        ratio = cast(dict[str, object], b.get("ratio_pred_over_actual", {}))
        lines.append(
            _row(
                [
                    b["family"],
                    b["searcher"],
                    b["proposed"],
                    b["s0_trials"],
                    b["s0_accepted"],
                    b["s0_duplicates"],
                    b["s0_rejected"],
                    b["s1_trials"],
                    f"{b['promoted_top']}+{b['promoted_random']}",
                    b["s2_trials"],
                    b["s2_done"],
                    b["s3_trials"],
                    b.get("optuna_evaluations"),
                    f"{_fmt(pred.get('S1'))},{_fmt(pred.get('S2'))}",
                    ",".join(_fmt(cost.get(s)) for s in ("S0", "S1", "S2", "S3")),
                    f"{_fmt(ratio.get('S1'))},{_fmt(ratio.get('S2'))}",
                    b.get("reward"),
                    b.get("wall_s"),
                ]
            )
        )
    lines += ["", "## N_eff (ONC, 창 단위) / N_total(S1 포함)"]
    lines.append(_row(["창 from", "창 to", "S2+", "N_eff", "S1", "N_total", "평균 rho", "품질"]))
    lines.append("|" + "---|" * 8)
    for g in cast(list[dict[str, object]], totals.get("neff_groups", [])):
        keys = ("window_from_ns", "window_to_ns", "n_s2plus", "n_eff", "n_s1", "n_total", "avg_rho", "quality")
        lines.append(_row([g[k] for k in keys]))
    lines += ["", "## DSR 2회 보고 (창별 최고 S3, 기간 단위)"]
    lines.append(_row(["trial", "SR(일)", "N_eff", "N_total", "SR0 eff", "SR0 total", "DSR eff", "DSR total", "보수"]))
    lines.append("|" + "---|" * 9)
    for d in cast(list[dict[str, object]], totals.get("dsr_best_s3", [])):
        keys = ("trial", "sr_daily", "n_eff", "n_total", "sr0_eff", "sr0_total", "dsr_eff", "dsr_total", "conservative")
        lines.append(_row([d[k] for k in keys]))
    lines += ["", "## S1 대 S3 순위상관 (계열별)", _row(["계열", "쌍", "Spearman"]), "|---|---|---|"]
    for f in cast(list[dict[str, object]], totals.get("fidelity_s1_vs_s3", [])):
        lines.append(_row([f["family"], f["pairs"], f["rank_corr"]]))
    lines += ["", "## Random 대 TPE (합성 지형, 같은 비용 예산; 두 값을 보고만 함)"]
    lines.append(
        _row(
            [
                "계열",
                "시드",
                "Random 회수율",
                "TPE 회수율",
                "Random 평가",
                "TPE 평가",
                "Random distinct",
                "TPE distinct",
            ]
        )
    )
    lines.append("|" + "---|" * 8)
    for s in cast(list[dict[str, object]], totals.get("synthetic_random_vs_tpe", [])):
        r, t = cast(dict[str, object], s["random"]), cast(dict[str, object], s["tpe"])
        cells = [s["family"], s["seeds"], r["recovery"], t["recovery"], r["evaluations"], t["evaluations"]]
        lines.append(_row([*cells, r["distinct_points"], t["distinct_points"]]))
    lines += [
        "",
        "## 아카이브·bandit",
        f"- MAP-Elites: `{_fmt(totals.get('archive'))}`",
        f"- bandit 스냅샷: `{_fmt(totals.get('bandit'))}`",
        f"- 메모리 모델: {totals.get('memory_model')}",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
