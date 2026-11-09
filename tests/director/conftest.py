"""Shared fixtures: a synthetic point-in-time panel, a bootstrapped catalog, a research store, a Director."""

from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from quant_research.backtest.panel import Panel, PanelConfig, assemble, decision_grid, settlements, slice_panel
from quant_research.catalog.bootstrap import bootstrap
from quant_research.catalog.store import Catalog
from quant_research.director.budget import Budget
from quant_research.director.director import Director, DirectorConfig
from quant_research.director.llm import FakeLLM, LLMClient
from quant_research.director.states import State
from quant_research.research.ledger import TrialLedger
from quant_research.research.store import ResearchStore

M = 60 * 10**9
H = 60 * M
D = 24 * H
T0 = 1_767_225_600 * 10**9  # 2026-01-01 (research split)
DAYS = 45
SYMBOLS = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]


def _minutes(symbol: str, k: int) -> pl.DataFrame:
    n = DAYS * 1_440
    i = np.arange(n)
    price = 100.0 * (1 + 0.03 * np.sin(i / (400 + 90 * k)) + 0.00002 * (k - 1) * i)
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


@pytest.fixture(scope="session")
def full_panel() -> Panel:
    minutes = pl.concat([_minutes(s, k) for k, s in enumerate(SYMBOLS)])
    cfg = PanelConfig(H)
    decisions = decision_grid(T0, T0 + (DAYS * 24 - 2) * H, cfg)
    mark = minutes.select("symbol", "event_ns", "open", "high", "low", "close", "available_ns")
    members = pl.DataFrame(
        {"symbol": SYMBOLS, "from_ns": [T0] * 3, "to_ns": [None] * 3}, schema_overrides={"to_ns": pl.Int64}
    )
    frame = assemble(decisions, SYMBOLS, minutes, cfg, mark=mark, members=members)
    return Panel(frame, settlements(None, None), [("lake/synthetic.parquet", 1)], cfg)


@pytest.fixture(scope="session")
def splits(full_panel: Panel) -> tuple[Panel, Panel]:
    warm = 21 * D
    selection = slice_panel(full_panel, T0 + 21 * D, T0 + 35 * D, warmup_ns=warm)
    confirmation = slice_panel(full_panel, T0 + 35 * D + H, T0 + 44 * D, warmup_ns=warm)
    return selection, confirmation


@dataclass
class Env:
    catalog: Catalog
    store: ResearchStore
    ledger: TrialLedger
    selection: Panel
    confirmation: Panel
    path: Path

    def director(
        self,
        llm: LLMClient | None = None,
        *,
        config: DirectorConfig | None = None,
        budget: Budget | None = None,
        gate: Callable[[], tuple[bool, str]] = lambda: (True, "ok"),
        data: tuple[str, ...] = ("klines", "mark"),
        crash_at: State | None = None,
    ) -> Director:
        return Director(
            config or DirectorConfig(run_id="run-test", seed=11, universe_n=2),
            ledger=self.ledger,
            catalog=self.catalog,
            selection=self.selection,
            confirmation=self.confirmation,
            llm=llm or FakeLLM(seed=11),
            budget=budget or Budget(),
            gate=gate,
            data_available=data,
            crash_at=crash_at,
        )

    def reopen(self) -> None:
        """Simulate a process restart: new connections, same files."""
        self.store.close()
        self.store = ResearchStore.open(self.path / "research.db")
        self.ledger = TrialLedger(self.store, env_id="ENV-T", code_sha="sha")


@pytest.fixture
def env(tmp_path: Path, splits: tuple[Panel, Panel]) -> Generator[Env]:
    catalog = Catalog.open(tmp_path / "catalog.db")
    bootstrap(catalog)
    store = ResearchStore.open(tmp_path / "research.db")
    e = Env(catalog, store, TrialLedger(store, env_id="ENV-T", code_sha="sha"), splits[0], splits[1], tmp_path)
    yield e
    e.store.close()
    catalog.close()
