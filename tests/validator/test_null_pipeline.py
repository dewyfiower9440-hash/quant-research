"""Phase 5 step 10: synthetic null / planted-alpha markets, three-way splits, and one end-to-end replication
(reduced search -> holdout verdict with stress re-runs) on a temporary catalog and ledger."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from quant_research.backtest.panel import Panel, PanelConfig, assemble, decision_grid, settlements
from quant_research.catalog.bootstrap import bootstrap
from quant_research.catalog.store import Catalog
from quant_research.research.ledger import TrialLedger
from quant_research.research.store import ResearchStore
from quant_research.search.pipeline import SearchBudget
from quant_research.strategy.templates import templates
from quant_research.validator.null_pipeline import (
    HarnessResult,
    Replication,
    SyntheticSpec,
    families_round_robin,
    holdout_evaluation,
    hourly_closes,
    incomplete_verdict,
    random_factory,
    register_and_judge,
    replicate,
    split_three,
    synthetic_panel,
    synthetic_returns,
    synthetic_splits,
)
from quant_research.validator.store import ValidatorStore
from quant_research.validator.verdict import DecisionRule, Verdict

M = 60 * 10**9
H = 60 * M
D = 24 * H
T0 = 1_767_225_600 * 10**9
DAYS = 40


@pytest.fixture(scope="module")
def base() -> Panel:
    rng = np.random.default_rng(9)
    n = DAYS * 1_440
    names = [f"S{k}USDT" for k in range(3)]
    frames: list[pl.DataFrame] = []
    for k, name in enumerate(names):
        price = 100.0 * np.exp(np.cumsum(0.0004 * rng.standard_normal(n)))
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


def test_synthetic_panel_keeps_the_grid_and_the_null_has_no_planted_structure(base: Panel) -> None:
    syn = synthetic_panel(base, SyntheticSpec(), seed=1)
    assert syn.frame["decision_ns"].n_unique() == base.frame["decision_ns"].n_unique()
    assert set(syn.frame["symbol"].unique().to_list()) == set(base.frame["symbol"].unique().to_list())
    assert syn.frame["complete"].all()
    again = synthetic_panel(base, SyntheticSpec(), seed=1)
    assert again.frame.equals(syn.frame)  # deterministic
    _, _, closes = hourly_closes(base)
    rng = np.random.default_rng(3)
    null = synthetic_returns(closes, SyntheticSpec(), rng)
    mech = synthetic_returns(closes, SyntheticSpec(alpha="mechanism", strength=0.6), np.random.default_rng(3))

    def ac(x: np.ndarray) -> float:
        return float(np.corrcoef(x[:-1, 0], x[1:, 0])[0, 1])

    assert abs(ac(null)) < 0.15 and ac(mech) < -0.3  # the mechanism plants negative lag-1 autocorrelation
    drift = synthetic_returns(closes, SyntheticSpec(alpha="random", strength=0.01), np.random.default_rng(3))
    assert drift.mean() > null.mean() + 0.001


def test_split_three_is_consecutive_and_warmup_rows_do_not_trade(base: Panel) -> None:
    sp = split_three(base, warmup_days=5, sel_days=15, conf_days=8)
    sel_last = int(sp.selection.frame.filter(pl.col("complete"))["decision_ns"].max())  # type: ignore[arg-type]
    conf_first = int(sp.confirmation.frame.filter(pl.col("complete"))["decision_ns"].min())  # type: ignore[arg-type]
    hold_first = int(sp.holdout.frame.filter(pl.col("complete"))["decision_ns"].min())  # type: ignore[arg-type]
    assert sel_last < conf_first < hold_first
    assert sp.selection.evaluate_from_ns is not None
    with pytest.raises(ValueError):
        split_three(base, warmup_days=5, sel_days=30, conf_days=10)


def test_one_replication_runs_search_and_holdout_verdict_with_stress_reruns(
    env: tuple[Catalog, TrialLedger], base: Panel
) -> None:
    catalog, ledger = env
    sp = synthetic_splits(base, SyntheticSpec(), seed=4, warmup_days=5, sel_days=14, conf_days=8)
    rule = DecisionRule(bootstrap_reps=200)
    rep = replicate(
        sp,
        catalog,
        ledger,
        family="mean_reversion",
        factory=random_factory,
        budget=SearchBudget(s1_count=6, top_k=1, promote_rate=0.0, s3_top=1),
        rule=rule,
        seed=4,
    )
    assert rep.family == "mean_reversion" and rep.searcher == "random"
    stages = dict(ledger.store.conn.execute("select stage, count(*) from trial group by stage").fetchall())
    assert stages["S0"] == 6 and stages["S1"] >= 1 and stages["S2"] >= 1
    if rep.s3_sr is not None:  # a completed S3: the holdout base run + 2 cost + 2 lag re-runs are all trials
        assert stages["reference"] == 5
        assert isinstance(rep.passed, bool)
    res = HarnessResult("null", (rep,))
    s = res.summary()
    assert s["replications"] == 1 and 0.0 <= float(str(s["pass_rate"])) <= 1.0
    lo, hi = res.wilson_ci()
    assert 0.0 <= lo <= hi <= 1.0
    assert families_round_robin(["a", "b"], 5) == ["a", "b", "a", "b", "a"]


def test_harness_verdicts_are_booked_in_a_validator_db(tmp_path: Path) -> None:
    store = ValidatorStore.open(tmp_path / "v.db")
    try:
        rule = DecisionRule()
        v = Verdict(False, ("H0 not rejected",), {"p": 0.3, "rule_hash": rule.hash()})
        assert register_and_judge(store, 1, "h1", rule, v, epoch=1, k=100, in_sample_sr=0.1) is False
        assert store.ack(1) is False and store.budget(1) == (100, 1)
        empty = HarnessResult("x", ())
        assert empty.summary()["replications"] == 0
        assert Replication(1, "trend", "tpe", 0, None, False, ("no S3 candidate",)).passed is False
    finally:
        store.close()


def test_lag_stress_reruns_use_a_holdout_assembled_at_the_stressed_latency(
    env: tuple[Catalog, TrialLedger], base: Panel
) -> None:
    """Review finding 2: the lag stress changed only `config.latency_ns` and reused the frame assembled at the
    base latency, so 'lag+1' and 'lagx2' always equalled the base SR. The holdout is rebuilt per latency."""
    catalog, ledger = env
    sp = synthetic_splits(base, SyntheticSpec(), seed=4, warmup_days=5, sel_days=14, conf_days=8)
    assert sp.holdout_at_latency is not None
    late = sp.holdout_at_latency(H)
    assert late.frame["close"].equals(sp.holdout.frame["close"])  # the same synthetic market ...
    assert not late.frame["exec_open"].equals(sp.holdout.frame["exec_open"])  # ... filled one bar later
    body = templates()["mean_reversion"].model_dump(mode="json")
    body["universe"] = {"selector": "pit_top_liquidity", "n": 3, "rebalance": "1d"}
    from quant_research.strategy.spec import StrategySpec

    v = holdout_evaluation(
        StrategySpec.model_validate(body),
        sp.holdout,
        catalog,
        ledger,
        DecisionRule(bootstrap_reps=200),
        in_sample_sr=0.0,
        seed=1,
        panel_at_latency=sp.holdout_at_latency,
    )
    stressed = v.detail["stressed_sr"]
    assert isinstance(stressed, dict)
    base_sr = float(str(v.detail["sr_holdout"]))
    lag1, lag2 = float(str(stressed["lag+1"])), float(str(stressed["lagx2"]))  # pyright: ignore[reportUnknownArgumentType]
    assert lag1 != base_sr and lag2 != base_sr


def test_holdout_evaluation_refuses_a_lag_stress_without_a_rebuilt_panel(
    env: tuple[Catalog, TrialLedger], base: Panel
) -> None:
    catalog, ledger = env
    sp = split_three(synthetic_panel(base, SyntheticSpec(), seed=4), warmup_days=5, sel_days=14, conf_days=8)
    with pytest.raises(ValueError, match="rebuilt at the stressed latency"):
        holdout_evaluation(
            templates()["trend"],
            sp.holdout,
            catalog,
            ledger,
            DecisionRule(bootstrap_reps=200),
            in_sample_sr=0.0,
            seed=1,
        )


def test_an_incomplete_holdout_verdict_carries_the_rule_hash_and_can_be_booked(tmp_path: Path) -> None:
    """Review 2 finding 7: the 'holdout run did not complete' verdict had no rule hash, so judge() refused it."""
    store = ValidatorStore.open(tmp_path / "v.db")
    try:
        rule = DecisionRule(bootstrap_reps=200)
        v = incomplete_verdict(rule, status="failed", reason="no data")
        assert v.passed is False and v.detail["rule_hash"] == rule.hash()
        assert register_and_judge(store, 1, "h", rule, v, epoch=1, k=20, in_sample_sr=0.1) is False
    finally:
        store.close()
