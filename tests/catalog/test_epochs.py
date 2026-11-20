"""v2.2 B2 (ADR-040): registering seal epoch 2 (historical + forward holdout) for the ADR-013 datasets."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from quant_research.catalog.epochs import EPOCH2_WINDOWS, register_epoch2
from quant_research.catalog.store import Catalog, Dataset, LagPolicy, SealEpoch


def _ns(y: int, m: int, d: int = 1) -> int:
    return int(datetime(y, m, d, tzinfo=UTC).timestamp()) * 10**9


def test_epoch2_moves_only_the_adr013_datasets_and_is_idempotent(tmp_path: Path) -> None:
    c = Catalog.open(tmp_path / "c.db")
    try:
        for ds, rationale, until, holdout in (
            ("binance.um.bulk.klines_1m", "ADR-013: research < 2026-07-01", _ns(2026, 7), _ns(2026, 11)),
            ("binance.um.book_ticker_1s", "ADR-011 T0 pre-registration", _ns(2026, 12), _ns(2027, 1)),
        ):
            c.register_dataset(Dataset(ds, "binance", "interval_start", 60_000, "t"))
            c.add_lag_policy(LagPolicy(ds, 0, "t"))
            c.add_seal_epoch(SealEpoch(ds, 1, until, holdout, rationale))
        planned = register_epoch2(c, apply=False)
        assert planned == ["binance.um.bulk.klines_1m"] and c.seal("binance.um.bulk.klines_1m").epoch == 1  # dry run
        assert register_epoch2(c, apply=True) == ["binance.um.bulk.klines_1m"]
        assert c.seal("binance.um.bulk.klines_1m").epoch == 2 and c.seal("binance.um.book_ticker_1s").epoch == 1
        assert c.research_windows("binance.um.bulk.klines_1m") == [
            (_ns(2020, 1), _ns(2023, 1)),
            (_ns(2024, 12), _ns(2026, 7)),
        ]
        assert register_epoch2(c, apply=True) == []  # already on epoch 2: nothing to do
        assert {w.role for w in EPOCH2_WINDOWS} == {"research", "holdout_hist", "holdout_fwd"}
    finally:
        c.close()


def test_bootstrap_seals_every_bulk_dataset_with_epoch_2_from_the_start(tmp_path: Path) -> None:
    """v2.2 B4: download tools call `bootstrap`; a bulk dataset it registers later (a new data tier) got only the
    ADR-013 epoch 1, under which research may read the historical holdout until someone reran the epoch-2 tool.
    Bootstrap now moves every ADR-013 epoch-1 dataset to epoch 2 itself (idempotent)."""
    from quant_research.catalog.bootstrap import BULK_DATASETS, bootstrap

    c = Catalog.open(tmp_path / "c.db")
    try:
        bootstrap(c)
        for d in BULK_DATASETS:
            assert c.seal(d.dataset_id).epoch == 2, d.dataset_id
            assert c.research_windows(d.dataset_id) == [(_ns(2020, 1), _ns(2023, 1)), (_ns(2024, 12), _ns(2026, 7))]
        assert c.seal("binance.um.book_ticker_1s").epoch == 1  # T0 collection datasets keep their own epoch
        bootstrap(c)  # a rerun changes nothing
        assert all(c.seal(d.dataset_id).epoch == 2 for d in BULK_DATASETS)
    finally:
        c.close()


def test_a_concurrent_epoch_2_registration_is_a_no_op_for_the_loser(tmp_path: Path) -> None:
    """B4 review 2: two processes running bootstrap computed the same candidates; the later one raised
    'epochs are consecutive' and killed the tool. A dataset another process already moved is skipped."""
    from quant_research.catalog.epochs import epoch2_candidates

    c = Catalog.open(tmp_path / "c.db")
    try:
        c.register_dataset(Dataset("binance.um.bulk.klines_1m", "binance", "interval_start", 60_000, "t"))
        c.add_lag_policy(LagPolicy("binance.um.bulk.klines_1m", 0, "t"))
        c.add_seal_epoch(SealEpoch("binance.um.bulk.klines_1m", 1, _ns(2026, 7), _ns(2026, 11), "ADR-013: t"))
        stale = epoch2_candidates(c)  # what the slower process saw
        assert register_epoch2(c, apply=True) == ["binance.um.bulk.klines_1m"]  # the faster process
        assert register_epoch2(c, apply=True, candidates=stale) == []  # the slower one: nothing left, no error
        assert c.seal("binance.um.bulk.klines_1m").epoch == 2
    finally:
        c.close()


def test_a_registration_lost_to_another_process_is_accepted_only_if_the_row_now_exists() -> None:
    """B4 review 9: two tools bootstrapping a new dataset at once: the later insert fails although the row it
    wanted is there now; anything else is re-raised."""
    import sqlite3

    import pytest

    from quant_research.catalog.bootstrap import tolerate_race

    def lost() -> None:
        raise sqlite3.IntegrityError("UNIQUE constraint failed")

    assert tolerate_race(lost, exists=lambda: True) is False  # the other process registered it
    with pytest.raises(sqlite3.IntegrityError):
        tolerate_race(lost, exists=lambda: False)
    assert tolerate_race(lambda: None, exists=lambda: False) is True
