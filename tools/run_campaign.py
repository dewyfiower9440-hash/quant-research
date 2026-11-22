"""v2.2 B7/B9: the autonomous research campaign (design delta D8, ADR-047) on the real lake.

    uv run python tools/run_campaign.py --run-id c1 [--timeframes 4h 1d] [--top-m 100] [--windows w2]
        [--families ...] [--max-batches N] [--max-hours H] [--s1-count 60] [--top-k 5]
        [--polars-threads 2] [--log-file F]

Cells = family x timeframe x top-M x research window (seal epoch 2). Panels come from the panel store (B3), built
on first use and reused (one panel in memory at a time). State + bandit are saved after every batch to
qr-data/run/campaign-<run>.json, so a rerun with the same --run-id resumes where it stopped (the replayed batch
reuses its trials). Stops: no eligible cell (exhausted), --max-batches / --max-hours, the research gate (memory,
disk, SSD heat; exit 3) or the file qr-data/run/STOP-campaign. At the end it writes the report
docs/measurements/campaign-<run>.md and the data request queue qr-data/run/data-queue.json.
Exit codes: 0 exhausted or budget, 3 stopped by the gate or the stop file, 1 error."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

ENV_ID = "ENV-2026.11.09"
BELOW_NORMAL_PRIORITY_CLASS = 0x4000
WINDOWS = {"w1": ((2020, 1), (2023, 1)), "w2": ((2024, 12), (2026, 7))}
TIMEFRAME_S = {"1h": 3_600, "4h": 14_400, "1d": 86_400}


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


def run_id(text: str) -> str:
    """The run id names files under qr-data/run and docs/measurements: letters, digits, '-' and '_' only."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", text):
        raise argparse.ArgumentTypeError(f"run id must be 1-40 of [A-Za-z0-9_-], got {text!r}")
    return text


