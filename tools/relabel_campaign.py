"""v2.4 step 2 (ADR-050, design v2.3 section 2.2): a stored campaign under the current cause rules. Read-only.

    uv run python tools/relabel_campaign.py [--run-id c1] [--report docs/measurements/v24-relabel.md]

Reads `run/campaign-<run>.json` and research.db (read-only), evaluates nothing and writes no trial. The report has
the old cause x new cause table, the S3 verdicts per recorded cause (why "S3 judged" and "unstable" differ) and,
for the `sparse` batches, the share of their S2 points whose longest window is beyond the ADR-051 lookback cap.
A measurement, not a performance claim."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import cast

from quant_research.capture.run import repo_root
from quant_research.config import load_settings
from quant_research.search.campaign import Cell, Relabelled, relabel
from quant_research.strategy.space import exceeds_cap

MIN_EXPOSURE = 0.05  # the campaign's S3 eligibility (CampaignConfig.min_exposure)


def s2_points(db: Path, run_id: str) -> dict[str, list[Mapping[str, float]]]:
    """fold -> the parameter points of its S2 trials (the metrics carry the point)."""
    with closing(sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "select fold, metrics_json from trial where stage = 'S2' and metrics_json is not null and fold like ?",
            (f"{run_id}:%",),
        ).fetchall()
    out: dict[str, list[Mapping[str, float]]] = {}
    for fold, raw in rows:
        point = cast(Mapping[str, object], json.loads(raw)).get("point")
        if isinstance(point, Mapping):
            params = cast(Mapping[str, object], point).get("params")
            if isinstance(params, Mapping):
                typed = cast(Mapping[str, object], params)
                out.setdefault(str(fold), []).append(
                    {str(k): float(v) for k, v in typed.items() if isinstance(v, int | float)}
                )
    return out


def batch_folds(run_id: str, history: Sequence[Mapping[str, object]]) -> list[str]:
    """The ledger fold of every batch: `<run>:<cell>:<pull>`, pull = that cell's earlier batches."""
    seen: Counter[str] = Counter()
    folds: list[str] = []
    for h in history:
        cell = str(h["cell"])
        folds.append(f"{run_id}:{cell}:{seen[cell]}")
        seen[cell] += 1
    return folds


def render(run_id: str, rows: Sequence[Relabelled], over_cap: Mapping[str, tuple[int, int]]) -> str:
    old = sorted({r.old for r in rows})
    new = sorted({r.new for r in rows})
    cross = Counter((r.old, r.new) for r in rows)
    lines = [
        f"# 캠페인 {run_id} 재집계: 라벨 v3 (ADR-050)",
        "",
        "> 저장된 배치 요약에 현재 라벨 함수만 다시 적용했다. 평가를 다시 하지 않았고 trial은 늘지 않았다.",
        "> 이 표는 측정이고 성과 주장이 아니다.",
        "",
        f"- 배치 수: {len(rows)}",
        f"- S3 판정 수: {sum(r.s3_judged for r in rows)} (확인 SR 양수 {sum(1 for r in rows if (r.s3_sr or 0) > 0)})",
        "",
        "## 기록된 원인별 라벨 v3",
        "",
        "| 기록된 원인 | " + " | ".join(new) + " | 합계 |",
        "|---|" + "---|" * (len(new) + 1),
    ]
    for o in old:
        counts = [cross[(o, n)] for n in new]
        lines.append(f"| {o} | " + " | ".join(str(c) for c in counts) + f" | {sum(counts)} |")
    totals = [sum(cross[(o, n)] for o in old) for n in new]
    lines.append("| 합계 | " + " | ".join(str(c) for c in totals) + f" | {sum(totals)} |")
    lines += [
        "",
        "## S3 판정이 있었던 배치 (기록된 원인별)",
        "",
        "| 기록된 원인 | S3 판정 | 확인 SR 양수 |",
        "|---|---|---|",
    ]
    for o in old:
        judged = [r for r in rows if r.old == o and r.s3_judged]
        lines.append(f"| {o} | {len(judged)} | {sum(1 for r in judged if (r.s3_sr or 0) > 0)} |")
    reasons = Counter(r.reason for r in rows if r.reason is not None)
    lines += ["", "## `below_bar` 하위 사유", ""]
    lines += [f"- {reason}: {n}" for reason, n in sorted(reasons.items())] or ["- 없음"]
    lines += [
        "",
        "## `sparse` 배치와 lookback 상한 (ADR-051)",
        "",
        "> 신호 창 매개변수만 본다. 비중의 변동성 창과 평활은 세지 않으므로 실제 초과 비율은 이보다 높을 수 있다.",
        "",
        "| 셀 | sparse 배치의 S2 점 | 상한 초과 | 비율 |",
        "|---|---|---|---|",
    ]
    for cell, (n, over) in sorted(over_cap.items()):
        lines.append(f"| {cell} | {n} | {over} | {over / n:.2f} |" if n else f"| {cell} | 0 | 0 | — |")
    n_all = sum(n for n, _ in over_cap.values())
    over_all = sum(o for _, o in over_cap.values())
    lines.append(f"| 합계 | {n_all} | {over_all} | {over_all / n_all:.2f} |" if n_all else "| 합계 | 0 | 0 | — |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", default="c1")
    ap.add_argument("--report", type=Path, default=None)
    a = ap.parse_args()
    root = load_settings().data_root
    state = json.loads((root / "run" / f"campaign-{a.run_id}.json").read_text(encoding="utf-8"))["state"]
    history = cast(list[Mapping[str, object]], state["history"])
    rows = relabel(history, min_exposure=MIN_EXPOSURE)
    points = s2_points(root / "meta" / "research.db", a.run_id)
    over_cap: dict[str, tuple[int, int]] = {}
    for row, fold in zip(rows, batch_folds(a.run_id, history), strict=True):
        if row.new != "sparse":
            continue
        cell = Cell.parse(row.cell)
        batch = points.get(fold, [])
        n, over = over_cap.get(row.cell, (0, 0))
        over_cap[row.cell] = (
            n + len(batch),
            over + sum(exceeds_cap(cell.family, cell.timeframe, p) for p in batch),
        )
    report = a.report or repo_root() / "docs" / "measurements" / "v24-relabel.md"
    report.write_bytes(render(a.run_id, rows, over_cap).encode("utf-8"))
    print(
        json.dumps(
            {
                "batches": len(rows),
                "s3_judged": sum(r.s3_judged for r in rows),
                "new_causes": dict(Counter(r.new for r in rows)),
                "report": str(report),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
