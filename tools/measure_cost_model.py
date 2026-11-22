"""v2.3 step 6 (ADR-049 decision 4, A7): held-out band of the cost models on the recorded trials. Read-only.

    uv run python tools/measure_cost_model.py [--report docs/measurements/v23-cost-model.md]

Trials of research.db (S1, S2; done or rejected, with rows / ops / cost_s) in recording order, grouped by batch
(fold). Each model is fitted on the first half of the batches and predicts every later batch's stage total; the
share of totals within [0.5, 1.5] is the completion measure (criterion 0.9, unchanged). Also per run id prefix.
A measurement, not a performance claim."""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from quant_research.capture.run import repo_root
from quant_research.config import load_settings
from quant_research.search.cost_model import BAND, CostSample, fit_cost, fit_cost_v2, holdout_band


def load_samples(db: Path) -> list[CostSample]:
    with closing(sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "select stage, metrics_json, cost_s, coalesce(fold, '') from trial where status in ('done', 'rejected') "
            "and cost_s is not null and metrics_json is not null and stage in ('S1', 'S2') order by trial_id"
        ).fetchall()
    out: list[CostSample] = []
    for stage, m, cost, fold in rows:
        body = json.loads(m)
        r, o = body.get("rows"), body.get("ops")
        if isinstance(r, int) and isinstance(o, int) and r > 0 and o > 0:
            out.append(CostSample(str(stage), r, o, float(cost), group=str(fold)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", type=Path, default=None)
    a = ap.parse_args()
    samples = load_samples(load_settings().data_root / "meta" / "research.db")
    groups = list(dict.fromkeys(s.group for s in samples))
    by_run: dict[str, list[CostSample]] = {}
    for s in samples:
        by_run.setdefault(s.group.split(":")[0] or "(none)", []).append(s)
    lines = [
        "# 비용 모형 v1 대 v2: 보류 대역 (tools/measure_cost_model.py)",
        "",
        "> research.db의 S1·S2 trial을 기록 순서대로 배치(fold)로 묶고, 앞 절반 배치로 적합해 뒤 절반 배치의 단계별"
        f" 합계를 예측했다. 대역 {list(BAND)} 안 비율, 기준 0.9(변경 없음). 측정 기록이며 성과 주장이 아니다.",
        "",
        f"- trial {len(samples)}건, 배치 {len(groups)}개",
        "",
        "| 범위 | trial | 배치 | v1 (rows x ops 선형) | v2 (패널 구간별 rows, 최근 가중) |",
        "|---|---|---|---|---|",
    ]
    scopes = [("전체", samples), *sorted(by_run.items())]
    for name, part in scopes:
        n_groups = len(set(s.group for s in part))
        if n_groups < 2:
            continue
        v1, v2 = holdout_band(part, fit_cost), holdout_band(part, fit_cost_v2)
        lines.append(f"| {name} | {len(part)} | {n_groups} | {v1:.3f} | {v2:.3f} |")
    text = "\n".join(lines) + "\n"
    print(text)
    out = a.report or repo_root() / "docs" / "measurements" / "v23-cost-model.md"
    out.write_bytes(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
