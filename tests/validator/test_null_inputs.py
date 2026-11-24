"""v2.4 step 8 (ADR-055 decision 4): synthetic null markets with premium and funding inputs, so the v3 campaign
rules (funding required, model cells) can be run end to end on a market without any predictable structure."""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest
from search_fixtures import DAYS, NAMES, T0, H, walk_klines

from quant_research.backtest.panel import Panel, PanelConfig, assemble, decision_grid, settlements
from quant_research.search.campaign import Cell
from quant_research.search.campaign_runner import missing_data
from quant_research.validator.null_pipeline import SyntheticSpec, synthetic_panel


@pytest.fixture(scope="module")
def base() -> Panel:
    cfg = PanelConfig(H)
    frame = assemble(decision_grid(T0, T0 + (DAYS * 24 - 2) * H, cfg), NAMES, walk_klines(NAMES, DAYS, drift=0.0), cfg)
    return Panel(frame, settlements(None, None), [("lake/rw.parquet", 1)], cfg)


def test_null_inputs_add_premium_and_funding_without_touching_the_prices(base: Panel) -> None:
    spec = SyntheticSpec(null="permute", hours=24 * 40)
    plain = synthetic_panel(base, spec, seed=5)
    full = synthetic_panel(base, spec, seed=5, inputs=True)
    assert full.frame["close"].to_list() == plain.frame["close"].to_list()  # the same market, inputs on top
    assert full.frame["exec_open"].to_list() == plain.frame["exec_open"].to_list()
    assert plain.funding.height == 0 and plain.frame["funding_rate"].null_count() == plain.frame.height
    assert full.funding.height > 0 and set(full.funding["symbol"].to_list()) == set(NAMES)
    assert full.files != plain.files  # the inputs are part of the data identity
    for column in ("premium_close", "funding_rate"):
        members = full.frame.filter(pl.col("member"))
        assert members[column].null_count() < 0.2 * members.height, column
    cell = Cell("combo", "1h", 30, "w2")
    assert missing_data(cell, full, require_funding=True) == ()
    assert "binance.um.bulk.funding" in missing_data(cell, plain, require_funding=True)
    assert synthetic_panel(base, spec, seed=5, inputs=True).frame.equals(full.frame)  # deterministic
    assert not synthetic_panel(base, spec, seed=6, inputs=True).frame.equals(full.frame)


def test_null_inputs_carry_no_information_about_the_returns(base: Panel) -> None:
    full = synthetic_panel(base, SyntheticSpec(null="signflip", hours=24 * 60), seed=11, inputs=True)
    frame = full.frame.sort(["symbol", "decision_ns"]).with_columns(
        (pl.col("close").shift(-24).over("symbol") / pl.col("close") - 1.0).alias("fwd")
    )
    for column in ("premium_close", "funding_rate"):
        rows = frame.filter(pl.col(column).is_not_null() & pl.col("fwd").is_not_null())
        corr = float(np.corrcoef(rows[column].to_numpy(), rows["fwd"].to_numpy())[0, 1])
        assert abs(corr) < 0.1, (column, corr)