def md(value: object) -> str:
    """A Markdown table cell: cell keys contain '|'."""
    return str(value).replace("|", "\\|")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True, type=run_id)
    ap.add_argument("--timeframes", nargs="*", default=["4h", "1d"], choices=sorted(TIMEFRAME_S))
    ap.add_argument("--top-m", nargs="*", type=int, default=[100])
    ap.add_argument("--windows", nargs="*", default=["w2"], choices=sorted(WINDOWS))
    ap.add_argument("--families", nargs="*", default=None)
    ap.add_argument("--max-batches", type=int, default=None)
    ap.add_argument("--max-hours", type=float, default=None)
    ap.add_argument("--max-pulls", type=int, default=3)
    ap.add_argument("--s1-count", type=int, default=60)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--report", type=Path, default=None)
    # 4 threads ran c1 into the 2 GB gate (B9); without the flag an inherited POLARS_MAX_THREADS wins, else 2
    ap.add_argument("--polars-threads", type=int, default=None)
    ap.add_argument("--log-file", type=Path, default=None, help="append stdout and stderr here (pythonw has none)")
    a = ap.parse_args()
    if a.polars_threads is not None:  # before Polars is imported
        os.environ["POLARS_MAX_THREADS"] = str(a.polars_threads)
    else:
        os.environ.setdefault("POLARS_MAX_THREADS", "2")
    if a.log_file is not None:  # a scheduled run: refusals, gate stops and tracebacks must land somewhere
        a.log_file.parent.mkdir(parents=True, exist_ok=True)
        sink = a.log_file.open("a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = sink
        print(json.dumps({"start": datetime.now(UTC).isoformat(timespec="seconds"), "argv": sys.argv[1:]}))
    lower_priority()
    from quant_research.adapters.jobhost import write_json
    from quant_research.backtest.panel import Panel, PanelConfig
    from quant_research.backtest.panel_store import PanelStore, Window, load_universe_panel
    from quant_research.capture.lock import AlreadyRunningError, InstanceLock
    from quant_research.capture.run import git_sha, repo_root
    from quant_research.catalog.store import Catalog
    from quant_research.catalog.universe_load import PIT_RULE
    from quant_research.config import load_settings
    from quant_research.data.access import PointInTime
    from quant_research.research.guard import research_gate
    from quant_research.research.ledger import TrialLedger
    from quant_research.research.store import ResearchStore
    from quant_research.search.bandit import BanditPrior, FamilyBandit
    from quant_research.search.campaign import CampaignState, Cell, negative_report
    from quant_research.search.campaign_runner import CampaignConfig, run_campaign, split_window
    from quant_research.strategy.templates import ALL_FAMILIES

    root = load_settings().data_root
    families = a.families or list(ALL_FAMILIES)
    unknown = sorted(set(families) - set(ALL_FAMILIES))
    if unknown:
        ap.error(f"unknown families {unknown}")
    state_path = root / "run" / f"campaign-{a.run_id}.json"
    # rewards are validation-split daily SRs (~0.01-0.1): a prior on that scale, no minimum-budget lottery
    prior = BanditPrior(mu0=0.0, k0=1.0, sigma=0.05, gamma=0.98, min_pulls=0.0)
    grid = [Cell(f, tf, m, w) for w in a.windows for tf in a.timeframes for m in a.top_m for f in families]
    config = CampaignConfig(seed=a.seed, s1_count=a.s1_count, top_k=a.top_k, max_batches=a.max_batches)
    settings: dict[str, object] = {
        "grid": [c.key for c in grid],
        "max_pulls": a.max_pulls,
        "config": config.identity(),
    }
    if state_path.exists():
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        state = CampaignState.from_json(saved["state"])
        if state.settings != json.loads(json.dumps(settings)):
            print(
                json.dumps(
                    {"refused": "resume with other settings", "saved": state.settings, "given": settings},
                    ensure_ascii=False,
                )
            )
            return 1  # a resumed batch must propose the same points (no new evaluations, no mixed campaign)
        bandit = FamilyBandit.restore(tuple(c.key for c in state.all_cells()), prior, saved["bandit"])
        print(json.dumps({"resumed": a.run_id, "batches_done": len(state.history)}))
    else:
        state = CampaignState.new(a.run_id, grid, max_pulls=a.max_pulls)
        state.settings = json.loads(json.dumps(settings))
        bandit = FamilyBandit(tuple(c.key for c in state.all_cells()), prior)
    store = PanelStore(root / "cache" / "panels")
    gate0 = research_gate(root)
    stop_file = root / "run" / "STOP-campaign"
    deadline = None if a.max_hours is None else time.monotonic() + a.max_hours * 3_600
    loaded: dict[tuple[str, str, int], tuple[Panel, Panel]] = {}

    try:  # one research process at a time: close_abandoned() below assumes nobody else has running trials
        lock = InstanceLock(root / "run" / "research.lock").__enter__()
    except AlreadyRunningError as e:
        print(json.dumps({"refused": f"another research process holds run/research.lock: {e}"}))
        return 2
    catalog = Catalog.open(root / "meta" / "catalog.db")
    research = ResearchStore.open(root / "meta" / "research.db")
    try:
        ledger = TrialLedger(research, env_id=ENV_ID, code_sha=git_sha(repo_root()))
        ledger.close_abandoned()
        pit = PointInTime(catalog)

        def panels(cell: Cell) -> tuple[Panel, Panel]:
            key = (cell.window, cell.timeframe, cell.top_m)
            if key not in loaded:
                loaded.clear()  # one panel in memory at a time
                (y0, m0), (y1, m1) = WINDOWS[cell.window]
                window = Window(month_ns(y0, m0), month_ns(y1, m1))
                t0 = time.monotonic()
                universe = load_universe_panel(
                    pit,
                    root,
                    store,
                    window,
                    PanelConfig(TIMEFRAME_S[cell.timeframe] * 10**9),
                    top_m=cell.top_m,
                    rule=PIT_RULE,
                )
                loaded[key] = split_window(universe.panel, window)
                print(
                    json.dumps(
                        {
                            "panel": list(key),
                            "built": universe.built,
                            "reused": universe.reused,
                            "missing": list(universe.missing),
                            "rows": universe.panel.frame.height,
                            "s": round(time.monotonic() - t0, 1),
                        }
                    )
                )
                sys.stdout.flush()
            return loaded[key]

        def out_of_time() -> bool:
            return deadline is not None and time.monotonic() > deadline

        def persist(s: CampaignState, b: FamilyBandit) -> None:
            write_json(state_path, {"state": s.to_json(), "bandit": b.snapshot()})
            last = s.history[-1]
            print(json.dumps({"batch": len(s.history), "cell": last["cell"], "cause": last["cause"]}))
            sys.stdout.flush()

        outcome = run_campaign(
            state,
            bandit,
            ledger=ledger,
            catalog=catalog,
            panels=panels,
            config=config,
            gate=gate0,
            stop=stop_file.exists,
            persist=persist,
            out_of_time=out_of_time,
        )
    finally:
        research.close()
        catalog.close()
        lock.__exit__(None, None, None)
    report = negative_report(state) | {"status": outcome.status, "reason": outcome.reason, "ran": outcome.batches}
    write_json(root / "run" / "data-queue.json", {"run_id": a.run_id, "next_data": report["next_data"]})
    out = a.report or repo_root() / "docs" / "measurements" / f"campaign-{a.run_id}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# 캠페인 {a.run_id} (tools/run_campaign.py)",
        "",
        "> 연구 역할, 봉인 epoch 2 연구 창 안. 측정 기록이며 성과 주장이 아니다. 후보는 validator 판정 전이다.",
        "",
        "| 항목 | 값 |",
        "|---|---|",
        *(f"| {k} | {md(json.dumps(v, ensure_ascii=False))} |" for k, v in report.items()),
        "",
        "## 배치",
        "",
        "| # | 셀 | 원인 | 보상 | best S2 SR | S3 SR | S3 기준 | S3 trial | 오류 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, h in enumerate(state.history, 1):
        raw = h.get("summary")
        sm: dict[str, object] = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
        lines.append(
            f"| {i} | {md(h['cell'])} | {h['cause']} | {h['reward']} | {sm.get('best_sr_net')} | {sm.get('s3_sr')} "
            f"| {sm.get('s3_threshold')} | {sm.get('s3_trial_id')} | {md(sm.get('error', ''))} |"
        )
    out.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    print(json.dumps({"status": outcome.status, "reason": outcome.reason, "report": str(out)}, ensure_ascii=False))
    return 0 if outcome.status in ("exhausted", "budget") else 3


if __name__ == "__main__":
    raise SystemExit(main())
