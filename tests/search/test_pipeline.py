"""Phase 4 step 10 (driver): S0 -> S1 -> promotion -> S2 -> S3 with one trial per evaluation, every proposal
told back to the searcher once, resume without duplicates, memory refusal before any trial."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from quant_research.backtest.panel import Panel, PanelConfig, assemble, decision_grid, settlements, slice_panel
from quant_research.catalog.bootstrap import bootstrap
from quant_research.catalog.store import Catalog
from quant_research.research.ledger import TrialLedger
from quant_research.research.store import ResearchStore
from quant_research.search.archive import Archive
from quant_research.search.bandit import BanditPrior, FamilyBandit
from quant_research.search.cost_model import LinearMemory, MemoryBudgetExceeded
from quant_research.search.optimizers import RandomSearcher, TpeSearcher
from quant_research.search.pipeline import SearchBudget, SearchResult, point_from_json, point_json, run_family_search
from quant_research.search.s1 import S1Config
from quant_research.strategy.space import SPACES

M = 60 * 10**9
H = 60 * M
D = 24 * H
T0 = 1_767_225_600 * 10**9
DAYS = 30
CONFIG: dict[str, object] = {"data_version": "d1", "lag_versions": {"klines": 1}, "seed": 0}


@pytest.fixture(scope="module")
def panels() -> tuple[Panel, Panel]:
    rng = np.random.default_rng(8)
    n = DAYS * 1_440
    names = [f"S{k}USDT" for k in range(4)]
    frames: list[pl.DataFrame] = []
    for k, name in enumerate(names):
        price = 100.0 * np.exp(np.cumsum(0.0004 * rng.standard_normal(n) + 0.00001 * (k - 1)))
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
    full = Panel(frame, settlements(None, None), [("lake/rw.parquet", 1)], cfg)
    return slice_panel(full, T0 + 7 * D, T0 + 20 * D, warmup_ns=7 * D), slice_panel(
        full, T0 + 20 * D + H, T0 + 29 * D, warmup_ns=7 * D
    )


@pytest.fixture
def env(tmp_path: Path) -> Generator[tuple[Catalog, TrialLedger]]:
    catalog = Catalog.open(tmp_path / "c.db")
    bootstrap(catalog)
    store = ResearchStore.open(tmp_path / "r.db")
    yield catalog, TrialLedger(store, env_id="ENV-T", code_sha="abc")
    store.close()
    catalog.close()


def _count(ledger: TrialLedger, stage: str, fold: str | None = None) -> int:
    if fold is None:
        return int(ledger.store.conn.execute("select count(*) from trial where stage = ?", (stage,)).fetchone()[0])
    return int(
        ledger.store.conn.execute("select count(*) from trial where stage = ? and fold = ?", (stage, fold)).fetchone()[
            0
        ]
    )


def test_every_stage_is_accounted_and_the_searcher_is_told_once_per_proposal(
    env: tuple[Catalog, TrialLedger], panels: tuple[Panel, Panel]
) -> None:
    catalog, ledger = env
    sel, conf = panels
    searcher = TpeSearcher(SPACES["mean_reversion"], seed=3, n_startup_trials=5)
    bandit = FamilyBandit(tuple(SPACES), BanditPrior(min_pulls=0.0))
    archive = Archive()
    res = run_family_search(
        ledger,
        "mean_reversion",
        searcher,
        selection=sel,
        confirmation=conf,
        catalog=catalog,
        config=CONFIG,
        s1=S1Config(horizon_bars=4),
        budget=SearchBudget(s1_count=12, top_k=2, promote_rate=0.2, s3_top=1),
        seed=1,
        fold="cycle1",
        bandit=bandit,
        archive=archive,
    )
    c = res.counts()
    assert c["proposed"] == c["s0_trials"] == 12 == _count(ledger, "S0")
    assert c["s1_trials"] == c["s0_accepted"] == _count(ledger, "S1", "cycle1")
    assert c["s2_trials"] == c["promoted_top"] + c["promoted_random"] == _count(ledger, "S2", "cycle1")
    assert c["s3_trials"] == min(1, c["s2_done"]) == _count(ledger, "S3", "cycle1")
    assert searcher.pending() == 0 and searcher.internal_evaluations() == 12  # Optuna evaluations = proposals
    assert res.reward is not None and res.reward >= 0.0 and bandit.arms["mean_reversion"].pulls == 1.0
    assert res.archive_offers["offered"] == c["s2_done"] + sum(1 for o in res.s3 if o.status == "done")
    s = res.summary()
    assert s["best_s2_trial"] is not None and point_from_json(s["best_point"]) == res.best_s2().source.candidate.point  # type: ignore[arg-type,union-attr]
    assert set(res.cost_s) == {"S0", "S1", "S2", "S3"}
    assert ledger.store.conn.execute("select count(*) from trial where status = 'running'").fetchone()[0] == 0


def test_resume_in_the_same_fold_reuses_finished_s2_and_s3_trials(
    env: tuple[Catalog, TrialLedger], panels: tuple[Panel, Panel]
) -> None:
    catalog, ledger = env
    sel, conf = panels

    def run() -> SearchResult:
        return run_family_search(
            ledger,
            "momentum",
            RandomSearcher(SPACES["momentum"], seed=5),  # same seed -> same proposals (a restart replays)
            selection=sel,
            confirmation=conf,
            catalog=catalog,
            config=CONFIG,
            s1=S1Config(horizon_bars=4),
            budget=SearchBudget(s1_count=8, top_k=2, promote_rate=0.0, s3_top=1),
            seed=2,
            fold="cycle7",
        )

    first = run()
    s2_after_first, s3_after_first = _count(ledger, "S2", "cycle7"), _count(ledger, "S3", "cycle7")
    s1_after_first = _count(ledger, "S1", "cycle7")
    assert len(first.s2) == s2_after_first and first.best_s2() is not None
    second = run()  # the restart: S0 refusals are new trials (duplicates), S1/S2/S3 results are reused
    assert _count(ledger, "S2", "cycle7") == s2_after_first and _count(ledger, "S3", "cycle7") == s3_after_first
    assert _count(ledger, "S1", "cycle7") == s1_after_first  # same fold + data version: no second evaluation
    assert _count(ledger, "S0") == 16
    # review finding 3: the restart used to end with 0 candidates (every proposal an S0 duplicate)
    b1, b2 = first.best_s2(), second.best_s2()
    assert b2 is not None and b1 is not None and b2.outcome.trial_id == b1.outcome.trial_id
    assert len(second.s1) == len(first.s1) and len(second.s2) == len(first.s2)
    dup = ledger.store.conn.execute(
        "select experiment_key, count(*) from trial where stage in ('S2','S3') and fold = 'cycle7' "
        "group by experiment_key, stage having count(*) > 1"
    ).fetchall()
    assert dup == []


def test_memory_refusal_happens_before_any_trial(env: tuple[Catalog, TrialLedger], panels: tuple[Panel, Panel]) -> None:
    catalog, ledger = env
    sel, conf = panels
    searcher = TpeSearcher(SPACES["trend"], seed=1)
    with pytest.raises(MemoryBudgetExceeded):
        run_family_search(
            ledger,
            "trend",
            searcher,
            selection=sel,
            confirmation=conf,
            catalog=catalog,
            config=CONFIG,
            s1=S1Config(),
            budget=SearchBudget(s1_count=5, memory_limit_mb=10.0),
            seed=0,
            memory_model=LinearMemory(500.0, 0.001),  # predicts well above 10 MB
        )
    assert ledger.store.conn.execute("select count(*) from trial").fetchone()[0] == 0
    assert searcher.asked() == 0
    with pytest.raises(RuntimeError):
        run_family_search(
            ledger,
            "trend",
            searcher,
            selection=sel,
            confirmation=conf,
            catalog=catalog,
            config=CONFIG,
            s1=S1Config(),
            budget=SearchBudget(s1_count=5),
            seed=0,
            gate=lambda: (False, "hot"),
        )
    assert ledger.store.conn.execute("select count(*) from trial").fetchone()[0] == 0
    with pytest.raises(ValueError):
        SearchBudget(promote_rate=1.5)
    assert point_from_json(point_json(searcher.ask())).family == "trend"


def test_a_duplicate_on_another_window_is_evaluated_again_not_reused(
    env: tuple[Catalog, TrialLedger], panels: tuple[Panel, Panel]
) -> None:
    """Review 2 finding 8: the reuse key ignored the slice window (slice_panel keeps the file list), so the same
    fold label on another selection window reused S1 results computed on other data."""
    catalog, ledger = env
    sel, conf = panels
    grid = sel.frame["decision_ns"].unique().sort().to_list()
    shorter = slice_panel(sel, int(grid[len(grid) // 3]), int(grid[-1]))

    def run(panel: Panel) -> SearchResult:
        return run_family_search(
            ledger,
            "momentum",
            RandomSearcher(SPACES["momentum"], seed=5),
            selection=panel,
            confirmation=conf,
            catalog=catalog,
            config=CONFIG,
            s1=S1Config(horizon_bars=4),
            budget=SearchBudget(s1_count=6, top_k=1, promote_rate=0.0, s3_top=0),
            seed=2,
            fold="same-label",
        )

    first = run(sel)
    before = _count(ledger, "S1", "same-label")
    second = run(shorter)
    assert len(second.s1) == len(first.s1) and len(first.s1) > 0
    assert _count(ledger, "S1", "same-label") == before + len(second.s1)  # every one evaluated again


def test_reuse_finds_the_trial_of_the_same_window_among_several(
    env: tuple[Catalog, TrialLedger], panels: tuple[Panel, Panel]
) -> None:
    """Review 3: the reuse lookup took the OLDEST trial and compared its window, so after W1 and W2 a restart on
    W2 evaluated everything again."""
    catalog, ledger = env
    sel, conf = panels
    grid = sel.frame["decision_ns"].unique().sort().to_list()
    w2 = slice_panel(sel, int(grid[len(grid) // 3]), int(grid[-1]))

    def run(panel: Panel) -> SearchResult:
        return run_family_search(
            ledger,
            "momentum",
            RandomSearcher(SPACES["momentum"], seed=5),
            selection=panel,
            confirmation=conf,
            catalog=catalog,
            config=CONFIG,
            s1=S1Config(horizon_bars=4),
            budget=SearchBudget(s1_count=6, top_k=1, promote_rate=0.0, s3_top=0),
            seed=2,
            fold="two-windows",
        )

    run(sel)
    run(w2)
    before = _count(ledger, "S1", "two-windows")
    again = run(w2)
    assert len(again.s1) > 0 and _count(ledger, "S1", "two-windows") == before  # W2's own trials reused
