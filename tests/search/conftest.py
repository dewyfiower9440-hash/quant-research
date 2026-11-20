"""Fixtures shared by the search tests (pipeline, campaign runner): two 1h panels of a 4-symbol random walk
(selection, confirmation) and temporary catalog / research databases."""

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

M = 60 * 10**9
H = 60 * M
D = 24 * H
T0 = 1_767_225_600 * 10**9
DAYS = 30


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
