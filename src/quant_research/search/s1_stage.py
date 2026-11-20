"""S1 stage (I/O shell around `search.s1`): one S1 trial and one `feature_eval` row per evaluation
(design §8: every fitness evaluation is a trial, re-evaluations included; ADR-023). A failing evaluation is
recorded as a failed trial by the ledger and the error propagates."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import cast

from quant_research.backtest.panel import Panel
from quant_research.backtest.run import data_version
from quant_research.research.ledger import TrialLedger, TrialOrigin, experiment_key
from quant_research.search.cost_model import spec_ops
from quant_research.search.s0_stage import Accepted, point_json
from quant_research.search.s1 import S1Config, S1Result, evaluate_s1
from quant_research.strategy.canonical import canonical_tree, expanded_trees
from quant_research.strategy.space import build_point, space_of


@dataclass(frozen=True, slots=True)
class S1Scored:
    candidate: Accepted
    result: S1Result
    trial_id: int


def s1_config(config: Mapping[str, object], cfg: S1Config) -> dict[str, object]:
    return {**config, "stage": "S1", "s1": asdict(cfg)}


def panel_window(panel: Panel) -> list[int]:
    """[first, last] decision of a panel plus its evaluate-from boundary (0 when none)."""
    col = panel.frame["decision_ns"]
    lo, hi = col.min(), col.max()
    return [
        int(lo) if isinstance(lo, int) else 0,
        int(hi) if isinstance(hi, int) else 0,
        int(panel.evaluate_from_ns or 0),
    ]


def reuse_s1(
    ledger: TrialLedger,
    candidates: Sequence[Accepted],
    panel: Panel,
    config: Mapping[str, object],
    cfg: S1Config,
    *,
    fold: str | None = None,
) -> tuple[list[S1Scored], list[Accepted]]:
    """(reused, to evaluate): a candidate whose spec already has a done S1 trial in this fold on the same data
    version (feature_eval.data_version) and S1 config is reused; any other is evaluated again (a new trial)."""
    version = data_version(panel.files)
    window = panel_window(panel)
    reused: list[S1Scored] = []
    fresh: list[Accepted] = []
    for cand in candidates:
        key = experiment_key(cand.spec_hash, s1_config(config, cfg))
        rows = ledger.store.conn.execute(
            "select t.trial_id, t.metrics_json from trial t join feature_eval f using (trial_id) "
            "where t.experiment_key = ? and t.stage = 'S1' and t.status = 'done' "
            "and ((? is null and t.fold is null) or t.fold = ?) and f.data_version = ? order by t.trial_id",
            (key, fold, fold, version),
        ).fetchall()
        match = None  # same files AND the same slice (slice_panel keeps the file list): the first such trial
        for trial_id, raw in rows:
            metrics = cast(dict[str, object], json.loads(str(raw)))
            if metrics.get("window") == window:
                match = (int(trial_id), metrics)
                break
        if match is None:
            fresh.append(cand)
            continue
        reused.append(S1Scored(cand, S1Result.from_metrics(match[1]), match[0]))
    return reused, fresh


def run_s1(
    ledger: TrialLedger,
    accepted: Sequence[Accepted],
    panel: Panel,
    config: Mapping[str, object],
    cfg: S1Config,
    *,
    origin: TrialOrigin | None = None,
    fold: str | None = None,
) -> list[S1Scored]:
    out: list[S1Scored] = []
    version = data_version(panel.files)
    lags = json.dumps(config.get("lag_versions", {}), sort_keys=True)
    for cand in accepted:
        spec = build_point(space_of(cand.point.family), cand.point)
        key = ledger.register_experiment(cand.spec_hash, spec.model_dump_json(), s1_config(config, cfg))
        signal = expanded_trees(spec)["signal"]
        feature = json.dumps(None if signal is None else canonical_tree(signal), sort_keys=True)
        with ledger.evaluate(key, cand.spec_hash, "S1", fold=fold, origin=origin) as trial:
            result = evaluate_s1(spec, panel, cfg)
            trial.metrics = {
                **result.metrics(),
                "point": point_json(cand.point),
                "window": panel_window(panel),  # the reuse key with data_version (resume)
                "rows": panel.frame.height,  # cost model inputs (search.cost_model)
                "ops": spec_ops(spec),
            }
            ledger.store.conn.execute(
                "insert into feature_eval(trial_id, feature_json, horizon_bars, lag_versions_json, ic, ic_hac_t, "
                "samples, data_version, created_ns) values (?,?,?,?,?,?,?,?,?)",
                (
                    trial.trial_id,
                    feature,
                    cfg.horizon_bars,
                    lags,
                    result.ic_mean,
                    result.ic_hac_t,
                    result.ic_samples,
                    version,
                    ledger.now_ns(),
                ),
            )
        out.append(S1Scored(cand, result, trial.trial_id))
    return out
