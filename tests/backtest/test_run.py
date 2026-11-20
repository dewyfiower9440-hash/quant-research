"""Phase 2 step 8: runner, trial / backtest_run accounting, refusals, templates through the full pipeline."""

from collections.abc import Generator
from pathlib import Path
from typing import cast

import numpy as np
import polars as pl
import pytest

from quant_research.backtest.panel import Panel, PanelConfig, assemble, decision_grid, settlements
from quant_research.backtest.run import RunConfig, run_backtest, unblob
from quant_research.catalog.bootstrap import bootstrap
from quant_research.catalog.store import Catalog
from quant_research.research.ledger import TrialLedger
from quant_research.research.store import ResearchStore
from quant_research.strategy.s0 import check
from quant_research.strategy.spec import StrategySpec
from quant_research.strategy.templates import templates

M = 60 * 10**9
H = 60 * M
T0 = 1_767_225_600 * 10**9  # 2026-01-01
DAYS = 21


def _minutes(symbol: str, k: int) -> pl.DataFrame:
    n = DAYS * 1_440
    i = np.arange(n)
    # deterministic, symbol-specific wave + drift (no RNG)
    price = 100.0 * (1 + 0.02 * np.sin(i / (300 + 50 * k)) + 0.00001 * (k - 1) * i)
    return pl.DataFrame(
        {
            "symbol": [symbol] * n,
            "event_ns": T0 + i * M,
            "open": price,
            "high": price * 1.001,
            "low": price * 0.999,
            "close": price * (1 + 0.0002 * np.cos(i)),
            "volume": np.full(n, 10.0),
            "quote_volume": np.full(n, 1_000.0 * (k + 1)),
            "available_ns": T0 + i * M + 2 * M,
        }
    )


@pytest.fixture(scope="module")
def panel() -> Panel:
    syms = ["AAA", "BBB", "CCC"]
    minutes = pl.concat([_minutes(s, k) for k, s in enumerate(syms)])
    cfg = PanelConfig(H)
    decisions = decision_grid(T0, T0 + (DAYS * 24 - 2) * H, cfg)
    funding = pl.DataFrame(
        {
            "symbol": [s for s in syms for _ in range(DAYS * 3)],
            "event_ns": [T0 + j * 8 * H for _ in syms for j in range(DAYS * 3)],
            "last_funding_rate": [0.0001 * (k + 1) for k, _ in enumerate(syms) for _ in range(DAYS * 3)],
        }
    ).with_columns((pl.col("event_ns") + M).alias("available_ns"))
    mark = minutes.select("symbol", "event_ns", "open", "high", "low", "close", "available_ns")
    members = pl.DataFrame(
        {"symbol": syms, "from_ns": [T0] * 3, "to_ns": [None] * 3}, schema_overrides={"to_ns": pl.Int64}
    )
    frame = assemble(decisions, syms, minutes, cfg, mark=mark, funding=funding, members=members)
    return Panel(frame, settlements(funding, mark), [("lake/x.parquet", 1)], cfg)


@pytest.fixture
def env(tmp_path: Path) -> Generator[tuple[Catalog, TrialLedger]]:
    catalog = Catalog.open(tmp_path / "c.db")
    bootstrap(catalog)
    store = ResearchStore.open(tmp_path / "r.db")
    yield catalog, TrialLedger(store, env_id="ENV-T", code_sha="sha")
    store.close()
    catalog.close()


def _spec(**changes: object) -> StrategySpec:
    body = templates()["trend"].model_dump(mode="json")
    body.update({"universe": {"selector": "pit_top_liquidity", "n": 2, "rebalance": "1d"}})
    body.update(changes)
    return StrategySpec.model_validate(body)


COUNT_SQL = {
    "trial": "select count(*) from trial",
    "backtest_run": "select count(*) from backtest_run",
    "experiment": "select count(*) from experiment",
}


def _count(ledger: TrialLedger, table: str) -> int:
    return int(ledger.store.conn.execute(COUNT_SQL[table]).fetchone()[0])


def test_templates_pass_s0() -> None:
    assert {name: check(s) for name, s in templates().items()} == {name: [] for name in templates()}


@pytest.mark.parametrize("name", sorted(templates()))
def test_each_template_runs_through_the_full_pipeline_and_both_engines_agree(
    name: str, panel: Panel, env: tuple[Catalog, TrialLedger]
) -> None:
    catalog, ledger = env
    body = templates()[name].model_dump(mode="json")
    body["universe"] = {"selector": "pit_top_liquidity", "n": 2, "rebalance": "1d"}
    spec = StrategySpec.model_validate(body)
    ev = run_backtest(spec, panel, catalog, ledger, RunConfig(engine="event"))
    vec = run_backtest(spec, panel, catalog, ledger, RunConfig(engine="vector"))
    assert ev.result is not None and vec.result is not None
    np.testing.assert_allclose(ev.result.equity, vec.result.equity, rtol=1e-9, atol=1e-9)
    assert ev.status == vec.status
    runs = ledger.store.conn.execute("select pnl_daily from backtest_run order by trial_id").fetchall()
    np.testing.assert_allclose(unblob(runs[0][0]), unblob(runs[1][0]), rtol=1e-9, atol=1e-12)
    assert _count(ledger, "trial") == 2 == _count(ledger, "backtest_run")


