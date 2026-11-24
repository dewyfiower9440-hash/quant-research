"""v2.2 B7/B9 + v2.3: the autonomous research campaign (design delta D8, ADR-047, ADR-049) on the real lake.

    uv run python tools/run_campaign.py --run-id c2 --space-version 3 [--grid preregistered|cli]
        [--timeframes 4h 1d] [--top-m 100] [--windows w2] [--families ...] [--max-batches N] [--max-hours H]
        [--s1-count 60] [--top-k 5] [--polars-threads 2] [--log-file F] [--report F]

Cells = family x timeframe x top-M x research window (seal epoch 2). `--grid preregistered` (default) starts with
group G1 and opens rounds (ADR-049): when every cell is retired, data_missing cells whose data has arrived (catalog
research coverage) reopen and the next group is added; `--grid cli` runs only the --timeframes/--top-m/--windows
grid (still reopening). `--space-version 2` (default, campaign c1): the v2 spaces and the groups G1..G4.
`--space-version 3` (campaign c2, ADR-051..055): the capped spaces, funding required, and the round order G1', G2',
`combo`, `ridge`, 1h, top 30, then the regime-gated families; a state file never resumes under the other version.
Panels come from the panel store (B3), built on first use and reused (one panel
in memory at a time). State + bandit are saved after every batch and round to qr-data/run/campaign-<run>.json, so
a rerun with the same --run-id resumes where it stopped (the replayed batch reuses its trials). The S3 null N
counts the S3 runs of the other campaign files on the same splits. Stops: a round with nothing to open
(waiting_data), --max-batches / --max-hours, the research gate (memory, disk, SSD heat; exit 3) or the file
qr-data/run/STOP-campaign. The time budget is checked between batches: a tick that has to build a panel first can
run far past --max-hours (c2's first tick: 8 and 21 minutes for the two 1d top-100 windows, built in the batch).
At the end of a tick new candidates go to the validator outbox, and it writes the report (--report, default
docs/measurements/campaign-<run>.md) and the data request queue qr-data/run/data-queue.json.
Exit codes: 0 waiting_data, exhausted or budget, 3 stopped by the gate or the stop file, 2 another research process
holds run/research.lock, 1 error (unreadable state files, a resume with other settings)."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
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


def resumable(saved: Mapping[str, object], given: Mapping[str, object], first_group: Sequence[str]) -> bool:
    """A resume must keep what defines the batches (max_pulls, config) and the grid mode. A preregistered campaign
    may resume after families were added (they enter through the groups, ADR-049), so its saved grid only has to lie
    inside the current group G1; a state saved before grid modes existed (c1) counts as a cli grid that equals G1."""
    if any(saved.get(k) != given[k] for k in ("max_pulls", "config")):
        return False
    mode = saved.get("grid_mode", "cli")
    keys = set(first_group)
    saved_grid = {str(k) for k in cast(Sequence[object], saved.get("grid", []))}
    if given["grid_mode"] == "cli":
        return mode == "cli" and saved.get("grid") == given["grid"]
    return (mode == "preregistered" or "grid_mode" not in saved) and saved_grid <= keys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True, type=run_id)
    # ADR-049: 'preregistered' starts with group G1 and adds G2..G4 in later rounds; 'cli' runs only the grid below
    ap.add_argument("--grid", choices=("preregistered", "cli"), default="preregistered")
    ap.add_argument("--timeframes", nargs="*", default=["4h", "1d"], choices=sorted(TIMEFRAME_S))
    ap.add_argument("--top-m", nargs="*", type=int, default=[100])
    ap.add_argument("--windows", nargs="*", default=["w2"], choices=sorted(WINDOWS))
    ap.add_argument("--families", nargs="*", default=None)
    ap.add_argument("--max-batches", type=int, default=None)
    ap.add_argument("--max-hours", type=float, default=None)
    ap.add_argument("--max-pulls", type=int, default=3)
    ap.add_argument(
        "--space-version",
        type=int,
        choices=(2, 3),
        default=2,
        help="3: the capped spaces and the 90-day warm-up of ADR-051 (a new run id; c1 was run with 2)",
    )
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
    from quant_research.backtest.panel_store import DEFAULT_DATASETS, PanelStore, Window, load_universe_panel
    from quant_research.bulk.tiers import parse_state, unready_datasets, usable_datasets, visible_coverage
    from quant_research.capture.lock import AlreadyRunningError, InstanceLock
    from quant_research.capture.run import git_sha, repo_root
    from quant_research.catalog.coverage import research_coverage
    from quant_research.catalog.store import Catalog
    from quant_research.catalog.universe_load import PIT_RULE
    from quant_research.config import load_settings
    from quant_research.data.access import PointInTime
    from quant_research.research.guard import research_gate
    from quant_research.research.ledger import TrialLedger
    from quant_research.research.store import ResearchStore
    from quant_research.search.bandit import BanditPrior, FamilyBandit
    from quant_research.search.campaign import (
        CampaignState,
        Cell,
        negative_report,
        preregistered_groups,
        preregistered_groups_v3,
    )
    from quant_research.search.campaign_runner import (
        CampaignConfig,
        Rounds,
        SplitRule,
        earlier_histories,
        judged_s3_counts,
        pooled_power,
        run_campaign,
        split_bounds,
        split_window,
    )
    from quant_research.search.pipeline import panel_datasets
    from quant_research.strategy.templates import ALL_FAMILIES, base_family
    from quant_research.validator.process import submit_campaign_candidates
    from quant_research.validator.verdict import DecisionRule

    root = load_settings().data_root
    families = a.families or list(ALL_FAMILIES)
    unknown = sorted(set(families) - set(ALL_FAMILIES))
    if unknown:
        ap.error(f"unknown families {unknown}")
    state_path = root / "run" / f"campaign-{a.run_id}.json"
    # rewards are validation-split daily SRs (~0.01-0.1): a prior on that scale, no minimum-budget lottery
    prior = BanditPrior(mu0=0.0, k0=1.0, sigma=0.05, gamma=0.98, min_pulls=0.0)
    if a.grid == "preregistered":
        # ADR-055: a v3 campaign follows its own round order (model cells, regime gates); c1 keeps ADR-049's
        groups = preregistered_groups_v3(families) if a.space_version == 3 else preregistered_groups(families)
        grid = groups[0]
    else:
        grid = [Cell(f, tf, m, w) for w in a.windows for tf in a.timeframes for m in a.top_m for f in families]
        groups = [grid]
    config = CampaignConfig(
        seed=a.seed, s1_count=a.s1_count, top_k=a.top_k, max_batches=a.max_batches, space_version=a.space_version
    )
    split_rule = SplitRule.of(a.space_version)
    settings: dict[str, object] = {
        "grid": [c.key for c in grid],
        "grid_mode": a.grid,
        "max_pulls": a.max_pulls,
        "config": config.identity(),
    }
    if state_path.exists():
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        state = CampaignState.from_json(saved["state"])
        given = json.loads(json.dumps(settings))
        if not resumable(state.settings, given, [c.key for c in groups[0]]):
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
    loaded: dict[tuple[str, str, int, tuple[str, ...]], tuple[Panel, Panel]] = {}
    # v2.3 step 11: tiers still downloading are not used. The state is read at the start (an unreadable file refuses
    # the tick) and again for every panel and round, because a tier's download may start during a long tick; a read
    # that fails then stops the campaign as a transient failure (inside a batch a plain error would be recorded as
    # a cell error and retire the grid).
    tier_path = root / "run" / "data-queue-state.json"

    def read_unready() -> frozenset[str]:
        raw: Mapping[str, Mapping[str, object]] = (
            cast(Mapping[str, Mapping[str, object]], json.loads(tier_path.read_text(encoding="utf-8")))
            if tier_path.exists()
            else {}
        )
        return unready_datasets({str(k): parse_state(str(k), v) for k, v in raw.items()})

    def unready_now() -> frozenset[str]:
        try:
            return read_unready()
        except (ValueError, AttributeError, TypeError, OSError) as e:
            raise PermissionError(f"unreadable {tier_path.name}: {type(e).__name__}: {e}") from e

    try:
        print(json.dumps({"unready_tiers": sorted(read_unready())}))
    except (ValueError, AttributeError, TypeError, OSError) as e:
        print(json.dumps({"refused": f"unreadable {tier_path.name}: {type(e).__name__}: {e}"}))
        return 1

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
            # v2.3 step 7: metrics / spot only for the families that read them; step 11: only finished tiers
            datasets = usable_datasets(panel_datasets(base_family(cell.family)), unready_now(), keep=DEFAULT_DATASETS)
            key = (cell.window, cell.timeframe, cell.top_m, datasets)
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
                    datasets=datasets,
                )
                loaded[key] = split_window(universe.panel, window, rule=split_rule)
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
            last = s.history[-1] if s.history else {}
            print(
                json.dumps(
                    {"round": s.round, "batch": len(s.history), "cell": last.get("cell"), "cause": last.get("cause")}
                )
            )
            sys.stdout.flush()

        windows_ns = {w: (month_ns(*lo), month_ns(*hi)) for w, (lo, hi) in WINDOWS.items()}
        rounds = Rounds(
            groups,
            lambda: visible_coverage(research_coverage(catalog, windows_ns), unready_now()),
            on_start=loaded.clear,
        )
        # ADR-049 decision 2: the S3 null N of earlier campaigns on the same splits, one history per run id (a copy
        # such as campaign-c1-old.json must not count c1 twice; this run's own file is never counted)
        others: list[CampaignState] = []
        for other in sorted((root / "run").glob("campaign-*.json")):
            try:
                others.append(CampaignState.from_json(json.loads(other.read_text(encoding="utf-8"))["state"]))
            except (KeyError, ValueError, TypeError) as e:
                print(json.dumps({"refused": f"unreadable campaign file {other.name}: {type(e).__name__}: {e}"}))
                return 1  # the S3 null N would silently drop its runs
        prior_s3 = judged_s3_counts(earlier_histories(others, a.run_id))
        print(json.dumps({"prior_s3": prior_s3, "round": state.round}))
        # design v2.3 section 3: the pooled bar and its power at this tick's N (a report; the bar is unchanged)
        pooled_days = 0
        for lo, hi in WINDOWS.values():
            _sel, conf_from, end = split_bounds(Window(month_ns(*lo), month_ns(*hi)), rule=split_rule)
            pooled_days += (end - conf_from) // (86_400 * 10**9)
        n0, bar0, power0 = pooled_power(state.history, prior_s3, pooled_days)
        print(json.dumps({"pooled_n": n0, "pooled_days": pooled_days, "bar_sr_ann": round(bar0, 3), "power": power0}))

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
            rounds=rounds,
            prior_s3=prior_s3,
        )
        # v2.3 step 9 (B10): new candidates go to the validator outbox at the end of every tick (research.lock held)
        sent = submit_campaign_candidates(research, state.history, rule=DecisionRule(), now_ns=time.time_ns())
        print(json.dumps({"validator_submitted": sent.submitted, "validator_skipped": sent.skipped}))
    finally:
        research.close()
        catalog.close()
        lock.__exit__(None, None, None)
    report = negative_report(state) | {"status": outcome.status, "reason": outcome.reason, "ran": outcome.batches}
    n1, bar1, power1 = pooled_power(state.history, prior_s3, pooled_days)
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
        "## 다음 배치의 결합 기준과 검정력 (보고용, 기준선은 바꾸지 않는다)",
        "",
        f"- 누적 N(두 확인 구간, 다음 배치의 2건 포함): {n1}",
        f"- 결합 확인 구간: {pooled_days}일, 기준: 연 SR {bar1:.2f}",
        "",
        "| 참 연 SR | 통과 확률(iid 근사) |",
        "|---|---|",
        *(f"| {level:g} | {power:.2f} |" for level, power in power1),
        "",
        "## 배치",
        "",
        "> best S2의 손익 분해(자본 대비): 순수익 = 가격 손익 - 비용 + 펀딩. 펀딩이 음수면 지급이다.",
        "",
        "| # | 셀 | 원인 | 보상 | best S2 SR | 순수익 | 비용 | 펀딩 | S3 SR | 교차 S3 SR | 결합 SR | 결합 기준 "
        "| S3 trial | 오류 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, h in enumerate(state.history, 1):
        raw = h.get("summary")
        sm: dict[str, object] = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
        lines.append(
            f"| {i} | {md(h['cell'])} | {h['cause']} | {h['reward']} | {sm.get('best_sr_net')} "
            f"| {sm.get('best_total_return')} | {sm.get('best_cost_fraction')} | {sm.get('best_funding_fraction')} "
            f"| {sm.get('s3_sr')} "
            f"| {sm.get('cross_sr')} | {sm.get('pooled_sr')} | {sm.get('pooled_threshold')} "
            f"| {sm.get('s3_trial_id')} | {md(sm.get('error', ''))} |"
        )
    out.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    print(json.dumps({"status": outcome.status, "reason": outcome.reason, "report": str(out)}, ensure_ascii=False))
    return 0 if outcome.status in ("exhausted", "budget", "waiting_data") else 3


if __name__ == "__main__":
    raise SystemExit(main())
