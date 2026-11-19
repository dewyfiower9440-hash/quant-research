"""Phase 6 step 16: mid-price bars and panels from 1 s quotes, quote streams after an order, and one paper
tick with an in-memory ledger: orders are planned, risk-checked, submitted idempotently and settled."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl
import pytest

from quant_research.backtest.panel import PanelConfig
from quant_research.config import Settings
from quant_research.paper.feed import latest_mid, mid_minute_bars, panel_from_quotes, quotes_after
from quant_research.paper.ledger import PaperLedger
from quant_research.paper.run import PaperConfig, Strategy, tick
from quant_research.risk.limits import Limits
from quant_research.risk.policy import FakeVerifier, Policy, PolicyStore, policy_bytes
from quant_research.strategy.spec import StrategySpec
from quant_research.strategy.templates import templates

S = 10**9
M = 60 * S
H = 60 * M
T0 = 1_767_225_600 * S


def _quotes(symbols: list[str], hours: int, seed: int = 1) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    rows: list[dict[str, object]] = []
    for k, s in enumerate(symbols):
        mid = 100.0 * (k + 1)
        for i in range(hours * 3600 // 10):  # a quote every 10 s
            mid *= float(np.exp(0.0002 * rng.standard_normal()))
            t = T0 + i * 10 * S
            rows.append(
                {
                    "symbol": s,
                    "event_ns": t - 50_000_000,
                    "received_ns": t,
                    "bid_price": mid * 0.9999,
                    "bid_qty": 5.0,
                    "ask_price": mid * 1.0001,
                    "ask_qty": 5.0,
                }
            )
    return pl.DataFrame(rows)


def test_minute_bars_and_panel_from_quotes() -> None:
    q = _quotes(["AAAUSDT", "BBBUSDT"], hours=3)
    bars = mid_minute_bars(q)
    assert bars.height == 2 * 180 and set(bars.columns) >= {
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "available_ns",
    }
    assert (bars["volume"] == 6.0).all() and (bars["high"] >= bars["low"]).all()
    assert (bars["available_ns"] >= bars["event_ns"]).all()  # available after the minute started
    cfg = PanelConfig(H, decision_delay_ns=0, latency_ns=0)
    panel = panel_from_quotes(q, ["AAAUSDT", "BBBUSDT"], cfg, first_decision_ns=T0 + H, last_decision_ns=T0 + 2 * H)
    assert panel.frame["decision_ns"].n_unique() == 2 and set(panel.frame["symbol"].unique()) == {"AAAUSDT", "BBBUSDT"}
    assert panel.frame["close"].null_count() == 0
    stream = quotes_after(q, "AAAUSDT", T0 + H, until_ns=T0 + H + 30 * S)
    assert len(stream) == 4 and all(s.symbol == "AAAUSDT" and s.at_ns >= T0 + H for s in stream)
    assert latest_mid(q, "BBBUSDT", T0 + 5 * S) is not None and latest_mid(q, "ZZZ", T0) is None
    assert mid_minute_bars(q.head(0)).height == 0


def test_one_paper_tick_plans_risk_checks_submits_and_settles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    symbols = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
    q = _quotes(symbols, hours=60, seed=3)

    def fake_load(root: Path, syms: list[str], window: object) -> pl.DataFrame:
        return q

    monkeypatch.setattr("quant_research.paper.run.load_quotes", fake_load)
    settings = Settings(data_root=tmp_path)
    ledger = PaperLedger.open(tmp_path / "ledger.db")
    verifier = FakeVerifier(b"k")
    policy = PolicyStore(verifier)
    limits = Limits(50_000.0, 20_000.0, 3.0, 5_000.0, 5_000.0, 5_000.0, 100, 20_000.0)
    msg = policy_bytes(Policy(1, limits, 0, T0 + 10**6 * S))
    policy.apply(msg, verifier.sign(msg), now_ns=T0)
    body = templates()["trend"].model_dump(mode="json")
    body["universe"] = {"selector": "pit_top_liquidity", "n": 2, "rebalance": "1d"}
    spec = StrategySpec.model_validate(body)
    strat = Strategy("h-trend", spec, "Paper")
    cfg = PaperConfig("paper-test", capital=10_000.0, lookback_days=2, latency_ns=M, max_wait_ns=5 * M)
    now = T0 + 59 * H + 30 * M
    first = tick(settings, cfg, ledger, policy, now_ns=now, symbols=symbols, strategies=[strat])
    assert first["quotes"] == q.height and first.get("policy") is None and "reconcile" not in first
    open_before = ledger.open_orders()
    again = tick(settings, cfg, ledger, policy, now_ns=now, symbols=symbols, strategies=[strat])
    assert again["submitted"] == 0  # same decision -> same client ids -> idempotent
    later = tick(settings, cfg, ledger, policy, now_ns=now + 20 * M, symbols=symbols, strategies=[strat])
    if open_before:  # the quotes after arrival exist in the feed: orders settle (fill or cancel after max_wait)
        assert int(str(later["settled"])) + int(str(later["canceled"])) >= 1 or ledger.open_orders() == []
        assert all(o.state != "PENDING_SUBMIT" for o in ledger.open_orders())
    assert ledger.verify_chain() == (True, "ok") and ledger.reconcile() == []
    no_policy = tick(settings, cfg, ledger, PolicyStore(verifier), now_ns=now, symbols=symbols, strategies=[strat])
    assert "no policy" in str(no_policy["policy"]) and no_policy["submitted"] == 0
    ledger.close()
