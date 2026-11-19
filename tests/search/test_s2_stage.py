"""Phase 4 step 5: every promoted S1 candidate (top-k or the seeded 5 % random sample) is exactly one S2
trial tagged with its promotion kind, and the S1 vs S3 rank correlation per family can be read back."""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from quant_research.backtest.panel import Panel, PanelConfig, assemble, decision_grid, settlements
from quant_research.backtest.run import RunConfig, run_backtest
from quant_research.catalog.bootstrap import bootstrap
from quant_research.catalog.store import Catalog
from quant_research.research.ledger import TrialLedger
from quant_research.research.store import ResearchStore
from quant_research.search.promotion import RANDOM_PROMOTION, TOP_PROMOTION, select_for_s2
from quant_research.search.s0_stage import run_s0
from quant_research.search.s1 import S1Config
from quant_research.search.s1_stage import run_s1
from quant_research.search.s2_stage import fidelity_by_family, promoted, run_s2
from quant_research.strategy.space import SPACES, build_point, generate

M = 60 * 10**9
H = 60 * M
T0 = 1_767_225_600 * 10**9
DAYS = 12
CONFIG: dict[str, object] = {"data_version": "d1", "lag_versions": {"klines": 1}, "seed": 0}


@pytest.fixture(scope="module")
def panel() -> Panel:
    rng = np.random.default_rng(3)
    n = DAYS * 1_440
    names = [f"S{k}USDT" for k in range(4)]
    frames: list[pl.DataFrame] = []
    for k, name in enumerate(names):
        price = 100.0 * np.exp(np.cumsum(0.0005 * rng.standard_normal(n) + 0.00001 * (k - 1)))
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
                    "quote_volume": np.full(n, 1_000.0 * (k + 1)),
                    "available_ns": T0 + np.arange(n) * M + 2 * M,
                }
            )
        )
    cfg = PanelConfig(H)
    frame = assemble(decision_grid(T0, T0 + (DAYS * 24 - 2) * H, cfg), names, pl.concat(frames), cfg)
    return Panel(frame, settlements(None, None), [("lake/rw.parquet", 1)], cfg)


@pytest.fixture
def env(tmp_path: Path) -> Generator[tuple[Catalog, TrialLedger]]:
    catalog = Catalog.open(tmp_path / "c.db")
    bootstrap(catalog)
    store = ResearchStore.open(tmp_path / "r.db")
    yield catalog, TrialLedger(store, env_id="ENV-T", code_sha="abc")
    store.close()
    catalog.close()


def _trials(ledger: TrialLedger, stage: str) -> list[tuple[int, str, dict[str, object]]]:
    rows = ledger.store.conn.execute(
        "select trial_id, status, metrics_json from trial where stage = ? order by trial_id", (stage,)
    )
    return [(int(r[0]), str(r[1]), json.loads(r[2]) if r[2] else {}) for r in rows]


def test_every_promoted_candidate_is_one_tagged_s2_trial(env: tuple[Catalog, TrialLedger], panel: Panel) -> None:
    catalog, ledger = env
    s0 = run_s0(ledger, generate(SPACES["mean_reversion"], seed=11, count=8), CONFIG)
    scored = run_s1(ledger, s0.accepted, panel, CONFIG, S1Config(horizon_bars=4))
    sel = select_for_s2(scored, top_k=2, promote_rate=0.5, seed=5)
    assert sel.promoted, "the fixture must draw at least one random promotion"
    out = run_s2(ledger, sel, panel, catalog, RunConfig(engine="vector"))
    s2 = _trials(ledger, "S2")
    assert len(s2) == len(out) == len(sel.top) + len(sel.promoted)  # accounting: one trial per promotion
    assert [t[0] for t in s2] == [o.outcome.trial_id for o in out]
    by_s1 = {int(str(t[2]["s1_trial_id"])): t[2] for t in s2}
    for s in sel.top:
        assert by_s1[s.trial_id]["promotion"] == TOP_PROMOTION
    for s in sel.promoted:
        assert by_s1[s.trial_id]["promotion"] == RANDOM_PROMOTION
    for s in sel.rest:
        assert s.trial_id not in by_s1
    tagged = s2[0][2]
    assert tagged["promotion_seed"] == 5 and tagged["promotion_rate"] == 0.5
    assert tagged["point"]["family"] == "mean_reversion" and "sr_daily" in tagged  # type: ignore[index]
    assert [o.promotion for o in out] == [sel.kind(s.trial_id) for s in promoted(sel)]


def test_a_rejected_s2_run_still_carries_its_promotion_tag(env: tuple[Catalog, TrialLedger], panel: Panel) -> None:
    catalog, ledger = env
    s0 = run_s0(ledger, generate(SPACES["trend"], seed=12, count=3), CONFIG)
    scored = run_s1(ledger, s0.accepted, panel, CONFIG, S1Config(horizon_bars=2))
    sel = select_for_s2(scored, top_k=1, promote_rate=0.0, seed=0)
    out = run_s2(ledger, sel, panel, catalog, RunConfig(engine="vector", fidelity="L2"))  # L2 needs aggTrades
    assert [o.outcome.status for o in out] == ["rejected"]
    (trial,) = _trials(ledger, "S2")
    assert trial[1] == "rejected" and trial[2]["promotion"] == TOP_PROMOTION


def test_s1_vs_s3_rank_correlation_is_reported_per_family(env: tuple[Catalog, TrialLedger], panel: Panel) -> None:
    catalog, ledger = env
    s0 = run_s0(ledger, generate(SPACES["momentum"], seed=13, count=6), CONFIG)
    scored = run_s1(ledger, s0.accepted, panel, CONFIG, S1Config(horizon_bars=4))
    assert fidelity_by_family(ledger) == []  # no S3 yet
    for s in scored:
        run_backtest(
            build_point(SPACES["momentum"], s.candidate.point),
            panel,
            catalog,
            ledger,
            RunConfig(engine="vector"),
            stage="S3",
        )
    (fam,) = fidelity_by_family(ledger)
    assert fam.family == "momentum"
    s3 = _trials(ledger, "S3")
    defined = [t for t in s3 if t[1] == "done" and t[2]["sr_daily"] == t[2]["sr_daily"]]
    assert fam.pairs == len(defined) > 0
    assert fam.rank_corr is None or -1.0 <= fam.rank_corr <= 1.0
    # a re-evaluated S3 is a new trial and the pairing uses the latest one (same count, no duplicates)
    run_backtest(
        build_point(SPACES["momentum"], scored[0].candidate.point),
        panel,
        catalog,
        ledger,
        RunConfig(engine="vector"),
        stage="S3",
    )
    assert fidelity_by_family(ledger)[0].pairs == fam.pairs
