"""Phase 2 step 3: point-in-time decision panel (synthetic 1m data, real lake layout for the loader)."""

import math
from pathlib import Path

import numpy as np
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


def test_under_seal_epoch_2_a_panel_may_not_read_the_historical_holdout(tmp_path: Path) -> None:
    """B2 (ADR-040) at the panel level: months of the historical holdout are refused (warm-up included), a
    research-window month still loads."""
    from quant_research.catalog.epochs import register_epoch2

    catalog = Catalog.open(tmp_path / "c.db")
    bootstrap(catalog)
    try:
        assert catalog.seal("binance.um.bulk.klines_1m").epoch == 2  # bootstrap registers epoch 2 (B4)
        assert register_epoch2(catalog, apply=True) == []  # nothing left to move
        june_2024 = 1_717_200_000 * 10**9  # 2024-06-01: inside the historical holdout
        frame = minutes("A", 30, start=june_2024)
        write_frame(
            frame.drop("available_ns", "symbol"),
            tmp_path / "lake" / "binance.um.bulk.klines_1m" / "2024-06" / "A-2024-06-v1.parquet",
            symbol="A",
        )
        _write(tmp_path, "binance.um.bulk.klines_1m", minutes("A", 30), "A")
        pit = PointInTime(catalog)
        with pytest.raises(SealViolationError, match="research window"):
            load_panel(
                pit,
                tmp_path,
                ["A"],
                first_bar_ns=june_2024,
                last_bar_ns=june_2024 + 10 * M,
                config=PanelConfig(5 * M),
                rule=None,
            )
        ok = load_panel(
            pit, tmp_path, ["A"], first_bar_ns=T0, last_bar_ns=T0 + 10 * M, config=PanelConfig(5 * M), rule=None
        )
        assert ok.frame.height > 0  # 2026-01 lies in the second research window
    finally:
        catalog.close()


def test_the_funding_feature_is_an_8h_equivalent_rate_while_settlements_keep_the_paid_rate() -> None:
    """B6 review 2: symbols settle every 4 h or 8 h; ranking raw rates across them mis-orders the carry. The
    feature column is rate * 8 / interval_hours; the cash flow (settlements) stays the rate actually paid."""
    cfg = PanelConfig(60 * M)
    kl = pl.concat([minutes("A", 6 * 60), minutes("B", 6 * 60)])
    funding = pl.DataFrame(
        {
            "symbol": ["A", "B"],
            "event_ns": [T0 + 60 * M, T0 + 60 * M],
            "funding_interval_hours": [4, 8],
            "last_funding_rate": [0.0001, 0.0001],
            "available_ns": [T0 + 61 * M, T0 + 61 * M],
        }
    )
    frame = assemble(decision_grid(T0, T0 + 4 * 60 * M, cfg), ["A", "B"], kl, cfg, funding=funding)
    last = frame.filter(pl.col("decision_ns") == frame["decision_ns"].max()).sort("symbol")
    assert last["funding_rate"].to_list() == pytest.approx([0.0002, 0.0001])  # A pays every 4 h: twice per 8 h
    assert settlements(funding, None)["rate"].to_list() == pytest.approx([0.0001, 0.0001])


