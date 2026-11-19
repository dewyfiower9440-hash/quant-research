"""Phase 4 step 7: N_eff groups from recorded S2/S3 pnl vectors, with N_total counting S1 trials too."""

from __future__ import annotations

import json
import zlib
from collections.abc import Generator
from pathlib import Path

import numpy as np
import pytest

from quant_research.research.ledger import Stage, TrialLedger
from quant_research.research.store import ResearchStore
from quant_research.search.neff_stage import group_summary, load_pnl_matrix, neff_groups

DAY = 86_400 * 10**9
W0, W1 = 1_767_225_600 * 10**9, 1_767_225_600 * 10**9 + 60 * DAY


@pytest.fixture
def ledger(tmp_path: Path) -> Generator[TrialLedger]:
    store = ResearchStore.open(tmp_path / "r.db")
    yield TrialLedger(store, env_id="ENV-T", code_sha="abc")
    store.close()


def _record(ledger: TrialLedger, stage: Stage, pnl: np.ndarray | None, *, window: tuple[int, int] = (W0, W1)) -> int:
    key = ledger.register_experiment(f"h{ledger.now_ns()}", "{}", {"n": ledger.now_ns()})
    with ledger.evaluate(key, key, stage) as trial:
        trial.metrics = {"sr_daily": 0.1}
        if pnl is not None:
            ledger.store.conn.execute(
                "insert into backtest_run(trial_id, engine, fidelity, data_version, lag_versions_json, "
                "seal_epochs_json, cost_json, window_from_ns, window_to_ns, pnl_daily, created_ns) "
                "values (?, 'vector', 'L1', 'd', '{}', '{}', '{}', ?, ?, ?, ?)",
                (trial.trial_id, window[0], window[1], zlib.compress(pnl.astype("<f8").tobytes()), 1),
            )
    return trial.trial_id


def test_groups_cluster_s2_plus_pnl_and_count_s1_in_n_total(ledger: TrialLedger) -> None:
    rng = np.random.default_rng(5)
    f = rng.standard_normal((60, 3))
    for b in range(3):
        for _ in range(4):
            _record(ledger, "S2" if b < 2 else "S3", f[:, b] + 0.4 * rng.standard_normal(60))
    for _ in range(7):
        _record(ledger, "S1", None)
    _record(ledger, "S2", rng.standard_normal(30), window=(W0, W0 + 30 * DAY))  # another window = another group
    groups = neff_groups(ledger, seed=1)
    assert [g.n_s2plus for g in groups] == [1, 12]
    big = groups[1]
    assert big.n_eff == 3 and big.n_s1 == 7 and big.n_total == 19
    assert len(big.trial_ids) == 12
    s = group_summary(big)
    assert s["n_total"] == 19 and json.dumps(s)  # serialisable for the measurement report
    small = group_summary(groups[0])
    assert small["n_eff"] == 1 and small["quality"] is None  # a single trial is its own cluster


def test_pnl_matrix_aligns_by_day_and_pads_with_nan(ledger: TrialLedger) -> None:
    _record(ledger, "S2", np.arange(60, dtype=float))
    _record(ledger, "S3", np.arange(40, dtype=float))
    ids, mat = load_pnl_matrix(ledger, window_from_ns=W0, window_to_ns=W1)
    assert len(ids) == 2 and mat.shape == (60, 2)
    assert np.isnan(mat[59, 1]) and mat[39, 1] == 39.0
    assert load_pnl_matrix(ledger, window_from_ns=1, window_to_ns=2)[1].shape == (0, 0)
