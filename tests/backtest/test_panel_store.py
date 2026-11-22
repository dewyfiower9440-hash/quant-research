"""v2.2 B3 (D7): cached per-symbol panel shards and the point-in-time top-M universe panel."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from quant_research.backtest.panel import PanelConfig, load_panel
from quant_research.backtest.panel_store import (
    PanelStore,
    ShardError,
    Window,
    load_universe_panel,
    members_by_month,
    top_m_by_month,
    window_symbols,
)
from quant_research.catalog.bootstrap import bootstrap
from quant_research.catalog.store import Catalog, Instrument, LagPolicy
from quant_research.data.access import PointInTime, SealViolationError
from quant_research.data.lake import write_frame
from quant_research.strategy.ops import KLINES, MARK, SPOT

M = 60 * 10**9
H = 60 * M
D = 24 * H
JAN = 1_767_225_600 * 10**9  # 2026-01-01 (research under epoch 1 of the bootstrap prior)
FEB = JAN + 31 * D
MAR = FEB + 28 * D
RULE = "binance.um.pit_top100.v1"
WINDOW = Window(JAN, MAR)
CONFIG = PanelConfig(H)


def _int(value: object) -> int:
    if not isinstance(value, int):
        raise TypeError(f"expected an integer, got {value!r}")
    return value


def _minutes(start: int, end: int, volume: float, *, seed: int = 0) -> pl.DataFrame:
    n = (end - start) // M
    rng = np.random.default_rng(seed)
    price = 100.0 * np.exp(np.cumsum(0.001 * rng.standard_normal(n)))
    return pl.DataFrame(
        {
            "event_ns": start + np.arange(n, dtype=np.int64) * M,
            "open": price,
            "high": price * 1.001,
            "low": price * 0.999,
            "close": price,
            "volume": np.ones(n),
            "quote_volume": np.full(n, volume),
        }
    )


def _write(
    root: Path, symbol: str, month: str, start: int, end: int, volume: float, *, version: str = "v1", ds: str = KLINES
) -> None:
    target = root / "lake" / ds / month / f"{symbol}-{month}-{version}.parquet"
    write_frame(_minutes(start, end, volume, seed=hash((symbol, month)) % 1000), target, symbol=symbol)


def _full(pit: PointInTime, root: Path, symbol: str, datasets: tuple[str, ...] = (KLINES,)) -> pl.DataFrame:
    panel = load_panel(
        pit, root, [symbol], first_bar_ns=JAN, last_bar_ns=MAR - 2 * H, config=CONFIG, rule=RULE, datasets=datasets
    )
    return panel.frame


@pytest.fixture
def env(tmp_path: Path) -> Generator[tuple[Catalog, Path]]:
    catalog = Catalog.open(tmp_path / "c.db")
    bootstrap(catalog)
    spans: dict[str, list[tuple[int, int | None]]] = {
        "AUSDT": [(JAN, None)],
        "BUSDT": [(FEB, None)],
        "CUSDT": [(FEB + 9 * D, FEB + 19 * D)],
        "DUSDT": [(MAR + D, None)],  # only after the window
        "EUSDT": [(JAN, JAN + 5 * D), (FEB + 10 * D, None)],  # two spans with a 36-day gap
    }
    for symbol, intervals in spans.items():
        iid = catalog.add_instrument(Instrument("binance", symbol, symbol[:-4], "USDT", "usdm_perp"))
        for lo, hi in intervals:
            catalog.add_membership(iid, RULE, from_ns=lo, to_ns=hi)
    # January volume: B (5) > E (3) > A (1); C trades in February only
    for symbol, volume in (("AUSDT", 1.0), ("BUSDT", 5.0), ("EUSDT", 3.0)):
        _write(tmp_path, symbol, "2026-01", JAN, FEB, volume)
        _write(tmp_path, symbol, "2026-02", FEB, MAR, volume)
    _write(tmp_path, "CUSDT", "2026-02", FEB + 5 * D, FEB + 20 * D + 630 * M, 9.0)  # listed and stopped in Feb
    _write(tmp_path, "AUSDT", "2026-01", JAN, FEB, 1.0, ds=MARK)
    _write(tmp_path, "AUSDT", "2026-02", FEB, MAR, 1.0, ds=MARK)
    yield catalog, tmp_path
    catalog.close()


def test_window_symbols_are_the_members_overlapping_the_window(env: tuple[Catalog, Path]) -> None:
    catalog, _root = env
    assert window_symbols(PointInTime(catalog), RULE, WINDOW) == ["AUSDT", "BUSDT", "CUSDT", "EUSDT"]


def test_membership_for_the_ranking_is_taken_at_each_months_start(env: tuple[Catalog, Path]) -> None:
    catalog, _root = env
    members = members_by_month(PointInTime(catalog), RULE, WINDOW)
    assert members == {"2026-01": {"AUSDT", "EUSDT"}, "2026-02": {"AUSDT", "BUSDT"}}  # C, E join after Feb 1


def test_a_shard_is_built_once_and_rebuilt_when_its_inputs_change(env: tuple[Catalog, Path]) -> None:
    catalog, root = env
    store = PanelStore(root / "cache" / "panels")
    pit = PointInTime(catalog)
    meta, built = store.shard(pit, root, "AUSDT", WINDOW, CONFIG, rule=RULE, datasets=(KLINES,))
    assert built and meta.rows > 0 and meta.member_rows > 0
    again, built_again = store.shard(pit, root, "AUSDT", WINDOW, CONFIG, rule=RULE, datasets=(KLINES,))
    assert not built_again and again == meta
    _write(root, "AUSDT", "2026-02", FEB, MAR, 2.0, version="v2")  # an upstream regeneration: newest version wins
    changed, rebuilt = store.shard(pit, root, "AUSDT", WINDOW, CONFIG, rule=RULE, datasets=(KLINES,))
    assert rebuilt and changed.key != meta.key
    assert any(p.endswith("AUSDT-2026-02-v2.parquet") for p, _s in changed.files)
    other, built_other = store.shard(pit, root, "AUSDT", WINDOW, PanelConfig(4 * H), rule=RULE, datasets=(KLINES,))
    assert built_other and other.key != changed.key  # the timeframe is part of the key
    # second review: a re-calibrated (larger) lag must not reuse a shard built with the old, shorter one
    catalog.add_lag_policy(LagPolicy(KLINES, 30 * M, "test: re-calibrated"))
    lagged, rebuilt_lag = store.shard(pit, root, "AUSDT", WINDOW, CONFIG, rule=RULE, datasets=(KLINES,))
    assert rebuilt_lag and lagged.key != changed.key


def test_a_multiplier_contract_reads_its_spot_pair_under_the_spot_name_through_the_store(
    env: tuple[Catalog, Path],
) -> None:
    """Review (v2.3 step 7) 1: the loader read the spot pair as SHIBUSDT but the cache key looked for 1000SHIBUSDT
    files, so every shard build failed its file check once spot data existed."""
    catalog, root = env
    iid = catalog.add_instrument(Instrument("binance", "1000SHIBUSDT", "SHIB", "USDT", "usdm_perp"))
    catalog.add_membership(iid, RULE, from_ns=JAN, to_ns=None)
    _write(root, "1000SHIBUSDT", "2026-01", JAN, FEB, 1.0)
    _write(root, "1000SHIBUSDT", "2026-02", FEB, MAR, 1.0)
    _write(root, "SHIBUSDT", "2026-01", JAN, FEB, 1.0, ds=SPOT)
    _write(root, "SHIBUSDT", "2026-02", FEB, MAR, 1.0, ds=SPOT)
    store = PanelStore(root / "cache" / "panels")
    pit = PointInTime(catalog)
    meta, built = store.shard(pit, root, "1000SHIBUSDT", WINDOW, CONFIG, rule=RULE, datasets=(KLINES, SPOT))
    assert built and any("/SHIBUSDT-2026-01-v1.parquet" in p for p, _s in meta.files)
    universe = load_universe_panel(pit, root, store, WINDOW, CONFIG, top_m=None, rule=RULE, datasets=(KLINES, SPOT))
    shib = universe.panel.frame.filter((pl.col("symbol") == "1000SHIBUSDT") & pl.col("spot_close").is_not_null())
    assert shib.height > 0
    # the synthetic spot pair trades near 100 like every series here; at the contract's level it is x 1000
    assert float(np.min(shib["spot_close"].to_numpy())) > 10_000.0
    _write(root, "SHIBUSDT", "2026-02", FEB, MAR, 2.0, version="v2", ds=SPOT)  # a spot file change rebuilds
    changed, rebuilt = store.shard(pit, root, "1000SHIBUSDT", WINDOW, CONFIG, rule=RULE, datasets=(KLINES, SPOT))
    assert rebuilt and changed.key != meta.key


def test_a_shard_equals_the_full_panel_on_every_row_including_membership_gaps(env: tuple[Catalog, Path]) -> None:
    """Review finding 1: features are row-based (shift / rolling / ewm), so dropping the rows of a gap between
    membership spans made a lag of 240 bars reach weeks back. The shard keeps every row between its first bar and
    its last executable interval and equals `load_panel` there; a row-based feature agrees at every member row."""
    catalog, root = env
    store = PanelStore(root / "cache" / "panels")
    pit = PointInTime(catalog)
    for symbol in ("EUSDT", "CUSDT"):
        meta, _ = store.shard(pit, root, symbol, WINDOW, CONFIG, rule=RULE, datasets=(KLINES,))
        shard, _ = store.read(meta)
        full = _full(pit, root, symbol)
        kept = full.filter(pl.col("decision_ns").is_between(shard["decision_ns"].min(), shard["decision_ns"].max()))
        assert shard.height == kept.height == meta.rows
        assert shard["decision_ns"].to_list() == kept["decision_ns"].to_list()  # no row dropped inside
        assert shard["member"].to_list() == kept["member"].to_list()

        def lagged(frame: pl.DataFrame) -> pl.DataFrame:
            return frame.sort("decision_ns").with_columns(pl.col("close").shift(240).alias("f")).filter("member")

        a, b = lagged(shard), lagged(full)
        assert a["decision_ns"].to_list() == b["decision_ns"].to_list()
        both = a.select("f").with_columns(b["f"].alias("g")).drop_nulls()
        assert both.height > 0 and np.allclose(both["f"].to_numpy(), both["g"].to_numpy(), rtol=1e-6)
    c_meta, _ = store.shard(pit, root, "CUSDT", WINDOW, CONFIG, rule=RULE, datasets=(KLINES,))
    c_shard, _ = store.read(c_meta)
    assert _int(c_shard["decision_ns"].min()) >= FEB + 5 * D  # the rows before the listing are cut


def test_shards_store_float32_and_read_back_float64_close_to_the_panel(env: tuple[Catalog, Path]) -> None:
    catalog, root = env
    store = PanelStore(root / "cache" / "panels")
    pit = PointInTime(catalog)
    datasets = (KLINES, MARK)
    meta, _ = store.shard(pit, root, "AUSDT", WINDOW, CONFIG, rule=RULE, datasets=datasets)
    raw = pl.read_parquet(store.root / f"{meta.key}.parquet")
    assert raw.schema["close"] == pl.Float32 and raw.schema["mark_close"] == pl.Float32
    assert raw.schema["decision_ns"] == pl.Int64 and raw.schema["bar_start_ns"] == pl.Int64
    frame, _ = store.read(meta)
    assert frame.schema["close"] == pl.Float64
    joined = frame.join(_full(pit, root, "AUSDT", datasets), on=["decision_ns", "symbol"], suffix="_f")
    for col in ("close", "mark_close", "mark_exec_high", "quote_volume"):
        pair = joined.select(col, f"{col}_f").drop_nulls()
        assert pair.height > 0 and np.allclose(pair[col].to_numpy(), pair[f"{col}_f"].to_numpy(), rtol=1e-6)
    # the January volume known at the first February decision: every January bar (the last one is available then)
    assert meta.rank_qv["2026-01"] == pytest.approx(31 * 1_440 * 1.0, rel=1e-6)


def test_a_shard_whose_parquet_does_not_match_its_meta_is_refused(env: tuple[Catalog, Path]) -> None:
    catalog, root = env
    store = PanelStore(root / "cache" / "panels")
    meta, _ = store.shard(PointInTime(catalog), root, "AUSDT", WINDOW, CONFIG, rule=RULE, datasets=(KLINES,))
    pl.read_parquet(store.root / f"{meta.key}.parquet").head(3).write_parquet(store.root / f"{meta.key}.parquet")
    with pytest.raises(ShardError, match="rows"):
        store.read(meta)


def test_top_m_ranks_each_month_by_the_previous_months_volume() -> None:
    qv = {"A": {"2026-01": 1.0, "2026-02": 50.0}, "B": {"2026-01": 5.0}, "C": {"2026-02": 9.0}}
    members = {"2026-01": {"A"}, "2026-02": {"A", "B", "C"}, "2026-03": {"A", "B", "C"}}
    top = top_m_by_month(qv, members, ["2026-01", "2026-02", "2026-03"], m=1)
    assert top["2026-01"] == set()  # first month of the window: no earlier month inside it (warm-up)
    assert top["2026-02"] == {"B"}  # January: B 5 > A 1
    assert top["2026-03"] == {"A"}  # February: A 50 > C 9; B has no February volume
    assert top_m_by_month(qv, members, ["2026-01", "2026-02"], m=5)["2026-02"] == {"A", "B"}  # C: no Jan volume
    with pytest.raises(ValueError, match="m"):
        top_m_by_month(qv, members, ["2026-01"], m=0)


def test_universe_panel_marks_members_by_the_top_m_and_reuses_shards(env: tuple[Catalog, Path]) -> None:
    catalog, root = env
    store = PanelStore(root / "cache" / "panels")
    pit = PointInTime(catalog)
    first = load_universe_panel(pit, root, store, WINDOW, CONFIG, top_m=1, rule=RULE, datasets=(KLINES,))
    assert (first.built, first.reused) == (4, 0)
    frame = first.panel.frame
    members = frame.filter(pl.col("member"))
    # February's universe: the Feb-1 members (A, B) ranked by January volume -> B; E rejoins on Feb 11 only
    assert set(members["symbol"].unique().to_list()) == {"BUSDT"}
    assert _int(members["decision_ns"].min()) >= FEB
    assert first.panel.evaluate_from_ns == FEB  # January is warm-up only
    assert first.panel.files == sorted(set(first.panel.files)) and first.panel.files
    second = load_universe_panel(pit, root, store, WINDOW, CONFIG, top_m=None, rule=RULE, datasets=(KLINES,))
    assert (second.built, second.reused) == (0, 4)
    chosen = set(second.panel.frame.filter(pl.col("member"))["symbol"].unique().to_list())
    assert chosen == {"AUSDT", "BUSDT", "CUSDT", "EUSDT"}
    assert second.panel.evaluate_from_ns is None


def test_a_window_reaching_into_the_seal_is_refused_for_research(env: tuple[Catalog, Path]) -> None:
    catalog, root = env
    store = PanelStore(root / "cache" / "panels")
    sealed = Window(JAN, 1_782_864_000 * 10**9 + 2 * D)  # past research_until (2026-07-01)
    with pytest.raises(SealViolationError):
        store.shard(PointInTime(catalog), root, "AUSDT", sealed, CONFIG, rule=RULE, datasets=(KLINES,))
    assert not list(store.root.glob("*.json"))  # nothing cached


def test_window_rejects_an_empty_span() -> None:
    with pytest.raises(ValueError, match="window"):
        Window(MAR, JAN)


def test_a_symbol_that_stops_trading_keeps_the_row_that_sees_its_last_bar(env: tuple[Catalog, Path]) -> None:
    """Re-review: rows after the last executable interval are cut, but the first decision that sees the last bar
    is kept (L0 marks an open position to that close, as on the full panel)."""
    catalog, root = env
    store = PanelStore(root / "cache" / "panels")
    pit = PointInTime(catalog)
    meta, _ = store.shard(pit, root, "CUSDT", WINDOW, CONFIG, rule=RULE, datasets=(KLINES,))
    shard, _ = store.read(meta)
    full = _full(pit, root, "CUSDT")
    last_bar = _int(full["bar_start_ns"].max())
    tail = shard.sort("decision_ns").tail(1)
    assert _int(tail["bar_start_ns"][0]) == last_bar
    first_seeing = full.filter(pl.col("bar_start_ns") == last_bar).sort("decision_ns").head(1)
    assert tail["decision_ns"][0] == first_seeing["decision_ns"][0]
    assert tail["close"][0] == pytest.approx(first_seeing["close"][0], rel=1e-6)
    assert shard.height < full.filter(pl.col("decision_ns") >= shard["decision_ns"].min()).height  # rest cut


def test_the_ranking_volume_counts_only_bars_available_at_the_months_first_decision(
    env: tuple[Catalog, Path],
) -> None:
    """Re-review (a): with a 30 min klines lag the last January 1h bar is not yet visible at the first February
    decision (00:01), so January's ranking volume lacks that bar's 60 minutes."""
    catalog, root = env
    catalog.add_lag_policy(LagPolicy(KLINES, 30 * M, "test: slower publication"))
    store = PanelStore(root / "cache" / "panels")
    meta, _ = store.shard(PointInTime(catalog), root, "AUSDT", WINDOW, CONFIG, rule=RULE, datasets=(KLINES,))
    assert meta.rank_qv["2026-01"] == pytest.approx(31 * 1_440 * 1.0 - 60.0, rel=1e-9)


def test_a_lake_file_appearing_during_a_build_is_refused(
    env: tuple[Catalog, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-review (c): the key and meta name the files listed before the build; a newer file version written
    while `load_panel` runs would be read but not named, so the build is refused and nothing is cached. The race
    is simulated at the filesystem boundary: the key's listing does not yet see the regenerated file."""
    from collections.abc import Iterable

    import quant_research.backtest.panel_store as store_module

    catalog, root = env
    _write(root, "AUSDT", "2026-02", FEB, MAR, 7.0, version="v9")  # appears after the key's listing
    real = store_module.bulk_files

    def listed_before(data_root: Path, dataset_id: str, symbols: Iterable[str], months: Iterable[str]) -> list[Path]:
        older = [f for f in real(data_root, dataset_id, symbols, months) if not f.name.endswith("-v9.parquet")]
        v1 = data_root / "lake" / dataset_id / "2026-02" / "AUSDT-2026-02-v1.parquet"
        return sorted({*older, v1}) if v1.exists() and "2026-02" in set(months) else older

    monkeypatch.setattr(store_module, "bulk_files", listed_before)
    store = PanelStore(root / "cache" / "panels")
    with pytest.raises(ShardError, match="changed"):
        store.shard(PointInTime(catalog), root, "AUSDT", WINDOW, CONFIG, rule=RULE, datasets=(KLINES,))
    assert not list(store.root.glob("*.json"))
