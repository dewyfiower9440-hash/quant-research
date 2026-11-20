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
