"""Phase 6 step 15: divergence decomposition adds up, pre-seal summaries drop sealed rows, and degradation
moves a paper strategy to Degraded (never back towards Active)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from quant_research.paper.divergence import DecisionRecord, decompose, pre_seal_summary
from quant_research.paper.monitor import apply_health, assess
from quant_research.validator.lifecycle import DegradationRule, IllegalLifecycleTransition, LifecycleLedger, Transition
from quant_research.validator.store import ValidatorStore


def _rec(**over: object) -> DecisionRecord:
    base: dict[str, object] = {
        "decision_ns": 1,
        "symbol": "BTCUSDT",
        "w_backtest": 0.5,
        "w_paper": 0.5,
        "p_ref": 100.0,
        "p_arrival": 100.0,
        "vwap": 100.0,
        "filled_fraction": 1.0,
        "r_ref": 0.01,
        "backtest_pnl": 0.005,
        "paper_pnl": 0.005,
    }
    base.update(over)
    return DecisionRecord(**base)  # type: ignore[arg-type]


def test_components_isolate_each_divergence_source() -> None:
    assert decompose([_rec()]).total == 0.0 and decompose([]).n == 0
    sig = decompose([_rec(w_paper=0.3, paper_pnl=0.003)])
    assert sig.signal == pytest.approx(-0.002) and sig.latency == 0 and sig.spread == 0 and sig.partial == 0
    assert sig.residual == pytest.approx(0.0) and sig.total == pytest.approx(-0.002)
    lat = decompose([_rec(p_arrival=101.0, vwap=101.0, paper_pnl=0.0)])
    assert lat.latency == pytest.approx(0.5 * (100.0 - 101.0) / 100.0) and lat.spread == pytest.approx(0.0)
    spr = decompose([_rec(vwap=100.2)])
    assert spr.spread == pytest.approx(-0.5 * 0.2 / 100.0)
    par = decompose([_rec(filled_fraction=0.4, paper_pnl=0.002)])
    assert par.partial == pytest.approx(-0.6 * 0.5 * 0.01) and par.total == pytest.approx(-0.003)
    short = decompose([_rec(w_paper=-0.5, w_backtest=-0.5, vwap=99.9, paper_pnl=-0.005)])
    assert short.spread == pytest.approx(-0.5 * (-1.0) * (99.9 - 100.0) / 100.0)  # selling below mid costs
    shares = decompose([_rec(w_paper=0.3, paper_pnl=0.003), _rec(symbol="ETHUSDT", vwap=100.2)]).shares()
    assert sum(shares.values()) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        decompose([_rec(p_ref=0.0)])


def test_pre_seal_summary_drops_sealed_decisions() -> None:
    recs = [
        _rec(decision_ns=1, filled_fraction=1.0),
        _rec(decision_ns=5, filled_fraction=0.0, vwap=None),
        _rec(decision_ns=9),
    ]
    s = pre_seal_summary(recs, seal_boundary_ns=9)
    assert s["decisions"] == 2 and s["dropped_post_seal"] == 1 and s["fill_rate"] == pytest.approx(0.5)
    assert pre_seal_summary(recs, 0)["fill_rate"] is None


def test_health_degrades_a_paper_strategy_but_never_promotes(tmp_path: Path) -> None:
    rule = DegradationRule()
    rng = np.random.default_rng(2)
    good = assess(0.001 + 0.01 * rng.standard_normal(60), 0.001, 0.01, [5.0] * 10, [5.0] * 10, 100, 2, rule)
    assert not good.degraded and good.reasons == ()
    bad_pnl = np.concatenate([0.001 + 0.01 * rng.standard_normal(30), -0.02 + 0.01 * rng.standard_normal(60)])
    bad = assess(bad_pnl, 0.001, 0.01, [20.0] * 10, [5.0] * 10, 100, 20, rule)
    assert bad.degraded and len(bad.reasons) == 3 and bad.cusum_alarm_index is not None
    store = ValidatorStore.open(tmp_path / "v.db")
    try:
        led = LifecycleLedger(store.conn, now_ns=lambda: 1)
        led.move(Transition("h", None, "Candidate", "s", "trial:1", "research"))
        led.move(Transition("h", "Candidate", "Validated", "v", "verdict:1", "validator"))
        assert apply_health(led, "h", bad, evidence_ref="paper:run") is None  # not in Paper yet: untouched
        led.move(Transition("h", "Validated", "Paper", "p", "paper:run", "paper"))
        assert apply_health(led, "h", good, evidence_ref="paper:run") is None
        assert apply_health(led, "h", bad, evidence_ref="paper:run") == "Degraded"
        assert led.state("h") == "Degraded"
        with pytest.raises(IllegalLifecycleTransition):
            led.move(Transition("h", "Degraded", "Active", "nope", "paper:run", "paper"))
        with pytest.raises(IllegalLifecycleTransition):
            led.move(Transition("h", "Degraded", "Validated", "back", "verdict:2", "paper"))  # only the validator
    finally:
        store.close()
