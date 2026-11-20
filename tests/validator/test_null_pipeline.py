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
    EpochResult,
    HarnessResult,
    Replication,
    SyntheticSpec,
    calibrate_strength,
    families_round_robin,
    holdout_evaluation,
    hourly_closes,
    incomplete_verdict,
    parse_null,
    random_factory,
    realizable_z,
    register_and_judge,
    replicate,
    replicate_epoch,
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
    # a 13-day synthetic holdout: the sample preconditions are relaxed so the replication is judged here
    rule = DecisionRule(bootstrap_reps=200, min_holdout_days=3, min_active_days=0, min_entries=0, min_mean_exposure=0.0)
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
        assert empty.summary()["replications"] == 0 and empty.summary()["measurable"] is False
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
        # the 13-day synthetic holdout tests the lag mechanics only: the sample preconditions are relaxed here
        DecisionRule(bootstrap_reps=200, min_holdout_days=3, min_active_days=0, min_entries=0, min_mean_exposure=0.0),
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
        assert v.passed is False and v.insufficient and v.detail["rule_hash"] == rule.hash()
        # a run that did not complete cannot be judged: refused, no query spent (A3 review finding 8, ADR-042)
        assert register_and_judge(store, 1, "h", rule, v, epoch=1, k=20, in_sample_sr=0.1) is False
        assert store.budget(1) == (20, 0)
    finally:
        store.close()


def test_an_insufficient_verdict_is_refused_without_spending_a_query(tmp_path: Path) -> None:
    store = ValidatorStore.open(tmp_path / "v.db")
    try:
        rule = DecisionRule(bootstrap_reps=200)
        v = Verdict(False, ("active pnl days 9 < 60",), {"rule_hash": rule.hash(), "insufficient": True}, True)
        assert register_and_judge(store, 3, "h", rule, v, epoch=1, k=20, in_sample_sr=0.1) is False
        assert store.budget(1) == (20, 0)  # nothing spent
        assert store.submission(1)[1] == "refused" and store.ack(3) is None  # no bit to research either
    finally:
        store.close()


def test_the_harness_separates_insufficient_replications_from_failed_ones() -> None:
    """A2 review: with the 600-day minimum a short synthetic holdout is insufficient; counting it as 'not passed'
    reported FWER 0 as if the criterion were met."""
    reps = (
        Replication(1, "trend", "random", 3, 0.1, False, ("holdout days 31 < 600",), insufficient=True),
        Replication(2, "trend", "random", 3, 0.1, True, ()),
        Replication(3, "trend", "random", 3, 0.1, False, ("H0 not rejected",)),
    )
    s = HarnessResult("x", reps).summary()
    assert s["judged"] == 2 and s["insufficient"] == 1 and s["pass_rate"] == 0.5 and s["measurable"] is True
    none = HarnessResult("y", reps[:1]).summary()
    assert none["measurable"] is False and none["judged"] == 0


def test_synthetic_markets_can_be_longer_than_the_base_and_three_nulls_are_available(base: Panel) -> None:
    """A3 (ADR-043): holdouts of >= 600 days need markets longer than the base sample; three pre-registered nulls."""
    hours = 60 * 24
    long = synthetic_panel(base, SyntheticSpec(hours=hours), seed=2)
    assert long.frame["decision_ns"].n_unique() == hours  # 60 days from a 40-day base
    _, _, closes = hourly_closes(base)
    raw = np.nan_to_num(np.diff(np.log(closes), axis=0))
    for null in ("bootstrap", "permute", "signflip"):
        r = synthetic_returns(closes, SyntheticSpec(null=null, hours=hours), np.random.default_rng(5))
        assert r.shape == (hours - 1, closes.shape[1])
        # every value comes from the centred base sample (up to its sign): no invented returns
        centred_simple = np.expm1(raw) - np.expm1(raw).mean(axis=0)
        assert np.isin(np.round(np.abs(np.expm1(r)), 12), np.round(np.abs(centred_simple), 12)).all()
    flip = synthetic_returns(closes, SyntheticSpec(null="signflip", hours=hours), np.random.default_rng(6))
    assert abs(float(np.mean(flip))) < abs(float(np.mean(raw))) + 2e-5  # the sign flip removes the market drift
    perm = synthetic_returns(closes, SyntheticSpec(null="permute", block_len=24), np.random.default_rng(7))
    centred = np.expm1(raw[:, 0]) - np.expm1(raw[:, 0]).mean()
    assert perm.shape == raw.shape and np.allclose(np.sort(np.expm1(perm[:, 0])), np.sort(centred))  # a reordering
    assert not np.array_equal(perm[:, 0], raw[:, 0])
    with pytest.raises(ValueError):
        SyntheticSpec(hours=1)
    with pytest.raises(ValueError, match="null"):
        SyntheticSpec(null="permutation")  # type: ignore[arg-type]  # a typo must not silently run the bootstrap
    with pytest.raises(ValueError, match="strength"):
        SyntheticSpec(alpha="mechanism", strength=1.0)  # |coef| >= 1 makes the AR path explode
    with pytest.raises(ValueError, match="strength"):
        SyntheticSpec(alpha="random", strength=float("nan"))


