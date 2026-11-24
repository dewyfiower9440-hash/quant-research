"""Fixtures shared by the search tests (pipeline, campaign runner): two 1h panels of a 4-symbol random walk
(selection, confirmation) and temporary catalog / research databases. `funded_panels` is the same walk with
premium bars and 8-hourly funding settlements (what a v3 campaign requires, ADR-052)."""

from __future__ import annotations

import math
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
NAMES = [f"S{k}USDT" for k in range(4)]


def walk_klines(names: list[str], days: int, *, seed: int = 8, drift: float = 0.00001) -> pl.DataFrame:
    """1m bars of a geometric random walk per symbol (deterministic for a seed). `drift` gives the symbols
    different trends (per minute, times k - 1); 0 makes them exchangeable."""
    rng = np.random.default_rng(seed)
    n = days * 1_440
    frames: list[pl.DataFrame] = []
    for k, name in enumerate(names):
        price = 100.0 * np.exp(np.cumsum(0.0004 * rng.standard_normal(n) + drift * (k - 1)))
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
    return pl.concat(frames)


def funding_frame(names: list[str], days: int) -> pl.DataFrame:
    """8-hourly settlements for every symbol: a different level per symbol that moves by +-30 % over time (a
    constant rate has no rolling variance, and its z-score would be undefined)."""
    times = [T0 + k * 8 * H for k in range(days * 3)]
    wave = [1.0 + 0.3 * math.cos(0.7 * j) for j in range(len(times))]
    return pl.DataFrame(
        {
            "symbol": [s for s in names for _ in times],
            "event_ns": [t for _ in names for t in times],
            "last_funding_rate": [0.0001 * (k + 1) * w for k, _ in enumerate(names) for w in wave],
            "funding_interval_hours": [8 for _ in names for _ in times],
            "available_ns": [t for _ in names for t in times],
        }
    )


def premium_frame(klines: pl.DataFrame, *, seed: int = 9) -> pl.DataFrame:
    """1m premium-index bars: a small noisy level per row (its own column family, like the lake dataset)."""
    rng = np.random.default_rng(seed)
    level = 0.0002 * rng.standard_normal(klines.height)
    return klines.select("symbol", "event_ns", "available_ns").with_columns(
        pl.Series("open", level), pl.Series("high", level), pl.Series("low", level), pl.Series("close", level)
    )


def planted_full(days: int, *, effect: float = 0.0000015) -> Panel:
    """8 symbols of 1h bars. Three exogenous characteristics (traded volume, premium level, funding rate) are
    fixed per symbol and independent; each lowers the symbol's drift by `effect` per minute (the orientation the
    families assume). `effect=0`: the same characteristics without any relation to the returns."""
    names = [f"P{k}USDT" for k in range(8)]
    rng = np.random.default_rng(21)
    raw = rng.standard_normal((len(names), 3))
    q, _r = np.linalg.qr(raw - raw.mean(axis=0, keepdims=True))  # centred and exactly uncorrelated in this sample
    chars = np.asarray(q.T * np.sqrt(len(names)), dtype=np.float64)  # unit variance per characteristic
    n = days * 1_440
    frames: list[pl.DataFrame] = []
    base = walk_klines(names, days, seed=5)
    for k, name in enumerate(names):
        drift = -effect * float(chars[0, k] + chars[1, k] + chars[2, k])  # per minute
        price = 100.0 * np.exp(np.cumsum(0.0003 * rng.standard_normal(n) + drift))
        one = base.filter(pl.col("symbol") == name).with_columns(
            pl.Series("open", price),
            pl.Series("high", price * 1.0005),
            pl.Series("low", price * 0.9995),
            pl.Series("close", price),
            pl.lit(1_000.0 * float(np.exp(chars[0, k]))).alias("quote_volume"),
        )
        frames.append(one)
    klines = pl.concat(frames)
    level = {name: 0.0002 * float(chars[1, k]) for k, name in enumerate(names)}
    wobble = pl.Series("wobble", 0.00002 * rng.standard_normal(klines.height))  # a level alone has no z-score
    premium = klines.select("symbol", "event_ns", "available_ns").with_columns(
        (pl.col("symbol").replace_strict(level, return_dtype=pl.Float64) + wobble).alias("close")
    )
    premium = premium.with_columns(
        pl.col("close").alias("open"), pl.col("close").alias("high"), pl.col("close").alias("low")
    )
    rate = {name: 0.0001 * float(chars[2, k]) for k, name in enumerate(names)}
    funding = funding_frame(names, days).with_columns(
        (  # the symbol's level times the fixture's +-30 % movement over time (a constant has no z-score)
            pl.col("symbol").replace_strict(rate, return_dtype=pl.Float64)
            * pl.col("last_funding_rate")
            / pl.col("last_funding_rate").mean().over("symbol")
        ).alias("last_funding_rate")
    )
    cfg = PanelConfig(H)
    frame = assemble(
        decision_grid(T0, T0 + (days * 24 - 2) * H, cfg), names, klines, cfg, premium=premium, funding=funding
    )
    return Panel(frame, settlements(funding, None), [("lake/planted.parquet", 1)], cfg)


def _split(full: Panel) -> tuple[Panel, Panel]:
    return slice_panel(full, T0 + 7 * D, T0 + 20 * D, warmup_ns=7 * D), slice_panel(
        full, T0 + 20 * D + H, T0 + 29 * D, warmup_ns=7 * D
    )


@pytest.fixture(scope="module")
def panels() -> tuple[Panel, Panel]:
    cfg = PanelConfig(H)
    frame = assemble(decision_grid(T0, T0 + (DAYS * 24 - 2) * H, cfg), NAMES, walk_klines(NAMES, DAYS), cfg)
    return _split(Panel(frame, settlements(None, None), [("lake/rw.parquet", 1)], cfg))


def funded_pair(*, drift: float = 0.00001, seed: int = 8) -> tuple[Panel, Panel]:
    """(selection, confirmation) of the walk with premium bars and funding settlements."""
    cfg = PanelConfig(H)
    klines = walk_klines(NAMES, DAYS, seed=seed, drift=drift)
    funding = funding_frame(NAMES, DAYS)
    frame = assemble(
        decision_grid(T0, T0 + (DAYS * 24 - 2) * H, cfg),
        NAMES,
        klines,
        cfg,
        premium=premium_frame(klines),
        funding=funding,
    )
    return _split(Panel(frame, settlements(funding, None), [("lake/rw.parquet", 1)], cfg))


@pytest.fixture(scope="module")
def funded_panels() -> tuple[Panel, Panel]:
    return funded_pair()


@pytest.fixture
def env(tmp_path: Path) -> Generator[tuple[Catalog, TrialLedger]]:
    catalog = Catalog.open(tmp_path / "c.db")
    bootstrap(catalog)
    store = ResearchStore.open(tmp_path / "r.db")
    yield catalog, TrialLedger(store, env_id="ENV-T", code_sha="abc")
    store.close()
    catalog.close()
