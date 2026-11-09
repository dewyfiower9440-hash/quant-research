"""Phase 2 step 3: point-in-time decision panel (synthetic 1m data, real lake layout for the loader)."""

from pathlib import Path

import polars as pl
import pytest

from quant_research.backtest.panel import (
    PanelConfig,
    assemble,
    bars,
    decision_grid,
    load_panel,
    select_top_liquidity,
    settlements,
)
from quant_research.catalog.bootstrap import bootstrap
from quant_research.catalog.store import Catalog
from quant_research.data.access import PointInTime, SealViolationError
from quant_research.data.lake import write_frame

M = 60 * 10**9
T0 = 1_767_225_600 * 10**9  # 2026-01-01T00:00Z (research split, before the 2026-07-01 seal)


def minutes(symbol: str, n: int, *, start: int = T0, lag: int = 2 * M, skip: tuple[int, ...] = ()) -> pl.DataFrame:
    """n 1m bars: open = 100 + i, high = open + 0.5, low = open - 0.5, close = open + 0.25; available = start + lag."""
    rows = [i for i in range(n) if i not in skip]
    return pl.DataFrame(
        {
            "symbol": [symbol] * len(rows),
            "event_ns": [start + i * M for i in rows],
            "open": [100.0 + i for i in rows],
            "high": [100.5 + i for i in rows],
            "low": [99.5 + i for i in rows],
            "close": [100.25 + i for i in rows],
            "volume": [1.0] * len(rows),
            "quote_volume": [100.0 + i for i in rows],
            "available_ns": [start + i * M + lag for i in rows],
        }
    )


def test_bars_aggregate_ohlcv_and_take_the_last_minutes_availability() -> None:
    b = bars(minutes("A", 10), 5 * M)
    first = b.row(0, named=True)
    assert (first["start_ns"], first["open"], first["high"], first["low"], first["close"]) == (
        T0,
        100.0,
        104.5,
        99.5,
        104.25,
    )
    assert (first["minutes"], first["volume"], first["available_ns"]) == (5, 5.0, T0 + 4 * M + 2 * M)
    assert b.height == 2


def test_decision_grid_is_bar_end_plus_delay() -> None:
    grid = decision_grid(T0, T0 + 5 * M, PanelConfig(5 * M))
    assert grid.to_list() == [T0 + 6 * M, T0 + 11 * M]


def test_a_bar_is_only_seen_once_available_and_a_larger_lag_makes_it_stale_not_leaky() -> None:
    cfg = PanelConfig(5 * M)
    decisions = decision_grid(T0, T0 + 5 * M, cfg)
    on_time = assemble(decisions, ["A"], minutes("A", 15), cfg)
    assert on_time["close"].to_list() == [104.25, 109.25]  # bar [0,5) at 6m, bar [5,10) at 11m
    late = assemble(decisions, ["A"], minutes("A", 15, lag=3 * M), cfg)  # one more minute of lag
    assert late["close"].to_list() == [None, 104.25]  # nothing yet at 6m; at 11m the older bar


def test_execution_interval_starts_at_the_decision_and_flags_missing_minutes() -> None:
    cfg = PanelConfig(5 * M)
    decisions = decision_grid(T0, T0 + 5 * M, cfg)
    full = assemble(decisions, ["A"], minutes("A", 20), cfg)
    row = full.row(0, named=True)
    assert (row["exec_open"], row["exec_high"], row["exec_low"], row["exec_close"]) == (106.0, 110.5, 105.5, 110.25)
    assert row["complete"] and row["exec_minutes"] == 5
    holed = assemble(decisions, ["A"], minutes("A", 20, skip=(8,)), cfg)
    assert holed["complete"].to_list() == [False, True]
    delayed = assemble(decisions, ["A"], minutes("A", 20), PanelConfig(5 * M, latency_ns=M))
    assert delayed.row(0, named=True)["exec_open"] == 107.0  # one minute of latency


def test_membership_spans_are_point_in_time_with_exclusive_end() -> None:
    cfg = PanelConfig(5 * M)
    decisions = decision_grid(T0, T0 + 10 * M, cfg)
    members = pl.DataFrame({"symbol": ["A"], "from_ns": [T0 + 11 * M], "to_ns": [T0 + 16 * M]})
    out = assemble(decisions, ["A"], minutes("A", 25), cfg, members=members)
    assert out["member"].to_list() == [False, True, False]


def test_top_liquidity_selection_uses_only_previous_days_volume() -> None:
    day = 86_400 * 10**9
    cfg = PanelConfig(day)
    frames: list[pl.DataFrame] = []
    for sym, base in (("A", 1.0), ("B", 5.0), ("C", 3.0)):
        f = minutes(sym, 3 * 1_440).with_columns((pl.col("quote_volume") * 0 + base).alias("quote_volume"))
        frames.append(f)
    one = pl.concat(frames)
    decisions = decision_grid(T0, T0 + day, cfg)
    panel = select_top_liquidity(assemble(decisions, ["A", "B", "C"], one, cfg), 1)
    chosen = panel.filter(pl.col("selected")).select("decision_ns", "symbol").rows()
    # decision 1 (day 1) sees day-0 bar volume: B is largest; no earlier day for the first UTC day's ranking
    assert all(sym == "B" for _d, sym in chosen) and len(chosen) >= 1


def test_settlements_carry_the_mark_open_at_the_funding_minute() -> None:
    funding = pl.DataFrame({"symbol": ["A"], "event_ns": [T0 + 2 * M], "last_funding_rate": [0.0001]})
    events = settlements(funding, minutes("A", 5))
    assert events.rows() == [("A", T0 + 2 * M, 0.0001, 102.0)]


def _write(root: Path, dataset: str, frame: pl.DataFrame, symbol: str) -> None:
    write_frame(
        frame.drop("available_ns", "symbol"),
        root / "lake" / dataset / "2026-01" / f"{symbol}-2026-01-v1.parquet",
        symbol=symbol,
    )


def test_load_panel_reads_only_the_requested_symbols_and_refuses_the_sealed_holdout(tmp_path: Path) -> None:
    catalog = Catalog.open(tmp_path / "c.db")
    bootstrap(catalog)
    try:
        for sym in ("A", "B"):
            _write(tmp_path, "binance.um.bulk.klines_1m", minutes(sym, 30), sym)
        pit = PointInTime(catalog)
        panel = load_panel(
            pit, tmp_path, ["A"], first_bar_ns=T0, last_bar_ns=T0 + 10 * M, config=PanelConfig(5 * M), rule=None
        )
        assert set(panel.frame["symbol"].to_list()) == {"A"}
        assert [p for p, _s in panel.files] == ["lake/binance.um.bulk.klines_1m/2026-01/A-2026-01-v1.parquet"]
        # the bulk rows are modelled: available = open + 1m interval + 1m lag (bootstrap prior) -> as in `minutes`
        assert panel.frame["close"].to_list()[:2] == [104.25, 109.25]
        sealed = 1_782_864_000 * 10**9  # 2026-07-01: research_until of the bulk datasets (ADR-013)
        with pytest.raises(SealViolationError):
            load_panel(
                pit, tmp_path, ["A"], first_bar_ns=T0, last_bar_ns=sealed, config=PanelConfig(86_400 * 10**9), rule=None
            )
    finally:
        catalog.close()