def test_every_null_is_demeaned_so_the_base_drift_is_not_planted(base: Panel) -> None:
    """A3 review finding 1: resampling raw returns carried each symbol's 2025H1 drift into the whole synthetic
    market (a long-short on the base drift signs reached z ~2.9 on a 700-day 'null' holdout)."""
    _, _, closes = hourly_closes(base)
    raw = np.nan_to_num(np.diff(np.log(closes), axis=0))
    perm = synthetic_returns(closes, SyntheticSpec(null="permute"), np.random.default_rng(7))
    simple = np.expm1(perm)  # the engine trades SIMPLE returns: those are centred (re-review: a log-centred null
    assert np.allclose(simple.mean(axis=0), 0.0, atol=1e-15)  # kept a sigma^2/2 arithmetic drift per symbol)
    centred = np.expm1(raw) - np.expm1(raw).mean(axis=0)
    assert np.allclose(np.sort(simple[:, 0]), np.sort(centred[:, 0]))
    flip = np.expm1(synthetic_returns(closes, SyntheticSpec(null="signflip", hours=20_001), np.random.default_rng(8)))
    assert np.all(np.abs(flip.mean(axis=0)) < 4 * flip.std(axis=0) / np.sqrt(flip.shape[0]))  # mean 0 in simple space


def test_alpha_strength_is_calibrated_so_a_tradable_strategy_reaches_the_target_z(base: Panel) -> None:
    """A3 review findings 2-3: the analytic formulas assumed independent symbols (real pairwise correlation ~0.76)
    and an oracle that knows WHICH hours drift; the strength is now calibrated by simulation so that a strategy
    that can actually be traded (long all symbols / fade the last hour) reaches the target z on the holdout."""
    _, _, closes = hourly_closes(base)
    hours = 24 * 300
    for alpha in ("random", "mechanism"):
        s = calibrate_strength(alpha, 3.0, closes, holdout_hours=hours, draws=16, seed=1, iters=25, null="signflip")  # type: ignore[arg-type]
        assert s > 0
        zs = [
            realizable_z(
                alpha,
                synthetic_returns(
                    closes,
                    SyntheticSpec(alpha=alpha, strength=s, hours=hours + 1, null="signflip"),  # type: ignore[arg-type]
                    np.random.default_rng(100 + i),
                ),
            )
            for i in range(16)
        ]
        assert 2.3 < float(np.mean(zs)) < 3.7  # the target on fresh draws (sd of the mean of 16 z's ~0.25)
    assert calibrate_strength("none", 3.0, closes, holdout_hours=hours, draws=2, seed=1) == 0.0
    with pytest.raises(ValueError, match="cannot reach"):  # an unreachable target is refused, not returned
        calibrate_strength("mechanism", 500.0, closes, holdout_hours=hours, draws=2, seed=1, iters=5)
    assert parse_null("signflip") == "signflip"
    with pytest.raises(ValueError, match="unknown null"):
        parse_null("sign_flip")


