"""Phase 3 step 7: unattended research cycles of the Director on real research-split data.

    uv run python tools/run_director.py [--cycles 10] [--seed 7] [--run-id ID] [--memory on|off]
        [--policy posterior|uniform] [--llm fake|claude-cli|claude-api] [--out PATH]

Data: the 8 large symbols already downloaded, 1h decisions; selection split 2025-01..04, confirmation split
2025-05..06, each with a 30-day warm-up (features only). Everything is research split (before the
2026-07-01 seal); the PIT API refuses the holdout.
LLM: `fake` (default) is free and offline. `claude-cli` runs the unmodified Claude Code CLI (`claude -p`) on
the user's own claude.ai subscription login (no API key; refuses if any API-billing variable is set; a usage
limit pauses the run). `claude-api` (paid per-token API) is refused: paid calls are not approved (2026-11-10).
Token limits per cycle and per call apply to every LLM.
Writes a measurement report (CPU, memory, trials, LLM tokens and cost per cycle). Makes no claim about any
strategy: the validator does not exist before Phase 5.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

ENV_ID = "ENV-2026.11.09"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT"]


def month_ns(y: int, m: int) -> int:
    return int(datetime(y, m, 1, tzinfo=UTC).timestamp()) * 10**9


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=10)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--memory", choices=["on", "off"], default="on")
    ap.add_argument("--policy", choices=["posterior", "uniform"], default="posterior")
    ap.add_argument("--llm", choices=["fake", "claude-cli", "claude-api"], default="fake")
    ap.add_argument("--max-total-usd", type=float, default=5.0)
    ap.add_argument("--universe-n", type=int, default=5, help="universe size of the specs (part of the experiment)")
    ap.add_argument("--search", choices=["grid", "space"], default="grid", help="space = Phase 4 pipeline (v2.2 A8)")
    ap.add_argument("--s1-count", type=int, default=40, help="space mode: S1 proposals per cycle")
    ap.add_argument("--top-k", type=int, default=3, help="space mode: S1 top-k promoted to S2")
    ap.add_argument("--out", type=Path, default=Path("docs/measurements/phase3-director-run.md"))
    a = ap.parse_args()
    os.environ.setdefault("POLARS_MAX_THREADS", "2")

    from quant_research.adapters.jobhost import memory_sample
    from quant_research.backtest.panel import PanelConfig, load_panel, slice_panel
    from quant_research.capture.lock import InstanceLock
    from quant_research.capture.run import git_sha, repo_root
    from quant_research.catalog.store import Catalog
    from quant_research.config import load_settings
    from quant_research.data.access import PointInTime
    from quant_research.director.budget import Budget
    from quant_research.director.director import Director, DirectorConfig
    from quant_research.director.llm import FakeLLM, LLMClient
    from quant_research.research.guard import research_gate
    from quant_research.research.ledger import TrialLedger
    from quant_research.research.store import ResearchStore
    from quant_research.strategy.ops import FUNDING, KLINES, MARK

    root = load_settings().data_root
    hour, day = 3_600 * 10**9, 86_400 * 10**9
    run_id = a.run_id or f"{a.llm}-s{a.seed}-{a.memory}-{a.policy}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
    llm: LLMClient
    if a.llm == "claude-api":
        print("refused: paid Claude API calls are not approved (use --llm claude-cli or fake)")
        return 2
    if a.llm == "claude-cli":
        from quant_research.director.claude_cli import ClaudeCliLLM

        llm = ClaudeCliLLM.create(root / "run" / "llm-cli")
    else:
        llm = FakeLLM(seed=a.seed)
    gate = research_gate(root)
    with InstanceLock(root / "run" / "research.lock"):
        catalog = Catalog.open(root / "meta" / "catalog.db")
        store = ResearchStore.open(root / "meta" / "research.db")
        try:
            spent = float(
                store.conn.execute("select coalesce(sum(usage_cost_usd), 0) from llm_call where billed = 1").fetchone()[
                    0
                ]
            )
            budget = Budget(max_total_usd=a.max_total_usd, spent_billed_usd=spent)
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
            data = ["klines", "mark"] + (["funding"] if panel.funding.height else [])
            director = Director(
                DirectorConfig(
                    run_id=run_id,
                    seed=a.seed,
                    memory=a.memory == "on",
                    policy=a.policy,
                    universe_n=a.universe_n,
                    search=a.search,
                    s1_count=a.s1_count,
                    top_k=a.top_k,
                ),
                ledger=TrialLedger(store, env_id=ENV_ID, code_sha=git_sha(repo_root())),
                catalog=catalog,
                selection=selection,
                confirmation=confirmation,
                llm=llm,
                budget=budget,
                gate=gate,
                data_available=data,
            )
            rows: list[dict[str, object]] = []
            peak = memory_sample()["private"]
            for seq in range(1, a.cycles + 1):
                w, c = time.monotonic(), time.process_time()
                res = director.run_cycle(seq)
                peak = max(peak, memory_sample()["private"])
                cyc = res.cycle_id
                q = store.conn.execute
                llm_row = q(
                    "select count(*), coalesce(sum(input_tokens),0), coalesce(sum(output_tokens),0), "
                    "coalesce(sum(estimate_usd),0), coalesce(sum(usage_cost_usd),0), coalesce(sum(billed),0) "
                    "from llm_call where cycle_id = ?",
                    (cyc,),
                ).fetchone()
                trials = q(
                    "select stage, status, count(*) from trial where cycle_id = ? group by 1, 2", (cyc,)
                ).fetchall()
                family = q("select family from hypothesis where cycle_id = ?", (cyc,)).fetchone()
                causes = [r[0] for r in q("select cause_code from failure where cycle_id = ?", (cyc,))]
                dup = q(
                    "select evidence_json from transition where cycle_id = ? and from_state = 'SEARCH'", (cyc,)
                ).fetchone()
                skipped = len(json.loads(dup[0]).get("duplicates_skipped", {})) if dup else 0
                rows.append(
                    {
                        "seq": seq,
                        "status": res.status,
                        "family": None if family is None else family[0],
                        "path": " > ".join(res.states),
                        "trials": {f"{s}:{st}": n for s, st, n in trials},
                        "duplicates_skipped": skipped,
                        "causes": ", ".join(str(c) for c in causes),
                        "llm_calls": llm_row[0],
                        "tokens_in": llm_row[1],
                        "tokens_out": llm_row[2],
                        "estimate_usd": round(float(llm_row[3]), 4),
                        "usage_cost_usd": round(float(llm_row[4]), 4),
                        "billed_calls": llm_row[5],
                        "wall_s": round(time.monotonic() - w, 2),
                        "cpu_s": round(time.process_time() - c, 2),
                    }
                )
                if res.status == "paused":
                    break
            totals: dict[str, object] = {
                "run_id": run_id,
                "llm": llm.model_id,
                "billed": llm.billed,
                "cycles": len(rows),
                "done": sum(r["status"] == "done" for r in rows),
                "panel_load_s": round(load_s, 1),
                "wall_s": round(time.monotonic() - t0, 1),
                "cpu_s": round(time.process_time() - c0, 1),
                "peak_private_mb": peak // 2**20,
                "trials": store.conn.execute(
                    "select count(*) from trial t join cycle c using (cycle_id) where c.run_id = ?", (run_id,)
                ).fetchone()[0],
                "done_duplicates": store.conn.execute(
                    "select count(*) from (select experiment_key from trial where status = 'done' "
                    "group by experiment_key, stage having count(*) > 1)"
                ).fetchone()[0],
                "running_left": store.conn.execute("select count(*) from trial where status = 'running'").fetchone()[0],
                "data_available": data,
                "billed_usd_total_all_runs": round(budget.spent_billed_usd, 4),
            }
        finally:
            store.close()
            catalog.close()
    _report(a.out, totals, rows)
    print(json.dumps(totals, indent=1))  # CLI output
    return 0


def _report(path: Path, totals: dict[str, object], rows: list[dict[str, object]]) -> None:
    billed = totals.get("billed") is True
    cost_note = (
        "> 유료 API 호출입니다. `사용량 $`는 응답 사용량에 공식 정가를 곱한 청구 가능 비용입니다(청구서 대조 전)."
        if billed
        else f"> LLM `{totals.get('llm')}`: API 청구가 없습니다(가짜 LLM 또는 구독 CLI). `예상 $`와 `사용량 $`는 같은 "
        "토큰을 claude-opus-5-5 정가로 환산한 **추정치**이며 청구액이 아닙니다."
    )
    lines = [
        "# Phase 3 Director 실행 측정 (tools/run_director.py)",
        "",
        cost_note,
        "> 이 결과는 자율 연구 능력의 증명이 아니고 전략 성과의 증거도 아닙니다.",
        "> 목적은 연구 루프, 시도 회계, Memory 재사용, 자원 사용량을 확인하는 것입니다.",
        "",
        f"- 합계: `{json.dumps(totals, ensure_ascii=False)}`",
        "",
        "| # | 상태 | 계열 | 경로 | trial | 중복 생략 | 원인 | LLM 호출 | 입력/출력 토큰 | 예상 $ "
        "| 사용량 $ | 시간 s | CPU s |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['seq']} | {r['status']} | {r['family']} | {r['path']} | {json.dumps(r['trials'])} | "
            f"{r['duplicates_skipped']} | {r['causes']} | "
            f"{r['llm_calls']} | {r['tokens_in']}/{r['tokens_out']} | {r['estimate_usd']} | {r['usage_cost_usd']} | "
            f"{r['wall_s']} | {r['cpu_s']} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
