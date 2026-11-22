"""v2.3 step 3 (ADR-049): which datasets the research role may read in each campaign window, and their version."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quant_research.catalog.coverage import research_coverage
from quant_research.catalog.store import Catalog, Dataset, SealEpoch, SealWindow


def _ns(y: int, m: int) -> int:
    return int(datetime(y, m, 1, tzinfo=UTC).timestamp()) * 10**9


W1 = (_ns(2020, 1), _ns(2023, 1))
W2 = (_ns(2024, 12), _ns(2026, 7))
WINDOWS = {"w1": W1, "w2": W2}


@pytest.fixture
def catalog(tmp_path: Path) -> Iterator[Catalog]:
    c = Catalog.open(tmp_path / "catalog.db")
    yield c
    c.close()


def _sealed(c: Catalog, ds: str) -> None:
    c.register_dataset(Dataset(ds, "binance", "interval_end", 60_000, "t"))
    e = SealEpoch(ds, 2, research_until_ns=_ns(2026, 7), holdout_until_ns=_ns(2026, 11), rationale="t")
    c.add_seal_windows(
        e,
        [
            SealWindow("research", W1[0], W1[1]),
            SealWindow("holdout_hist", W1[1], W2[0]),
            SealWindow("research", W2[0], W2[1]),
            SealWindow("holdout_fwd", W2[1], _ns(2026, 11)),
        ],
    )


def _file(c: Catalog, ds: str, month: str, fetched: int, symbol: str = "BTCUSDT") -> None:
    c.conn.execute(
        "insert into bulk_file(url, last_modified, dataset_id, symbol, month, sha256, zip_bytes, rows_written, "
        "lake_path, fetched_ns) values (?,?,?,?,?,?,?,?,?,?)",
        (f"u/{ds}/{symbol}/{month}", "lm", ds, symbol, month, "h", 1, 1, "p", fetched),
    )


def test_a_sealed_dataset_with_files_in_a_window_is_available_there_only(catalog: Catalog) -> None:
    _sealed(catalog, "binance.um.bulk.funding")
    _file(catalog, "binance.um.bulk.funding", "2021-03", 10)
    _file(catalog, "binance.um.bulk.funding", "2023-06", 11)  # holdout month: not research coverage
    cov = research_coverage(catalog, WINDOWS)
    assert "binance.um.bulk.funding" in cov["w1"] and "binance.um.bulk.funding" not in cov["w2"]


def test_the_version_changes_when_files_are_added_inside_the_window(catalog: Catalog) -> None:
    _sealed(catalog, "binance.um.bulk.metrics_5m")
    _file(catalog, "binance.um.bulk.metrics_5m", "2025-02", 10)
    before = research_coverage(catalog, WINDOWS)["w2"]["binance.um.bulk.metrics_5m"]
    _file(catalog, "binance.um.bulk.metrics_5m", "2026-06", 20, symbol="ETHUSDT")
    after = research_coverage(catalog, WINDOWS)["w2"]["binance.um.bulk.metrics_5m"]
    assert before != after
    _file(catalog, "binance.um.bulk.metrics_5m", "2026-08", 30, symbol="SOLUSDT")  # forward holdout month
    assert research_coverage(catalog, WINDOWS)["w2"]["binance.um.bulk.metrics_5m"] == after


@pytest.mark.parametrize(
    ("month", "window"),
    [("2023-01", None), ("2024-11", None), ("2026-07", None), ("2022-12", "w1"), ("2024-12", "w2"), ("2026-06", "w2")],
)
def test_the_window_edges_are_month_exact_with_an_exclusive_end(
    catalog: Catalog, month: str, window: str | None
) -> None:
    """Review (step 3) 4: a file in the first holdout month after a window, or just before one, is not coverage."""
    _sealed(catalog, "binance.um.bulk.funding")
    _file(catalog, "binance.um.bulk.funding", month, 10)
    cov = research_coverage(catalog, WINDOWS)
    assert [w for w, have in cov.items() if have] == ([] if window is None else [window])


def test_a_research_window_shorter_than_the_campaign_window_gives_no_coverage(catalog: Catalog) -> None:
    ds = "binance.um.bulk.metrics_5m"
    catalog.register_dataset(Dataset(ds, "binance", "interval_end", 300_000, "t"))
    e = SealEpoch(ds, 2, research_until_ns=_ns(2026, 5), holdout_until_ns=_ns(2026, 11), rationale="t")
    catalog.add_seal_windows(
        e, [SealWindow("research", W2[0], _ns(2026, 5)), SealWindow("holdout_fwd", _ns(2026, 5), _ns(2026, 11))]
    )
    _file(catalog, ds, "2025-03", 10)
    assert research_coverage(catalog, WINDOWS)["w2"] == {}  # its rows would end inside the campaign window


def test_an_unsealed_dataset_is_never_available_to_research(catalog: Catalog) -> None:
    catalog.register_dataset(Dataset("binance.um.bulk.agg_flow_1m", "binance", "interval_end", 60_000, "t"))
    _file(catalog, "binance.um.bulk.agg_flow_1m", "2025-02", 10)
    cov = research_coverage(catalog, WINDOWS)
    assert cov == {"w1": {}, "w2": {}}
