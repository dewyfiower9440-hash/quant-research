"""v2.5 steps 5-6 (ADR-056): recovery of a planted alpha through the whole campaign funnel.

    uv run python tools/measure_funnel_power.py [--z 6,4] [--null] [--reps 43] [--max-minutes 18]
        [--max-batches N] [--state C:/dev/qr-data/run/funnel-power.jsonl]
        [--report C:/dev/qr-data/run/funnel-power.md]

One replication of an (alpha, z) pair = three independent synthetic DAILY markets with that alpha planted (shaped
like w2, like w1, and a holdout as long as H_hist), rebuilt from real research-window returns of 100 symbols
(stationary block bootstrap, per-symbol demeaned). Each of the pair's cells runs ONE campaign batch on the w2
market with the real runner at campaign c2's budget (space version 3, 60 S1 proposals, top 5 + 5 % to S2, the 5 %
exposure floor, S3, the cross confirmation on the w1 market, the pooled best-of-N bar). A batch that ends as a
candidate is then judged on the synthetic holdout by the validator's rule (ADR-042). The row keeps how far the
batch got (`validator.funnel_power.stages`).

The bar uses the N fixed when the measurement first started (decision 7) and the alpha strengths calibrated then;
both are kept in `<state>.setup.json`. Every replication uses its own temporary catalog / research.db: nothing is
written to the research databases and no holdout data is read. Finished rows are appended to `--state` and skipped
on the next call. A measurement, not a performance claim."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
import zlib
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

ENV_ID = "ENV-2026.11.09"
DAY_NS = 86_400 * 10**9
WARMUP_DAYS = 90  # ADR-051
SHAPES = {"w2": (330, 149), "w1": (700, 305)}  # selection, confirmation days of campaign c2's windows
HOLDOUT_DAYS = 700  # H_hist [2023-01, 2024-12)
BLOCK_DAYS = 5
# research window w1; the longest span for which 100 symbols have every month of klines (105 have these six)
BASE_FROM, BASE_TO = (2022, 4), (2022, 10)
SYMBOLS = 100
S1_COUNT, TOP_K, PROMOTE_RATE = 60, 5, 0.05  # the budget of campaign c2


def month_ns(y: int, m: int) -> int:
    return int(datetime(y, m, 1, tzinfo=UTC).timestamp()) * 10**9


def seed_of(*parts: object) -> int:
    return zlib.crc32("|".join(str(p) for p in parts).encode("utf-8")) % (2**31)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--z", default="6,4", help="oracle z values, in running order")
    ap.add_argument("--null", action="store_true", help="also (first) the null condition")
    ap.add_argument("--reps", type=int, default=None, help="replications per condition (default: ADR-056's 43)")
    ap.add_argument("--max-minutes", type=float, default=18.0)
    ap.add_argument("--max-batches", type=int, default=None, help="stop after this many new batches (timing run)")
    ap.add_argument("--seed", type=int, default=56)
    ap.add_argument("--state", type=Path, default=None)
    ap.add_argument("--report", type=Path, default=None)
    a = ap.parse_args()
    os.environ.setdefault("POLARS_MAX_THREADS", "2")
    os.environ.setdefault("NUMBA_NUM_THREADS", "2")

    import sqlite3
    from contextlib import closing

    from quant_research.backtest.panel import Panel, PanelConfig, load_panel, slice_panel
    from quant_research.catalog.bootstrap import bootstrap
    from quant_research.catalog.store import Catalog
    from quant_research.config import load_settings
    from quant_research.data.access import PointInTime
    from quant_research.research.ledger import TrialLedger
    from quant_research.research.store import ResearchStore
    from quant_research.search.bandit import BanditPrior, FamilyBandit
    from quant_research.search.campaign import CampaignState, Cell
    from quant_research.search.campaign_runner import CampaignConfig, judged_s3_counts, run_campaign
    from quant_research.search.pipeline import point_from_json
    from quant_research.strategy.ops import KLINES, MARK
    from quant_research.strategy.space import build_point, space_of
    from quant_research.validator.funnel_power import (
        REPS,
        Condition,
        conditions,
        null_conditions,
        render,
        row_of,
    )
    from quant_research.validator.null_pipeline import (
        SyntheticSpec,
        calibrate_strength,
        holdout_evaluation,
        hourly_closes,
        parse_alpha,
        synthetic_panel,
    )
    from quant_research.validator.verdict import DecisionRule

    reps = a.reps or REPS
    zs = [float(z) for z in a.z.split(",") if z]
    root = load_settings().data_root
    state_path = a.state or root / "run" / "funnel-power.jsonl"
    setup_path = state_path.with_suffix(".setup.json")
    # outside the repository while the measurement runs: a rewritten tracked file would void the gate evidence
    report = a.report or root / "run" / "funnel-power.md"
    rows: list[dict[str, object]] = []
    if state_path.exists():
        rows = [json.loads(line) for line in state_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    done = {(str(r["alpha"]), float(cast(float, r["z"])), str(r["family"]), int(cast(int, r["rep"]))) for r in rows}
    setup: dict[str, object] = json.loads(setup_path.read_text(encoding="utf-8")) if setup_path.exists() else {}
    design: dict[str, object] = {
        "seed": a.seed,
        "shapes": {w: list(v) for w, v in SHAPES.items()},
        "warmup_days": WARMUP_DAYS,
        "holdout_days": HOLDOUT_DAYS,
        "block_days": BLOCK_DAYS,
        "base": [list(BASE_FROM), list(BASE_TO)],
        "budget": [S1_COUNT, TOP_K, PROMOTE_RATE],
    }
    if setup and setup.get("design") != design:
        raise SystemExit(
            f"{setup_path} was fixed with another design or seed: {setup.get('design')} != {design}; "
            "a measurement is not continued under different settings (use another --state)"
        )
    setup["design"] = design
    deadline = time.monotonic() + a.max_minutes * 60.0

    # ---- the base: daily bars of the symbols that have every month of the span (returns to resample)
    months = [f"{y}-{m:02d}" for y in range(BASE_FROM[0], BASE_TO[0] + 1) for m in range(1, 13)]
    months = [m for m in months if f"{BASE_FROM[0]}-{BASE_FROM[1]:02d}" <= m < f"{BASE_TO[0]}-{BASE_TO[1]:02d}"]
    uri = f"file:{(root / 'meta' / 'catalog.db').as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        pairs = conn.execute(
            "select distinct symbol, month from bulk_file where dataset_id = ? and month >= ? and month <= ?",
            (KLINES, months[0], months[-1]),
        ).fetchall()
    per_symbol: dict[str, set[str]] = {}
    for s, m in pairs:
        per_symbol.setdefault(str(s), set()).add(str(m))
    complete = sorted(s for s, held in per_symbol.items() if held >= set(months))
    symbols = cast(list[str], setup.get("symbols")) or complete[:SYMBOLS]
    if len(symbols) < SYMBOLS:
        raise SystemExit(f"only {len(symbols)} symbols have every month of the base span; {SYMBOLS} are needed")
    catalog = Catalog.open(root / "meta" / "catalog.db")
    try:
        base = load_panel(
            PointInTime(catalog),
            root,
            symbols,
            first_bar_ns=month_ns(*BASE_FROM),
            last_bar_ns=month_ns(*BASE_TO) - DAY_NS,
            config=PanelConfig(DAY_NS),
            datasets=(KLINES, MARK),
        )
    finally:
        catalog.close()
    closes = hourly_closes(base)[2]

    # ---- fixed at the first start: the N of the bar (decision 7), the symbols, the alpha strengths
    if "start_n" not in setup:
        histories: list[list[dict[str, object]]] = []
        for run in ("c1", "c2"):
            raw = json.loads((root / "run" / f"campaign-{run}.json").read_text(encoding="utf-8"))
            histories.append(CampaignState.from_json(raw["state"]).history)
        setup["start_n"] = judged_s3_counts(histories)
        setup["symbols"] = symbols
        setup["fixed_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
    start_n = {str(k): int(v) for k, v in cast(Mapping[str, int], setup["start_n"]).items()}
    strengths = {str(k): float(v) for k, v in cast(Mapping[str, float], setup.get("strengths", {})).items()}
    wanted = conditions(zs)
    for alpha, z in dict.fromkeys((c.alpha, c.z) for c in wanted):
        key = f"{alpha}:{z:g}"
        if key not in strengths:
            strengths[key] = calibrate_strength(
                parse_alpha(alpha),
                z,
                closes,
                holdout_hours=HOLDOUT_DAYS,
                draws=8,
                seed=seed_of(a.seed, "calibration", key),
                block_len=BLOCK_DAYS,
            )
    setup["strengths"] = strengths
    setup_path.write_text(json.dumps(setup, indent=1), encoding="utf-8")

    config = CampaignConfig(
        seed=a.seed,
        s1_count=S1_COUNT,
        top_k=TOP_K,
        promote_rate=PROMOTE_RATE,
        s3_top=1,
        s3_random_rate=0.0,
        space_version=3,
        max_batches=1,
    )
    rule = DecisionRule()
    warm = WARMUP_DAYS * DAY_NS

    def market(spec: SyntheticSpec, seed: int, sel_days: int) -> tuple[Panel, Panel]:
        panel = synthetic_panel(base, spec, seed=seed, inputs=True)
        grid = sorted(set(panel.frame["decision_ns"].to_list()))
        t0 = int(grid[0]) + warm
        s1 = t0 + sel_days * DAY_NS
        return slice_panel(panel, t0, s1 - DAY_NS, warmup_ns=warm), slice_panel(
            panel, s1, int(grid[-1]) - DAY_NS, warmup_ns=warm
        )

    def holdout(spec: SyntheticSpec, seed: int, latency_ns: int | None = None) -> Panel:
        panel = synthetic_panel(base, spec, seed=seed, inputs=True, latency_ns=latency_ns)
        grid = sorted(set(panel.frame["decision_ns"].to_list()))
        return slice_panel(panel, int(grid[0]) + warm, int(grid[-1]) - DAY_NS, warmup_ns=warm)

    groups: list[tuple[str, float, list[Condition]]] = []
    if a.null:
        groups.append(("none", 0.0, null_conditions()))
    for alpha, z in dict.fromkeys((c.alpha, c.z) for c in wanted):
        groups.append((alpha, z, [c for c in wanted if (c.alpha, c.z) == (alpha, z)]))

    ran = 0
    spent: list[float] = [float(cast(float, r["seconds"])) for r in rows if r.get("seconds") is not None]
    work = Path(tempfile.mkdtemp(prefix="qr-funnel-power-"))

    def replicate(alpha: str, z: float, strength: float, rep: int, todo: list[Condition]) -> list[dict[str, object]]:
        """One replication of an (alpha, z) pair: its three markets, one batch per cell, the holdout verdict."""
        seed = seed_of(a.seed, alpha, z, rep)

        def spec_of(days: int) -> SyntheticSpec:
            return SyntheticSpec(alpha=parse_alpha(alpha), strength=strength, block_len=BLOCK_DAYS, hours=days + 1)

        markets = {
            w: market(spec_of(WARMUP_DAYS + sel + conf), seed + i, sel)
            for i, (w, (sel, conf)) in enumerate(SHAPES.items())
        }
        hold_spec = spec_of(WARMUP_DAYS + HOLDOUT_DAYS)
        hold = holdout(hold_spec, seed + 7)
        rep_dir = work / f"{alpha}-{z:g}-{rep}"
        rep_dir.mkdir(parents=True, exist_ok=True)
        cat = Catalog.open(rep_dir / "c.db")
        bootstrap(cat)
        store = ResearchStore.open(rep_dir / "r.db")
        out: list[dict[str, object]] = []
        try:
            ledger = TrialLedger(store, env_id=ENV_ID, code_sha="funnel-power")
            for cond in todo:
                t0 = time.perf_counter()
                cell = Cell(cond.family, "1d", 100, "w2")
                state = CampaignState.new(f"power-{alpha}-{z:g}-{rep}-{cond.family}", [cell], max_pulls=1)
                outcome = run_campaign(
                    state,
                    FamilyBandit(tuple(c.key for c in state.all_cells()), BanditPrior(min_pulls=0.0)),
                    ledger=ledger,
                    catalog=cat,
                    panels=lambda c: markets[c.window],
                    config=config,
                    gate=lambda: (True, ""),
                    stop=lambda: False,
                    persist=lambda _s, _b: None,
                    prior_s3=start_n,
                )
                if not state.history:  # the runner stopped before the batch (gate, memory): not a result
                    raise SystemExit(f"batch {cond} rep {rep} did not run: {outcome.status} ({outcome.reason})")
                h = state.history[0]
                summary = cast(Mapping[str, object], h["summary"])
                cause = str(h["cause"])
                validated: bool | None = None
                reasons: tuple[str, ...] = ()
                insufficient = False
                if cause == "candidate":
                    trial_id = int(cast(int, summary["s3_trial_id"]))
                    raw = store.conn.execute(
                        "select metrics_json from trial where trial_id = ?", (trial_id,)
                    ).fetchone()
                    point = point_from_json(json.loads(str(raw[0]))["point"])
                    verdict = holdout_evaluation(
                        build_point(space_of(point.family), point),
                        hold,
                        cat,
                        ledger,
                        rule,
                        in_sample_sr=float(cast(float, summary["pooled_sr"])),
                        seed=seed,
                        panel_at_latency=lambda lat: holdout(hold_spec, seed + 7, lat),
                    )
                    validated, reasons, insufficient = verdict.passed, verdict.reasons, verdict.insufficient
                seconds = round(time.perf_counter() - t0, 2)
                out.append(
                    row_of(
                        cond,
                        rep,
                        cause,
                        summary,
                        validated=validated,
                        insufficient=insufficient,
                        reasons=reasons,
                        seconds=seconds,
                    )
                )
        finally:
            store.close()
            cat.close()
            shutil.rmtree(rep_dir, ignore_errors=True)
        return out

    try:
        for alpha, z, cells in groups:
            strength = 0.0 if alpha == "none" else strengths[f"{alpha}:{z:g}"]
            for rep in range(reps):
                todo = [c for c in cells if (c.alpha, c.z, c.family, rep) not in done]
                out_of_budget = a.max_batches is not None and ran >= a.max_batches
                if not todo or out_of_budget or time.monotonic() > deadline:
                    continue
                new_rows = replicate(alpha, z, strength, rep, todo)
                with state_path.open("a", encoding="utf-8", newline="\n") as f:
                    for r in new_rows:
                        f.write(json.dumps(r) + "\n")
                rows.extend(new_rows)
                spent.extend(float(cast(float, r["seconds"])) for r in new_rows)
                ran += len(new_rows)
                done |= {(c.alpha, c.z, c.family, rep) for c in todo}
    finally:
        shutil.rmtree(work, ignore_errors=True)
    per_batch = sum(spent) / len(spent) if spent else None
    text = render(rows, start_n=start_n, strengths=strengths, seconds_per_batch=per_batch)
    report.write_bytes(text.encode("utf-8"))
    print(
        json.dumps(
            {
                "ran": ran,
                "batches": len(rows),
                "seconds_per_batch": None if per_batch is None else round(per_batch, 1),
                "candidates": sum(1 for r in rows if r["cause"] == "candidate"),
                "errors": sum(1 for r in rows if r["cause"] == "error"),
                "report": str(report),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
