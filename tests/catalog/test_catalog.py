"""Step 5: catalog schema, migrations, and the invariants design v2.1 relies on (§5.3-5.4, §9-2)."""

import json
import sqlite3
from pathlib import Path

import pytest

from quant_research.catalog.store import (
    Catalog,
    CatalogError,
    Dataset,
    Instrument,
    LagPolicy,
    SealEpoch,
)

NS = 10**9
DAY = 86_400 * NS


def _catalog(tmp_path: Path) -> Catalog:
    return Catalog.open(tmp_path / "catalog.db")


def _dataset(name: str = "binance.um.book_ticker_1s") -> Dataset:
    return Dataset(name, venue="binance", time_semantics="event", interval_ms=None, description="1 s L1")


def test_migrations_create_schema_once_and_record_version(tmp_path: Path) -> None:
    c = _catalog(tmp_path)
    version = c.schema_version()
    c.close()
    again = _catalog(tmp_path)  # re-opening must not re-apply or fail
    assert again.schema_version() == version >= 1
    tables = {r[0] for r in again.conn.execute("select name from sqlite_master where type='table'")}
    assert {"instrument", "dataset", "gap", "seal_policy", "universe_membership", "lag_policy"} <= tables
    assert again.conn.execute("pragma foreign_keys").fetchone()[0] == 1
    assert again.conn.execute("pragma journal_mode").fetchone()[0] == "wal"
    again.close()