def _reference_select_top_liquidity(frame: pl.DataFrame, n: int, window_days: int = 30) -> pl.DataFrame:
    """The pre-fix implementation (quadratic in days per symbol), kept as the reference semantics."""
    day = 86_400 * 10**9
    with_day = frame.sort(["symbol", "decision_ns"]).with_columns(
        (pl.col("decision_ns") // day).alias("_day"), pl.col("quote_volume").fill_null(0.0).alias("_qv")
    )
    per_bar = with_day.unique(subset=["symbol", "bar_start_ns"], keep="first", maintain_order=True)
    daily = per_bar.group_by("symbol", (pl.col("bar_start_ns") // day).alias("_bar_day")).agg(pl.col("_qv").sum())
    days = with_day.select("symbol", "_day").unique()
    trailing = (
        days.join(daily, on="symbol", how="left")
        .filter((pl.col("_bar_day") < pl.col("_day")) & (pl.col("_bar_day") >= pl.col("_day") - window_days))
        .group_by("symbol", "_day")
        .agg(pl.col("_qv").sum().alias("_trailing"))
    )
    first = with_day.filter(pl.col("decision_ns") == pl.col("decision_ns").min().over("_day"))
    ranked = (
        first.join(trailing, on=["symbol", "_day"], how="left")
        .filter(pl.col("member") & pl.col("_trailing").is_not_null())
        .with_columns(pl.col("_trailing").rank("ordinal", descending=True).over("_day").alias("_rank"))
        .filter(pl.col("_rank") <= n)
        .select("symbol", "_day", pl.lit(True).alias("selected"))
    )
    return (
        with_day.join(ranked, on=["symbol", "_day"], how="left")
        .with_columns((pl.col("selected").fill_null(False) & pl.col("member")).alias("selected"))
        .drop("_day", "_qv")
        .sort(["decision_ns", "symbol"])
    )


@pytest.mark.parametrize("seed", range(6))
def test_liquidity_selection_matches_the_reference_without_its_quadratic_join(seed: int) -> None:
    """Campaign c1 hit the 2 GB research gate: the trailing volume joined every (symbol, day) with all of that
    symbol's days (155 symbols x 1,100 days^2 = 190 M rows, 4.4 GB on a 36-month 1d panel). The rewrite uses
    cumulative sums with as-of joins; the selection must be identical (integer volumes keep the sums exact)."""
    rng = np.random.default_rng(seed)
    day, hour = 86_400 * 10**9, 3_600 * 10**9
    rows: list[tuple[str, int, int | None, float, bool]] = []
    for k in range(7):
        for d in range(60):
            if rng.random() < 0.15:  # missing days and late listings
                continue
            for h in (0, 8, 16):
                t = T0 + d * day + h * hour
                bar = None if rng.random() < 0.05 else t - hour
                rows.append((f"S{k}", t, bar, float(rng.integers(0, 50)), bool(rng.random() < 0.9)))
    frame = pl.DataFrame(
        rows,
        schema={
            "symbol": pl.String,
            "decision_ns": pl.Int64,
            "bar_start_ns": pl.Int64,
            "quote_volume": pl.Float64,
            "member": pl.Boolean,
        },
        orient="row",
    )
    for n, window in ((2, 30), (3, 7), (5, 1)):
        got = select_top_liquidity(frame, n, window_days=window)
        want = _reference_select_top_liquidity(frame, n, window_days=window)
        assert got.select("symbol", "decision_ns", "selected").equals(want.select("symbol", "decision_ns", "selected"))
        assert got["selected"].sum() > 0  # not a pass by both selecting nothing


def test_a_non_finite_volume_counts_as_zero_and_does_not_poison_later_days() -> None:
    """Review of F-020: with cumulative sums a NaN / inf bar volume made every later trailing sum NaN, ranked first,
    so the symbol was selected nearly every day. It counts as zero volume."""
    day, hour = 86_400 * 10**9, 3_600 * 10**9
    rows: list[tuple[str, int, int, float, bool]] = []
    for d in range(20):
        for sym, vol in (("A", 10.0), ("B", 20.0)):
            bad = sym == "A" and d == 2
            rows.append((sym, T0 + d * day, T0 + d * day - hour, math.nan if bad else vol, True))
    frame = pl.DataFrame(
        rows,
        schema={
            "symbol": pl.String,
            "decision_ns": pl.Int64,
            "bar_start_ns": pl.Int64,
            "quote_volume": pl.Float64,
            "member": pl.Boolean,
        },
        orient="row",
    )
    chosen = select_top_liquidity(frame, 1, window_days=3).filter(pl.col("selected"))
    assert set(chosen["symbol"].to_list()) == {"B"}  # B always trades more; A's NaN day never makes A "largest"
