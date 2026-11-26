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
on the next call. A measurement, not a performance claim.

ADR-060 (the re-measurement after step 8a): `--space-version 4 --n-rule cell --null-reps 111 --seed 57` with
`--state .../funnel-power-2.jsonl`, and the "before" run of the four criterion cells with `--only-criterion
--state .../funnel-power-2-before.jsonl --setup .../funnel-power-2.setup.json` (one setup file for both: the
same N snapshot and alpha strengths). Such a run prints batch counts, failures and time only; the report is
written when both state files hold their whole registered design (the after run given `--before-state`, or
`--report-only --state <after> --before-state <before>`), to `<state>.md` unless `--report` says otherwise.
A command line that is none of the registered runs is refused; `--trial` runs it anyway, on a state file
of its own, without a report and without pass counts."""

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
    ap.add_argument("--null-reps", type=int, default=None, help="replications of the null (default: --reps)")
    ap.add_argument("--space-version", type=int, choices=(3, 4), default=3)
    ap.add_argument("--n-rule", choices=("raw", "cell"), default="raw")
    ap.add_argument("--only-criterion", action="store_true", help="only the cells the branch reads (ADR-060)")
    ap.add_argument("--setup", type=Path, default=None, help="setup file shared by two runs (ADR-060)")
    ap.add_argument("--before-state", type=Path, default=None, help="rows of the before run, for the report")
    ap.add_argument("--report-only", action="store_true", help="write the ADR-060 report, run nothing")
    ap.add_argument("--trial", action="store_true", help="an unregistered run: no report, no pass counts")
    a = ap.parse_args()
    os.environ.setdefault("POLARS_MAX_THREADS", "2")
    os.environ.setdefault("NUMBA_NUM_THREADS", "2")

    import sqlite3
    from contextlib import closing

    import numpy as np

    from quant_research.backtest.panel import Panel, PanelConfig, load_panel, slice_panel
    from quant_research.catalog.bootstrap import bootstrap
    from quant_research.catalog.store import Catalog
    from quant_research.config import load_settings
    from quant_research.data.access import PointInTime
    from quant_research.research.ledger import TrialLedger
    from quant_research.research.store import ResearchStore
    from quant_research.search.bandit import BanditPrior, FamilyBandit
    from quant_research.search.campaign import CampaignState, Cell
    from quant_research.search.campaign_runner import (
        CampaignConfig,
        earlier_pairs,
        judged_s3_counts,
        run_campaign,
    )
    from quant_research.search.multiplicity import expected_max_sr
    from quant_research.search.pipeline import point_from_json
    from quant_research.strategy.ops import KLINES, MARK
    from quant_research.strategy.space import build_point, space_of
    from quant_research.validator.funnel_power import (
        NULL_REPS_REMEASURE,
        REPS,
        VARIANTS,
        Condition,
        Key,
        RunKind,
        closing_line,
        conditions,
        criterion_conditions,
        key_of,
        null_conditions,
        oracle_passes,
        oracle_tail,
        render,
        render_remeasure,
        report_problems,
        row_of,
        run_kind,
        variant_problem,
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

    def read_rows(path: Path) -> list[dict[str, object]]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    zs = [float(z) for z in a.z.split(",") if z]
    root = load_settings().data_root
    state_path = a.state or root / "run" / "funnel-power.jsonl"
    setup_path = a.setup or state_path.with_suffix(".setup.json")
    rows = read_rows(state_path)
    setup: dict[str, object] = json.loads(setup_path.read_text(encoding="utf-8")) if setup_path.exists() else {}
    kind: RunKind
    if a.report_only:  # the report of ADR-060 from finished files: the variant is the after run's, not a flag
        kind, seed = "after", int(cast(int, cast(Mapping[str, object], setup.get("design", {})).get("seed", -1)))
    else:
        try:
            kind = run_kind(
                space_version=a.space_version,
                n_rule=a.n_rule,
                only_criterion=a.only_criterion,
                seed=a.seed,
                reps=a.reps,
                null_reps=a.null_reps,
                zs=zs,
                setup_given=a.setup is not None,
                trial=a.trial,
                null=a.null,
            )
        except ValueError as e:
            raise SystemExit(str(e)) from e
        seed = a.seed
    version, rule_name = VARIANTS.get(kind, (a.space_version, a.n_rule))
    variant: dict[str, object] = {"space_version": version, "n_rule": rule_name}
    if kind == "trial":  # marked on every row: a trial never shares a state file with a registered run
        variant["trial"] = True
    if (why := variant_problem(rows, kind, variant)) is not None:  # ADR-060 decision 5: never a mixed file
        raise SystemExit(f"{state_path}: {why}; use another --state")
    reps = a.reps or REPS
    null_reps = a.null_reps or (NULL_REPS_REMEASURE if kind == "after" else reps)
    # outside the repository while the measurement runs: a rewritten tracked file would void the gate evidence.
    # ADR-060 decision 1: a run of the re-measurement never writes the first measurement's report
    report = a.report or state_path.with_suffix(".md")  # (the first measurement's default is its funnel-power.md)
    done = {key_of(r) for r in rows}
    design: dict[str, object] = {
        "seed": seed,
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
    before_rows = [] if a.before_state is None else read_rows(a.before_state)

    def write_report() -> None:
        fixed_n = {str(k): int(v) for k, v in cast(Mapping[str, int], setup["start_n"]).items()}
        fixed = {str(k): float(v) for k, v in cast(Mapping[str, float], setup.get("strengths", {})).items()}
        seconds = [float(cast(float, r["seconds"])) for r in rows if r.get("seconds") is not None]
        text = render_remeasure(
            rows,
            before_rows,
            start_n=fixed_n,
            start_pairs=len(cast(list[str], setup["start_pairs"])),
            strengths=fixed,
            seconds_per_batch=sum(seconds) / len(seconds) if seconds else None,
        )
        report.write_bytes(text.encode("utf-8"))

    if a.report_only:
        if "start_pairs" not in setup:
            raise SystemExit(f"{setup_path} has no ADR-060 setup (start_pairs): nothing to report")
        if waiting := report_problems(rows, before_rows, seed=seed):  # decision 9: both runs whole, or no table
            raise SystemExit("the report of ADR-060 is not written yet: " + "; ".join(waiting))
        write_report()
        print(json.dumps({"report": str(report), "after": len(rows), "before": len(before_rows)}))
        return 0

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
        earlier: list[CampaignState] = []
        for run in ("c1", "c2"):
            raw = json.loads((root / "run" / f"campaign-{run}.json").read_text(encoding="utf-8"))
            earlier.append(CampaignState.from_json(raw["state"]))
        setup["start_n"] = judged_s3_counts([s.history for s in earlier])
        if kind != "first":  # ADR-060 decision 5: both Ns from the same campaign records, read once
            setup["start_pairs"] = sorted(earlier_pairs(earlier, "funnel-power"))
        setup["symbols"] = symbols
        setup["fixed_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
    start_n = {str(k): int(v) for k, v in cast(Mapping[str, int], setup["start_n"]).items()}
    if kind != "first" and "start_pairs" not in setup:  # a setup fixed by the first measurement: its N is older
        raise SystemExit(f"{setup_path} was fixed without the pairs of ADR-060; use another --state / --setup")
    start_pairs = frozenset(str(k) for k in cast(list[str], setup.get("start_pairs", [])))
    strengths = {str(k): float(v) for k, v in cast(Mapping[str, float], setup.get("strengths", {})).items()}
    wanted = criterion_conditions(zs) if a.only_criterion else conditions(zs)
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
        space_version=version,
        n_rule="cell" if rule_name == "cell" else "raw",
        max_batches=1,
    )
    rule = DecisionRule()
    warm = WARMUP_DAYS * DAY_NS

    def market(panel: Panel, sel_days: int) -> tuple[Panel, Panel]:
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

        markets: dict[str, tuple[Panel, Panel]] = {}
        confirmed: list[np.ndarray] = []
        planted = kind != "first" and alpha != "none"
        for i, (w, (sel, conf)) in enumerate(SHAPES.items()):
            full = synthetic_panel(base, spec_of(WARMUP_DAYS + sel + conf), seed=seed + i, inputs=True)
            markets[w] = market(full, sel)
            if planted:
                confirmed.append(oracle_tail(parse_alpha(alpha), hourly_closes(full)[2], conf))
            del full
        hold_spec = spec_of(WARMUP_DAYS + HOLDOUT_DAYS)
        hold = holdout(hold_spec, seed + 7)
        oracle: bool | None = None
        if planted:  # the same for every cell of the replication: the bar of a batch with nothing judged yet
            joined = np.concatenate(confirmed)
            n_bar = (len(start_pairs) if rule_name == "cell" else sum(start_n.values())) + 2
            held = synthetic_panel(base, hold_spec, seed=seed + 7, inputs=True)
            oracle = oracle_passes(
                joined,
                oracle_tail(parse_alpha(alpha), hourly_closes(held)[2], HOLDOUT_DAYS),
                bar=expected_max_sr(n_bar, 1.0 / joined.shape[0]),
                rule=rule,
            )
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
                    prior_pairs=start_pairs,
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
                sizing: str | None = None
                if kind != "first" and summary.get("s3_trial_id") is not None:  # ADR-059: which sizing was judged
                    found = store.conn.execute(
                        "select metrics_json from trial where trial_id = ?", (int(cast(int, summary["s3_trial_id"])),)
                    ).fetchone()
                    if found is not None and found[0] is not None:
                        struct = point_from_json(json.loads(str(found[0]))["point"]).struct_dict()
                        sizing = str(struct.get("sizing", "")).partition(":")[0] or None
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
                        variant=None if kind == "first" else variant,
                        sizing=sizing,
                        oracle=oracle,
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
            for rep in range(null_reps if alpha == "none" else reps):
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
    wanted_keys: list[Key] = [
        (c.alpha, c.z, c.family, rep)
        for alpha, _z, cells in groups
        for c in cells
        for rep in range(null_reps if alpha == "none" else reps)
    ]
    written: str | None = None
    waiting: list[str] = []
    if kind == "first":
        report.write_bytes(
            render(rows, start_n=start_n, strengths=strengths, seconds_per_batch=per_batch).encode("utf-8")
        )
        written = str(report)
    elif kind == "after":  # ADR-060 decision 9: the table only when both runs hold their whole design
        waiting = report_problems(rows, before_rows, seed=seed)
        if not waiting:
            write_report()
            written = str(report)
    print(
        json.dumps(
            closing_line(
                kind,
                rows,
                ran=ran,
                wanted=wanted_keys,
                seconds_per_batch=per_batch,
                report=written,
                waiting=waiting,
            )
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
