"""S0 stage of the Phase 4 search pipeline (design v2.1 §8): static check and canonical de-duplication.

Every generated candidate becomes exactly one S0 trial, in one transaction per batch:
- `done`: passed S0 and its canonical spec is new for this configuration (the experiment key);
- `rejected`: a constraint or S0 rule refused it (all reasons kept), or it is a canonical duplicate of a
  spec that already passed S0 (in this batch or an earlier one) — duplicates are counted, never skipped.

A constraint-rejected candidate has no spec; its experiment is keyed by the point instead
(`spec_hash = "point:" + sha256(point)`), so it still has a trial and can be traced."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from quant_research.research.ledger import TrialLedger, TrialOrigin
from quant_research.strategy.canonical import spec_hash
from quant_research.strategy.space import Candidate, Point


@dataclass(frozen=True, slots=True)
class Accepted:
    point: Point
    spec_hash: str
    experiment_key: str
    trial_id: int


@dataclass(frozen=True, slots=True)
class S0Outcome:
    generated: int
    unique_new: int
    duplicates: int
    rejected: int
    accepted: list[Accepted] = field(default_factory=lambda: list[Accepted]())
    statuses: list[str] = field(default_factory=lambda: list[str]())  # per candidate: done | rejected | duplicate


def point_json(point: Point) -> dict[str, object]:
    return {"family": point.family, "params": point.params_dict(), "struct": point.struct_dict()}


def _point_hash(point: Point) -> str:
    body = json.dumps(point_json(point), sort_keys=True, separators=(",", ":"))
    return "point:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _passed_before(ledger: TrialLedger, key: str) -> int | None:
    row = ledger.store.conn.execute(
        "select trial_id from trial where experiment_key = ? and stage = 'S0' and status = 'done' "
        "order by trial_id limit 1",
        (key,),
    ).fetchone()
    return None if row is None else int(row[0])


def run_s0(
    ledger: TrialLedger,
    candidates: Sequence[Candidate],
    config: Mapping[str, object],
    *,
    origin: TrialOrigin | None = None,
) -> S0Outcome:
    accepted: list[Accepted] = []
    statuses: list[str] = []
    duplicates = rejected = 0
    with ledger.store.transaction():
        for cand in candidates:
            metrics: dict[str, object] = {"point": point_json(cand.point)}
            if cand.spec is None:
                h = _point_hash(cand.point)
                key = ledger.register_experiment(h, json.dumps(metrics["point"], sort_keys=True), config)
            else:
                h = spec_hash(cand.spec)
                key = ledger.register_experiment(h, cand.spec.model_dump_json(), config)
            first = None if cand.spec is None else _passed_before(ledger, key)
            with ledger.evaluate(key, h, "S0", origin=origin) as trial:
                trial.metrics = metrics
                if cand.spec is None:
                    trial.reject("; ".join(cand.reasons) or "rejected without a reason")
                    rejected += 1
                    statuses.append("rejected")
                elif first is not None:
                    trial.reject(f"duplicate: canonical spec already passed S0 in trial {first}")
                    duplicates += 1
                    statuses.append("duplicate")
                else:
                    statuses.append("done")
            if cand.spec is not None and first is None:
                accepted.append(Accepted(cand.point, h, key, trial.trial_id))
    return S0Outcome(len(candidates), len(accepted), duplicates, rejected, accepted, statuses)
