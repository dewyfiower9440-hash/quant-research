"""Trial accounting (design v2.1 §8 invariant: every fitness evaluation is exactly one trial).

The trial row is written BEFORE the evaluation runs (status `running`) and closed exactly once:
`done`, `rejected` (refused by a rule, with its reason), `failed` (exception, re-raised) or `aborted`
(interrupted; or found still running when the single research process starts again). Re-evaluating
the same experiment is another trial: there are no free re-runs (optional stopping, §9-2).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Literal

from quant_research.research.store import ResearchStore

Stage = Literal["S0", "S1", "S2", "S3", "reference", "smoke"]


def experiment_key(spec_hash: str, config: Mapping[str, object]) -> str:
    """hash(normalized spec, data version, lag policy, seal epoch, engine/fidelity/cost settings, seed) (§10)."""
    body = json.dumps({"spec_hash": spec_hash, "config": config}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TrialOrigin:
    """Where an evaluation came from (design §8: model id and LLM training cutoff on every trial)."""

    model_id: str | None = None
    llm_cutoff: str | None = None
    hypothesis_id: int | None = None
    cycle_id: int | None = None


@dataclass
class TrialHandle:
    trial_id: int
    metrics: dict[str, object] = field(default_factory=lambda: dict[str, object]())
    rejected_reason: str | None = None

    def reject(self, reason: str) -> None:
        self.rejected_reason = reason


class TrialLedger:
    def __init__(
        self, store: ResearchStore, *, env_id: str, code_sha: str, now_ns: Callable[[], int] = time.time_ns
    ) -> None:
        self.store = store
        self.env_id = env_id
        self.code_sha = code_sha
        self.now_ns = now_ns

    def register_experiment(self, spec_hash: str, spec_json: str, config: Mapping[str, object]) -> str:
        key = experiment_key(spec_hash, config)
        self.store.conn.execute(
            "insert or ignore into experiment(experiment_key, spec_hash, spec_json, config_json, created_ns) "
            "values (?,?,?,?,?)",
            (key, spec_hash, spec_json, json.dumps(config, sort_keys=True), self.now_ns()),
        )
        return key

    def close_abandoned(self) -> int:
        """Close trials a dead process left `running`. Call once at start, under the research lock."""
        cur = self.store.conn.execute(
            "update trial set status = 'aborted', reason = 'process ended during the evaluation', finished_ns = ? "
            "where status = 'running'",
            (self.now_ns(),),
        )
        return cur.rowcount

    def _finish(self, trial_id: int, status: str, reason: str | None, metrics: Mapping[str, object] | None) -> None:
        started = int(
            self.store.conn.execute("select started_ns from trial where trial_id = ?", (trial_id,)).fetchone()[0]
        )
        finished = max(self.now_ns(), started)
        self.store.conn.execute(
            "update trial set status = ?, reason = ?, metrics_json = ?, finished_ns = ?, cost_s = ? where trial_id = ?",
            (
                status,
                reason,
                None if metrics is None else json.dumps(metrics, sort_keys=True),
                finished,
                (finished - started) / 1e9,
                trial_id,
            ),
        )

    @contextmanager
    def evaluate(
        self,
        experiment: str,
        spec_hash: str,
        stage: Stage,
        *,
        fold: str | None = None,
        origin: TrialOrigin | None = None,
    ) -> Generator[TrialHandle]:
        o = origin or TrialOrigin()
        cur = self.store.conn.execute(
            "insert into trial(experiment_key, spec_hash, stage, fold, status, env_id, code_sha, started_ns, "
            "model_id, llm_cutoff, hypothesis_id, cycle_id) values (?,?,?,?, 'running', ?,?,?,?,?,?,?)",
            (
                experiment,
                spec_hash,
                stage,
                fold,
                self.env_id,
                self.code_sha,
                self.now_ns(),
                o.model_id,
                o.llm_cutoff,
                o.hypothesis_id,
                o.cycle_id,
            ),
        )
        handle = TrialHandle(int(cur.lastrowid or 0))
        try:
            yield handle
        except Exception as e:
            self._finish(handle.trial_id, "failed", f"{type(e).__name__}: {e}"[:500], handle.metrics or None)
            raise
        except BaseException as e:  # KeyboardInterrupt, SystemExit: the evaluation did not complete
            self._finish(handle.trial_id, "aborted", type(e).__name__, handle.metrics or None)
            raise
        if handle.rejected_reason is not None:
            self._finish(handle.trial_id, "rejected", handle.rejected_reason, handle.metrics or None)
        else:
            self._finish(handle.trial_id, "done", None, handle.metrics)
