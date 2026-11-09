"""Phase 4 step 3: the S1 stage records every IC evaluation as one S1 trial plus one feature_eval row."""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from quant_research.backtest.panel import Panel, PanelConfig, assemble, decision_grid, settlements
from quant_research.research.ledger import TrialLedger
from quant_research.research.store import ResearchStore
from quant_research.search.s0_stage import run_s0
from quant_research.search.s1 import S1Config
from quant_research.search.s1_stage import run_s1
from quant_research.strategy.space import SPACES, generate

M = 60 * 10**9
H = 60 * M
T0 = 1_767_225_600 * 10**9
CONFIG: dict[str, object] = {"data_version": "d1", "lag_versions": {"klines": 1}, "seed": 0}


@pytest.fixture(scope="module")
def panel() -> Panel:
    rng = np.random.default_rng(1)
    n = 8 * 1_440
    names = [f"S{k}USDT" for k in range(6)]
    frames: list[pl.DataFrame] = []
    for name in names:
        price = 100.0 * np.exp(np.cumsum(0.001 * rng.standard_normal(n)))
        frames.append(
            pl.DataFrame(
                {
                    "symbol": [name] * n,
                    "event_ns": T0 + np.arange(n) * M,
                    "open": price,
                    "high": price * 1.0005,
                    "low": price * 0.9995,
                    "close": price,
                    "volume": np.full(n, 10.0),
                    "quote_volume": np.full(n, 1_000.0),
                    "available_ns": T0 + np.arange(n) * M + 2 * M,
                }
            )
        )
    cfg = PanelConfig(H)
    frame = assemble(decision_grid(T0, T0 + (8 * 24 - 2) * H, cfg), names, pl.concat(frames), cfg)
    return Panel(frame, settlements(None, None), [("lake/rw.parquet", 1)], cfg)


@pytest.fixture
def ledger(tmp_path: Path) -> Generator[TrialLedger]:
    store = ResearchStore.open(tmp_path / "research.db")
    yield TrialLedger(store, env_id="ENV-T", code_sha="abc")
    store.close()


def _rows(ledger: TrialLedger, sql: str) -> list[tuple[object, ...]]:
    return [tuple(r) for r in ledger.store.conn.execute(sql)]


def test_every_s1_evaluation_is_one_trial_with_one_feature_eval_row(ledger: TrialLedger, panel: Panel) -> None:
    s0 = run_s0(ledger, generate(SPACES["mean_reversion"], seed=2, count=5), CONFIG)
    scored = run_s1(ledger, s0.accepted, panel, CONFIG, S1Config(horizon_bars=4))
    assert len(scored) == len(s0.accepted) == 5
    trials = _rows(ledger, "select trial_id, status, metrics_json from trial where stage = 'S1' order by trial_id")
    assert [t[1] for t in trials] == ["done"] * 5
    fe = _rows(ledger, "select trial_id, horizon_bars, samples, data_version, lag_versions_json, ic from feature_eval")
    assert sorted(int(str(r[0])) for r in fe) == [int(str(t[0])) for t in trials]
    assert {r[1] for r in fe} == {4}
    assert any(int(str(r[2])) > 0 for r in fe)
    # a window longer than the panel leaves no defined IC: recorded as samples 0 / ic NULL, not dropped
    assert all((int(str(r[2])) == 0) == (r[5] is None) for r in fe)
    assert json.loads(str(fe[0][4])) == {"klines": 1}
    m = json.loads(str(trials[0][2]))
    assert m["point"]["family"] == "mean_reversion" and "score" in m and m["horizon_bars"] == 4
    assert m["rows"] == panel.frame.height and m["ops"] > 0
    assert [s.trial_id for s in scored] == [int(str(t[0])) for t in trials]


def test_re_evaluating_the_same_candidate_is_another_trial(ledger: TrialLedger, panel: Panel) -> None:
    s0 = run_s0(ledger, generate(SPACES["trend"], seed=4, count=3), CONFIG)
    run_s1(ledger, s0.accepted[:1], panel, CONFIG, S1Config(horizon_bars=2))
    run_s1(ledger, s0.accepted[:1], panel, CONFIG, S1Config(horizon_bars=2))
    assert _rows(ledger, "select count(*) from trial where stage = 'S1'") == [(2,)]


def test_a_failing_evaluation_is_a_failed_trial_without_a_feature_row(ledger: TrialLedger, panel: Panel) -> None:
    s0 = run_s0(ledger, generate(SPACES["momentum"], seed=6, count=2), CONFIG)
    broken = Panel(panel.frame.drop("exec_open"), panel.funding, panel.files, panel.config)
    with pytest.raises(pl.exceptions.ColumnNotFoundError):
        run_s1(ledger, s0.accepted, broken, CONFIG, S1Config(horizon_bars=2))
    assert _rows(ledger, "select status from trial where stage = 'S1'") == [("failed",)]
    assert _rows(ledger, "select count(*) from feature_eval") == [(0,)]