def test_an_epoch_replication_spends_up_to_k_queries_and_counts_any_pass(
    env: tuple[Catalog, TrialLedger], base: Panel, tmp_path: Path
) -> None:
    """A3 / review 1 finding 10: FWER is P(any of up to k holdout queries passes) in one epoch, not the rate of a
    single query. Each judged candidate spends one query of the epoch's budget; insufficient ones spend none."""
    catalog, ledger = env
    # a 120-day market (longer than the 40-day base) with a planted mechanism, so positive survivors exist
    spec = SyntheticSpec(alpha="mechanism", strength=0.3, hours=24 * 120)
    sp = synthetic_splits(base, spec, seed=4, warmup_days=10, sel_days=40, conf_days=30)
    rule = DecisionRule(
        k=3, bootstrap_reps=200, min_holdout_days=3, min_active_days=0, min_entries=0, min_mean_exposure=0.0
    )
    vstore = ValidatorStore.open(tmp_path / "v.db")
    try:
        rep = replicate_epoch(
            sp,
            catalog,
            ledger,
            vstore,
            families=["mean_reversion", "momentum"],
            factory=random_factory,
            budget=SearchBudget(s1_count=6, top_k=2, promote_rate=0.0, s3_top=2),
            rule=rule,
            seed=4,
            k=rule.k,
        )
        k, used = vstore.budget(1)
        assert rep.candidates > 0 and rep.queries > 0  # precondition: the epoch actually reached the validator
        assert k == 3 and used == rep.queries <= 3 and rep.candidates >= rep.queries
        assert rep.passed == (rep.passes > 0) and rep.passes <= rep.queries
        assert all({"family", "sr3", "passed"} <= set(c) for c in rep.records)
        assert all(float(str(c["sr3"])) > 0 for c in rep.records)  # only positive survivors are submitted
    finally:
        vstore.close()
    res = EpochResult("x", (rep,)).summary()
    assert res["replications"] == 1 and res["measurable"] == (rep.queries > 0)


def test_the_epoch_fwer_counts_epochs_without_candidates_and_excludes_only_all_insufficient_ones() -> None:
    """A3 review finding 6: an epoch whose search found no positive survivor is a measured non-pass; only epochs
    whose every submission was insufficient measured nothing."""
    from quant_research.validator.null_pipeline import EpochReplication

    reps = (
        EpochReplication(1, "random", 0, 0, 0, 0, ()),  # no candidate: a measured non-pass
        EpochReplication(2, "random", 3, 0, 3, 0, ()),  # all insufficient: not measured
        EpochReplication(3, "random", 5, 2, 3, 1, ()),  # a pass
        EpochReplication(4, "random", 2, 2, 0, 0, ()),
    )
    s = EpochResult("x", reps).summary()
    assert s["judged_replications"] == 3 and s["unmeasured"] == 1 and s["no_candidate"] == 1
    assert s["fwer"] == pytest.approx(1 / 3)


def test_the_epoch_budget_must_be_the_rules_k(env: tuple[Catalog, TrialLedger], base: Panel, tmp_path: Path) -> None:
    """A3 review finding 4: k = 50 queries judged at alpha/20 would bound the FWER by 0.125, not 0.05."""
    catalog, ledger = env
    sp = synthetic_splits(base, SyntheticSpec(), seed=4, warmup_days=5, sel_days=14, conf_days=8)
    vstore = ValidatorStore.open(tmp_path / "v.db")
    try:
        with pytest.raises(ValueError, match="rule's k"):
            replicate_epoch(
                sp,
                catalog,
                ledger,
                vstore,
                families=["momentum"],
                factory=random_factory,
                budget=SearchBudget(s1_count=2, top_k=1, promote_rate=0.0, s3_top=1),
                rule=DecisionRule(bootstrap_reps=200),
                seed=1,
                k=50,
            )
    finally:
        vstore.close()


def test_the_two_lag_stresses_differ_at_zero_base_latency() -> None:
    """B8 review 9: with latency 0, lag+1 = tf and lagx2 = max(0, tf) = tf ran the same stress twice."""
    from quant_research.validator.null_pipeline import stress_latencies

    tf = 3_600 * 10**9
    assert stress_latencies(0, tf) == {"lag+1": tf, "lagx2": 2 * tf}
    assert stress_latencies(tf, tf) == {"lag+1": 2 * tf, "lagx2": 2 * tf}
    assert stress_latencies(3 * tf, tf) == {"lag+1": 4 * tf, "lagx2": 6 * tf}