def test_every_evaluation_is_one_trial_with_its_configuration(panel: Panel, env: tuple[Catalog, TrialLedger]) -> None:
    catalog, ledger = env
    out = run_backtest(_spec(), panel, catalog, ledger, RunConfig(engine="event"))
    run_backtest(_spec(), panel, catalog, ledger, RunConfig(engine="event"))  # a re-run is another trial
    assert _count(ledger, "trial") == 2 and _count(ledger, "backtest_run") == 2
    assert _count(ledger, "experiment") == 1  # same experiment key
    config = ledger.store.conn.execute("select config_json from experiment").fetchone()[0]
    assert '"lag_versions": {"binance.um.bulk.klines_1m": 1' in config and '"engine": "event"' in config
    assert set(out.metrics) >= {
        "total_return",
        "sr_daily",
        "max_drawdown",
        "costs",
        "funding",
        "worst_case_total_return",
    }
    assert isinstance(out.metrics["costs"], float) and out.metrics["costs"] > 0
    row = ledger.store.conn.execute("select fidelity, data_version, length(pnl_daily) > 0 from backtest_run").fetchone()
    assert row[0] == "L1" and len(row[1]) == 64 and row[2] == 1


def test_refusals_are_trials_with_reasons(panel: Panel, env: tuple[Catalog, TrialLedger]) -> None:
    catalog, ledger = env
    maker = run_backtest(
        _spec(execution={"style": "maker", "max_participation": 0.01}),
        panel,
        catalog,
        ledger,
        RunConfig(engine="event"),
    )
    l2 = run_backtest(_spec(), panel, catalog, ledger, RunConfig(engine="event", fidelity="L2"))
    stateful = run_backtest(_spec(exit={"time_stop_bars": 24}), panel, catalog, ledger, RunConfig(engine="vector"))
    assert maker.status == l2.status == stateful.status == "rejected"
    assert maker.reason is not None and "paper-trading only" in maker.reason
    assert l2.reason is not None and "aggTrades" in l2.reason
    assert stateful.reason is not None and "path dependent" in stateful.reason
    statuses = [r[0] for r in ledger.store.conn.execute("select status from trial order by trial_id")]
    assert statuses == ["rejected"] * 3 and _count(ledger, "backtest_run") == 0


def test_short_holding_needs_l2_and_is_rejected_after_the_run_is_recorded(env: tuple[Catalog, TrialLedger]) -> None:
    """5m bars and a signal that flips every bar: positions last ~5 minutes -> L2 required (§9-5)."""
    catalog, ledger = env
    cfg = PanelConfig(5 * M)
    syms = ["AAA", "BBB"]
    minutes = pl.concat([_minutes(s, k) for k, s in enumerate(syms)]).filter(pl.col("event_ns") < T0 + 2 * 1_440 * M)
    frame = assemble(decision_grid(T0, T0 + (2 * 288 - 2) * 5 * M, cfg), syms, minutes, cfg)
    fast = Panel(frame, settlements(None, None), [], cfg)
    flip = _spec(
        timeframe="5m",
        features={},
        signal={"op": "delta", "args": [{"op": "close"}], "window": 1},
        entry={"long": {"op": "gt", "args": ["signal", 0.0]}, "short": {"op": "lt", "args": ["signal", 0.0]}},
        sizing={"method": "equal", "max_leverage": 1.0},
    )
    out = run_backtest(flip, fast, catalog, ledger, RunConfig(engine="event"))
    holding = out.metrics["avg_holding_hours"]
    assert isinstance(holding, float) and holding < 1.0
    assert out.status == "rejected" and out.reason is not None and "needs L2 fidelity" in out.reason
    assert _count(ledger, "backtest_run") == 1  # the evaluation happened and is recorded


def test_missing_funding_data_is_flagged_not_silently_ignored(panel: Panel, env: tuple[Catalog, TrialLedger]) -> None:
    catalog, ledger = env
    bare = Panel(panel.frame, panel.funding.clear(), panel.files, panel.config)
    out = run_backtest(_spec(), bare, catalog, ledger, RunConfig(engine="event"))
    flags = out.metrics["flags"]
    assert isinstance(flags, list) and any("funding_not_modelled" in str(f) for f in cast(list[object], flags))
    assert out.metrics["funding"] == 0.0


def test_a_spec_on_a_panel_of_another_timeframe_is_refused(panel: Panel, env: tuple[Catalog, TrialLedger]) -> None:
    """v2.2 B5: specs now come in 1h / 4h / 1d; the metrics (holding hours, bars per year) use the spec's
    timeframe, so a spec on a panel of another timeframe would be silently mis-measured."""
    catalog, ledger = env
    out = run_backtest(_spec(timeframe="4h"), panel, catalog, ledger, RunConfig(engine="vector"))
    assert out.status == "rejected" and out.reason is not None and "timeframe" in out.reason


def test_the_rebalance_interval_and_band_reach_both_engines(panel: Panel, env: tuple[Catalog, TrialLedger]) -> None:
    """v2.2 B5 (D5): a slower rebalance trades less; vector and event agree on it."""
    catalog, ledger = env
    fast = run_backtest(_spec(), panel, catalog, ledger, RunConfig(engine="vector"))
    slow_exec = {"style": "taker", "max_participation": 0.01, "rebalance_bars": 24, "min_trade_weight": 0.02}
    slow = run_backtest(_spec(execution=slow_exec), panel, catalog, ledger, RunConfig(engine="vector"))
    slow_ev = run_backtest(_spec(execution=slow_exec), panel, catalog, ledger, RunConfig(engine="event"))
    assert fast.status == slow.status == slow_ev.status == "done"
    trades = (fast.metrics["trades"], slow.metrics["trades"])
    assert isinstance(trades[0], int) and isinstance(trades[1], int) and trades[1] < trades[0]
    assert slow.metrics["final_equity"] == pytest.approx(slow_ev.metrics["final_equity"], rel=1e-9)
