"""S1 stage (I/O shell around `search.s1`): one S1 trial and one `feature_eval` row per evaluation
(design §8: every fitness evaluation is a trial, re-evaluations included; ADR-023). A failing evaluation is
recorded as a failed trial by the ledger and the error propagates."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

from quant_research.backtest.panel import Panel
from quant_research.backtest.run import data_version
from quant_research.research.ledger import TrialLedger, TrialOrigin
from quant_research.search.cost_model import spec_ops
from quant_research.search.s0_stage import Accepted, point_json
from quant_research.search.s1 import S1Config, S1Result, evaluate_s1
from quant_research.strategy.canonical import canonical_tree, expanded_trees
from quant_research.strategy.space import SPACES, build_point


@dataclass(frozen=True, slots=True)
class S1Scored:
    candidate: Accepted
    result: S1Result
    trial_id: int


def s1_config(config: Mapping[str, object], cfg: S1Config) -> dict[str, object]:
    return {**config, "stage": "S1", "s1": asdict(cfg)}


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
        spec = build_point(SPACES[cand.point.family], cand.point)
        key = ledger.register_experiment(cand.spec_hash, spec.model_dump_json(), s1_config(config, cfg))
        signal = expanded_trees(spec)["signal"]
        feature = json.dumps(None if signal is None else canonical_tree(signal), sort_keys=True)
        with ledger.evaluate(key, cand.spec_hash, "S1", fold=fold, origin=origin) as trial:
            result = evaluate_s1(spec, panel, cfg)
            trial.metrics = {
                **result.metrics(),
                "point": point_json(cand.point),
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
