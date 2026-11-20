"""S2 stage (I/O shell): run the vectorised backtest for every promoted S1 candidate (Phase 4 step 5).

Each promoted candidate is exactly one S2 trial (`run_backtest`), tagged with how it got there —
`top_k` or `promoted_random` (the seeded 5 % sample of S1 rejects, design §8 fidelity-bias control) — and
with the S1 trial it came from. `fidelity_by_family` later pairs S1 scores with S3 scores of the same spec
to measure the S1 filter's bias (Spearman rank correlation per family)."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import cast

from quant_research.backtest.panel import Panel
from quant_research.backtest.run import RunConfig, RunOutcome, run_backtest
from quant_research.catalog.store import Catalog
from quant_research.research.ledger import TrialLedger, TrialOrigin
from quant_research.search.cost_model import spec_ops
from quant_research.search.promotion import Selection, rank_corr
from quant_research.search.s0_stage import point_json
from quant_research.search.s1_stage import S1Scored
from quant_research.strategy.space import SPACES, build_point
from quant_research.strategy.spec import StrategySpec

S3_SCORE = "sr_daily"


@dataclass(frozen=True, slots=True)
class S2Scored:
    source: S1Scored
    promotion: str  # TOP_PROMOTION | RANDOM_PROMOTION
    outcome: RunOutcome


@dataclass(frozen=True, slots=True)
class FamilyFidelity:
    family: str
    pairs: int
    rank_corr: float | None  # Spearman of S1 score vs S3 score; None when undefined (< 3 pairs, no variation)


def promoted(selection: Selection) -> list[S1Scored]:
    """Everything that goes to S2: top candidates first, then the random promotions (stable order)."""
    return [*selection.top, *selection.promoted]


def s2_engine_config(spec: StrategySpec, config: RunConfig) -> RunConfig:
    """The vector engine refuses path-dependent exits (time stop / ATR stop); such specs go to the event engine
    at S2 so the search space's `time_stop` axis is evaluated instead of rejected (ADR-020 rule). The resume
    lookup must use the same config, or it never finds those trials (review)."""
    stateful = spec.exit.time_stop_bars is not None or spec.exit.stop_loss_atr is not None
    return replace(config, engine="event") if stateful and config.engine == "vector" else config


def run_s2(
    ledger: TrialLedger,
    selection: Selection,
    panel: Panel,
    catalog: Catalog,
    config: RunConfig,
    *,
    origin: TrialOrigin | None = None,
    fold: str | None = None,
) -> list[S2Scored]:
    out: list[S2Scored] = []
    for s in promoted(selection):
        kind = selection.kind(s.trial_id)
        spec = build_point(SPACES[s.candidate.point.family], s.candidate.point)
        cfg = s2_engine_config(spec, config)
        tags: dict[str, object] = {
            "engine_used": cfg.engine,
            "point": point_json(s.candidate.point),
            "promotion": kind,
            "promotion_seed": selection.seed,
            "promotion_rate": selection.promote_rate,
            "s1_trial_id": s.trial_id,
            "s1_score": s.result.score(),
            "rows": panel.frame.height,  # cost model inputs (search.cost_model)
            "ops": spec_ops(spec),
        }
        result = run_backtest(spec, panel, catalog, ledger, cfg, stage="S2", fold=fold, origin=origin, tags=tags)
        out.append(S2Scored(s, kind, result))
    return out


def fidelity_by_family(ledger: TrialLedger, *, s3_score: str = S3_SCORE) -> list[FamilyFidelity]:
    """S1 vs S3 rank correlation per family over specs with both a done S1 and a done S3 trial.

    The latest S1 and S3 trial of each spec hash is paired (re-evaluations are separate trials, §9-2);
    a pair whose S3 score is undefined (NaN / missing) is left out."""
    rows = ledger.store.conn.execute(
        "select s1.metrics_json, s3.metrics_json from "
        "(select spec_hash, max(trial_id) as t from trial where stage = 'S1' and status = 'done' group by spec_hash) a "
        "join trial s1 on s1.trial_id = a.t "
        "join (select spec_hash, max(trial_id) as t from trial where stage = 'S3' and status = 'done' "
        "group by spec_hash) b on b.spec_hash = s1.spec_hash "
        "join trial s3 on s3.trial_id = b.t order by s1.spec_hash"
    ).fetchall()
    by_family: dict[str, tuple[list[float], list[float]]] = {}
    for m1, m3 in rows:
        s1: dict[str, object] = json.loads(str(m1))
        s3: dict[str, object] = json.loads(str(m3))
        a, b = s1.get("score"), s3.get(s3_score)
        if not isinstance(a, int | float) or not isinstance(b, int | float) or a != a or b != b:
            continue
        point = s1.get("point")
        family = str(cast(dict[str, object], point).get("family", "?")) if isinstance(point, dict) else "?"
        xs, ys = by_family.setdefault(family, ([], []))
        xs.append(float(a))
        ys.append(float(b))
    return [FamilyFidelity(f, len(xs), rank_corr(xs, ys)) for f, (xs, ys) in sorted(by_family.items())]