def test_dataset_time_semantics_is_a_closed_set(tmp_path: Path) -> None:
    c = _catalog(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        c.conn.execute(
            "insert into dataset(dataset_id, venue, time_semantics, registered_ns) values ('x','binance','sometimes',1)"
        )
    c.close()


def test_instrument_lineage_links_a_renamed_contract_to_its_successor(tmp_path: Path) -> None:
    c = _catalog(tmp_path)
    old = c.add_instrument(Instrument("binance", "MATICUSDT", "MATIC", "USDT", "usdm_perp", onboard_ns=1, delist_ns=5))
    new = c.add_instrument(Instrument("binance", "POLUSDT", "POL", "USDT", "usdm_perp", onboard_ns=5))
    c.link_successor(old, new)
    assert c.lineage(new) == [old, new]
    with pytest.raises(sqlite3.IntegrityError):  # same venue symbol listed twice at the same onboard time
        c.add_instrument(Instrument("binance", "POLUSDT", "POL", "USDT", "usdm_perp", onboard_ns=5))
    c.close()


def test_lag_policy_versions_may_only_grow_and_rows_are_immutable(tmp_path: Path) -> None:
    c = _catalog(tmp_path)
    c.register_dataset(_dataset("binance.um.oi_hist"))
    c.add_lag_policy(LagPolicy("binance.um.oi_hist", lag_ns=60 * NS, rationale="prior: 1 decision bar"))
    c.add_lag_policy(LagPolicy("binance.um.oi_hist", lag_ns=90 * NS, rationale="p99 observed 72 s"))
    assert c.lag_ns("binance.um.oi_hist") == 90 * NS
    assert c.lag_ns("binance.um.oi_hist", version=1) == 60 * NS
    with pytest.raises(CatalogError, match="only grow"):  # Python guard
        c.add_lag_policy(LagPolicy("binance.um.oi_hist", lag_ns=30 * NS, rationale="tuned"))
    with pytest.raises(sqlite3.IntegrityError, match="only grow"):  # SQL trigger, even bypassing Python
        c.conn.execute(
            "insert into lag_policy(dataset_id, version, lag_ns, rationale, decided_ns) "
            "values ('binance.um.oi_hist', 3, 1, 'x', 1)"
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        c.conn.execute("update lag_policy set lag_ns = 1 where version = 2")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        c.conn.execute("delete from lag_policy")
    c.close()


def test_seal_epochs_are_immutable_and_cannot_move_backwards(tmp_path: Path) -> None:
    c = _catalog(tmp_path)
    c.register_dataset(_dataset())
    e1 = SealEpoch(
        "binance.um.book_ticker_1s", 1, research_until_ns=60 * DAY, holdout_until_ns=90 * DAY, rationale="T0"
    )
    c.add_seal_epoch(e1)
    assert c.seal("binance.um.book_ticker_1s") == e1
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        c.conn.execute("update seal_policy set research_until_ns = 999 * 86400000000000")
    with pytest.raises(CatalogError, match="backwards"):
        c.add_seal_epoch(SealEpoch("binance.um.book_ticker_1s", 2, 50 * DAY, 120 * DAY, rationale="x"))
    with pytest.raises(CatalogError, match="holdout"):
        c.add_seal_epoch(SealEpoch("binance.um.book_ticker_1s", 2, 120 * DAY, 100 * DAY, rationale="x"))
    c.add_seal_epoch(SealEpoch("binance.um.book_ticker_1s", 2, 90 * DAY, 120 * DAY, rationale="roll"))
    assert c.seal("binance.um.book_ticker_1s").epoch == 2
    c.close()


def test_research_boundary_is_unsealed_when_no_policy_exists_is_an_error(tmp_path: Path) -> None:
    c = _catalog(tmp_path)
    c.register_dataset(_dataset())
    with pytest.raises(CatalogError, match="no seal policy"):
        c.seal("binance.um.book_ticker_1s")
    c.close()


def test_universe_membership_is_point_in_time(tmp_path: Path) -> None:
    c = _catalog(tmp_path)
    btc = c.add_instrument(Instrument("binance", "BTCUSDT", "BTC", "USDT", "usdm_perp", onboard_ns=0))
    doge = c.add_instrument(Instrument("binance", "DOGEUSDT", "DOGE", "USDT", "usdm_perp", onboard_ns=0))
    c.add_membership(btc, "top50-v1", from_ns=0, to_ns=None)
    c.add_membership(doge, "top50-v1", from_ns=10, to_ns=20)
    assert c.members("top50-v1", at_ns=5) == [btc]
    assert c.members("top50-v1", at_ns=15) == [btc, doge]
    assert c.members("top50-v1", at_ns=20) == [btc]  # to_ns is exclusive
    with pytest.raises(CatalogError, match="empty"):
        c.add_membership(doge, "top50-v1", from_ns=30, to_ns=30)
    c.close()


def test_gap_import_from_raw_logs_is_idempotent_and_keeps_unknown_starts(tmp_path: Path) -> None:
    raw = tmp_path / "raw" / "_gaps"
    raw.mkdir(parents=True)
    rows = [
        {"source": "process.rest", "start_ns": None, "end_ns": 5, "cause": "restart"},
        {"source": "process.rest", "start_ns": 7, "end_ns": 9, "cause": "restart"},
        {"source": "upbit.trades", "start_ns": 1, "end_ns": 3, "cause": "api_window_exceeded:KRW-BTC"},
    ]
    (raw / "process.rest.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows[:2]), encoding="utf-8")
    (raw / "upbit.trades.jsonl").write_text(json.dumps(rows[2]) + "\n" + '{"torn', encoding="utf-8")
    c = _catalog(tmp_path)
    first = c.import_gaps(tmp_path / "raw")
    second = c.import_gaps(tmp_path / "raw")
    assert (first.imported, first.skipped_lines) == (3, 1)
    assert second.imported == 0
    got = c.conn.execute("select source, start_ns, end_ns, cause, detail from gap order by gap_id").fetchall()
    assert got == [
        ("process.rest", None, 5, "restart", None),
        ("process.rest", 7, 9, "restart", None),
        ("upbit.trades", 1, 3, "api_window_exceeded", "KRW-BTC"),
    ]
    c.close()


def test_gap_cause_is_a_closed_set_and_spans_must_be_ordered(tmp_path: Path) -> None:
    c = _catalog(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        c.conn.execute(
            "insert into gap(source, start_ns, end_ns, cause, recorded_ns) values ('s', 1, 2, 'whatever', 1)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        c.conn.execute("insert into gap(source, start_ns, end_ns, cause, recorded_ns) values ('s', 5, 2, 'restart', 1)")
    c.close()


def test_bootstrap_registers_t0_datasets_priors_and_seal_once(tmp_path: Path) -> None:
    from quant_research.catalog.bootstrap import (
        BULK_DATASETS,
        BULK_HOLDOUT_UNTIL_NS,
        BULK_RESEARCH_UNTIL_NS,
        T0_DATASETS,
        T0_START_NS,
        bootstrap,
    )

    c = _catalog(tmp_path)
    first = bootstrap(c)
    second = bootstrap(c)
    n = len(T0_DATASETS) + len(BULK_DATASETS)
    bulk = c.seal("binance.um.bulk.klines_1m")
    assert (bulk.research_until_ns, bulk.holdout_until_ns) == (BULK_RESEARCH_UNTIL_NS, BULK_HOLDOUT_UNTIL_NS)
    assert c.lag_ns("binance.um.bulk.mark_klines_1m") == 60 * NS
    assert (first.datasets_added, first.lag_policies_added, first.seal_epochs_added) == (n, n, n)
    assert (second.datasets_added, second.lag_policies_added, second.seal_epochs_added) == (0, 0, 0)
    assert c.lag_ns("binance.um.oi_hist") == 300 * NS  # unknown interval meaning: one extra interval
    assert c.lag_ns("binance.um.book_ticker_1s") == 0  # live-observed: received_at is used
    seal = c.seal("upbit.trades")
    assert seal.research_until_ns == T0_START_NS + 60 * DAY
    assert seal.holdout_until_ns == T0_START_NS + 90 * DAY
    c.close()
