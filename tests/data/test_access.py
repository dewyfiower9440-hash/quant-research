"""Step 6: point-in-time access (design v2.1 §5.4). The leak tests inject future information on purpose."""

import ast
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import polars as pl
import pytest

from quant_research.catalog.store import Catalog, CatalogError, Dataset, LagPolicy, SealEpoch, SealWindow
from quant_research.data.access import PointInTime, SealViolationError
from quant_research.errors import AppError

S = 10**9
MIN5 = 300 * S


@pytest.fixture
def catalog(tmp_path: Path) -> Iterator[Catalog]:
    c = Catalog.open(tmp_path / "catalog.db")
    c.register_dataset(Dataset("oi", "binance", "unknown_interval", 300_000, "5m metric, meaning unknown"))
    c.register_dataset(Dataset("oi_end", "binance", "interval_end", 300_000, "5m metric stamped at its end"))
    c.register_dataset(Dataset("trades", "upbit", "event", None, "events"))
    for ds, lag in (("oi", MIN5), ("oi_end", 0), ("trades", 0)):
        c.add_lag_policy(LagPolicy(ds, lag, "test prior"))
        c.add_seal_epoch(SealEpoch(ds, 1, research_until_ns=10_000 * S, holdout_until_ns=20_000 * S, rationale="t"))
    yield c
    c.close()


def test_interval_stamped_at_its_start_is_not_visible_until_end_plus_lag(catalog: Catalog) -> None:
    """Injected leak: a value stamped at the interval START is only knowable after the interval ends."""
    pit = PointInTime(catalog)
    frame = pl.DataFrame({"event_ns": [0, MIN5, 2 * MIN5], "value": [1.0, 2.0, 3.0]})
    decisions = pl.Series("decision_ns", [MIN5 + 1, 2 * MIN5 + 1, 3 * MIN5 - 1, 3 * MIN5])
    out = pit.join_asof(decisions, frame, "oi")
    # value 1 (stamped 0) becomes available at 0 + 5m + 5m lag = 2*MIN5
    assert out["value"].to_list() == [None, 1.0, 1.0, 2.0]
    assert all(a is None or a <= d for a, d in zip(out["available_ns"], out["decision_ns"], strict=True))


def test_interval_end_semantics_only_adds_the_lag(catalog: Catalog) -> None:
    pit = PointInTime(catalog)
    frame = pl.DataFrame({"event_ns": [MIN5], "value": [7.0]})
    out = pit.join_asof(pl.Series("decision_ns", [MIN5 - 1, MIN5]), frame, "oi_end")
    assert out["value"].to_list() == [None, 7.0]


def test_observed_receive_time_is_used_for_live_events(catalog: Catalog) -> None:
    pit = PointInTime(catalog)
    frame = pl.DataFrame({"event_ns": [100 * S], "received_ns": [102 * S], "value": [1.0]})
    out = pit.join_asof(pl.Series("decision_ns", [101 * S, 102 * S]), frame, "trades")
    assert out["value"].to_list() == [None, 1.0]


def test_future_values_never_leak_into_any_decision(catalog: Catalog) -> None:
    """Property-style check over a dense grid: no returned row is available after its decision time."""
    pit = PointInTime(catalog)
    events = list(range(0, 30 * MIN5, MIN5))  # stays before the seal boundary (10_000 s)
    frame = pl.DataFrame({"event_ns": events, "value": [float(i) for i in range(len(events))]})
    decisions = pl.Series("decision_ns", list(range(0, 30 * MIN5, 37 * S)))
    out = pit.join_asof(decisions, frame, "oi")
    for value, decision in zip(out["value"], out["decision_ns"], strict=True):
        if value is not None:
            assert events[int(value)] + 2 * MIN5 <= decision


def test_per_instrument_join_never_mixes_symbols(catalog: Catalog) -> None:
    pit = PointInTime(catalog)
    frame = pl.DataFrame(
        {"instrument_id": [1, 2], "event_ns": [0, 0], "received_ns": [5 * S, 1 * S], "value": [1.0, 2.0]}
    )
    decisions = pl.DataFrame({"instrument_id": [1, 2], "decision_ns": [2 * S, 2 * S]})
    out = pit.join_asof(decisions, frame, "trades", by="instrument_id")
    assert out.sort("instrument_id")["value"].to_list() == [None, 2.0]


def test_research_cannot_ask_for_decisions_inside_the_sealed_holdout(catalog: Catalog) -> None:
    pit = PointInTime(catalog, role="research")
    frame = pl.DataFrame({"event_ns": [0], "received_ns": [0], "value": [1.0]})
    with pytest.raises(SealViolationError, match="research_until"):
        pit.join_asof(pl.Series("decision_ns", [10_000 * S]), frame, "trades")


def test_research_never_receives_rows_that_become_available_inside_the_holdout(catalog: Catalog) -> None:
    pit = PointInTime(catalog, role="research")
    frame = pl.DataFrame(
        {"event_ns": [9_999 * S, 10_001 * S], "received_ns": [9_999 * S, 10_001 * S], "value": [1.0, 2.0]}
    )
    visible = pit.rows_visible(frame, "trades")
    assert visible["value"].to_list() == [1.0]


def test_validator_role_may_read_the_holdout(catalog: Catalog) -> None:
    pit = PointInTime(catalog, role="validator")
    frame = pl.DataFrame({"event_ns": [10_001 * S], "received_ns": [10_001 * S], "value": [2.0]})
    out = pit.join_asof(pl.Series("decision_ns", [15_000 * S]), frame, "trades")
    assert out["value"].to_list() == [2.0]


def test_pinned_lag_version_is_reproducible(catalog: Catalog) -> None:
    catalog.add_lag_policy(LagPolicy("oi", 2 * MIN5, "observed p99 grew"))
    frame = pl.DataFrame({"event_ns": [0], "value": [1.0]})
    decisions = pl.Series("decision_ns", [2 * MIN5])
    assert PointInTime(catalog).join_asof(decisions, frame, "oi")["value"].to_list() == [None]  # latest: lag 10 min
    # replaying an archived run under its original (superseded) lag must be explicit (review #4)
    pinned = PointInTime(catalog, lag_versions={"oi": 1}, reproduce_archived_run=True).join_asof(decisions, frame, "oi")
    assert pinned["value"].to_list() == [1.0]


def test_event_rows_without_receive_time_use_event_time_plus_lag(catalog: Catalog) -> None:
    catalog.add_lag_policy(LagPolicy("trades", 3 * S, "bulk-only rows: modelled lag"))
    frame = pl.DataFrame({"event_ns": [100 * S], "value": [1.0]})
    out = PointInTime(catalog).join_asof(pl.Series("decision_ns", [102 * S, 103 * S]), frame, "trades")
    assert out["value"].to_list() == [None, 1.0]


ALLOWED_PARQUET_READERS = {Path("src/quant_research/data/lake.py")}
FORBIDDEN_CALLS = {"read_parquet", "scan_parquet", "read_parquet_schema"}
FORBIDDEN_IMPORTS = {"pyarrow", "duckdb"}


def test_architecture_only_the_lake_module_reads_parquet_directly() -> None:
    """v2.1 §5.4: every read goes through the point-in-time API; direct Parquet access is confined."""
    root = Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    for path in sorted((root / "src").rglob("*.py")):
        rel = path.relative_to(root)
        if rel in ALLOWED_PARQUET_READERS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                names = [a.name for a in node.names] + (
                    [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
                )
                if any(n.split(".")[0] in FORBIDDEN_IMPORTS for n in names):
                    offenders.append(f"{rel}:{node.lineno} imports {names}")
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_CALLS:
                offenders.append(f"{rel}:{node.lineno} calls {node.attr}")
    assert offenders == []


def test_by_join_is_correct_for_shuffled_interleaved_inputs(catalog: Catalog) -> None:
    pit = PointInTime(catalog)
    rows = [(inst, t) for t in range(10) for inst in (1, 2, 3)]
    frame = pl.DataFrame(
        {
            "instrument_id": [i for i, _ in rows][::-1],  # reversed: unsorted on purpose
            "event_ns": [t * S for _, t in rows][::-1],
            "received_ns": [t * S for _, t in rows][::-1],
            "value": [float(i * 100 + t) for i, t in rows][::-1],
        }
    )
    decisions = pl.DataFrame({"instrument_id": [3, 1, 2, 1], "decision_ns": [4 * S + 1, 9 * S, 0, 2 * S]})
    out = pit.join_asof(decisions, frame, "trades", by="instrument_id")
    got = {(r["instrument_id"], r["decision_ns"]): r["value"] for r in out.iter_rows(named=True)}
    assert got == {(3, 4 * S + 1): 304.0, (1, 9 * S): 109.0, (2, 0): 200.0, (1, 2 * S): 102.0}


def test_revised_old_bucket_never_replaces_a_newer_bucket(catalog: Catalog) -> None:
    """Review #3: a refetch that revises an old interval must not be returned as the latest value."""
    pit = PointInTime(catalog)
    frame = pl.DataFrame(
        {
            "event_ns": [0, 3000 * S, 0],
            "received_ns": [700 * S, 3700 * S, 4000 * S],  # last row: revision of event 0 received later
            "value": [1, 2, 9],
        }
    )
    out = pit.join_asof(pl.Series("decision_ns", [800 * S, 3800 * S, 5000 * S]), frame, "oi_end")
    assert out["value"].to_list() == [1, 2, 2]
    assert out["event_ns"].to_list() == [0, 3000 * S, 3000 * S]


def test_revision_of_the_newest_bucket_replaces_it_once_available(catalog: Catalog) -> None:
    pit = PointInTime(catalog)
    frame = pl.DataFrame({"event_ns": [0, 0], "received_ns": [10 * S, 20 * S], "value": [1, 5]})
    out = pit.join_asof(pl.Series("decision_ns", [15 * S, 25 * S]), frame, "oi_end")
    assert out["value"].to_list() == [1, 5]


def test_rows_available_at_the_same_instant_resolve_to_the_newest_event(catalog: Catalog) -> None:
    """A backfill receives many buckets at one instant: the answer must not depend on input order."""
    pit = PointInTime(catalog)
    for order in ([0, 1, 2], [2, 0, 1], [1, 2, 0]):
        frame = pl.DataFrame({"event_ns": [t * MIN5 for t in order], "received_ns": [9000 * S] * 3, "value": order})
        out = pit.join_asof(pl.Series("decision_ns", [9000 * S]), frame, "oi_end")
        assert out["value"].to_list() == [2]


def test_revisions_are_resolved_per_instrument(catalog: Catalog) -> None:
    pit = PointInTime(catalog)
    frame = pl.DataFrame(
        {
            "instrument_id": [1, 2, 1],
            "event_ns": [3000 * S, 0, 0],
            "received_ns": [3100 * S, 3200 * S, 4000 * S],  # instrument 2's old bucket is its newest
            "value": [10, 20, 99],
        }
    )
    decisions = pl.DataFrame({"instrument_id": [1, 2], "decision_ns": [5000 * S, 5000 * S]})
    out = pit.join_asof(decisions, frame, "oi_end", by="instrument_id")
    assert dict(zip(out["instrument_id"], out["value"], strict=True)) == {1: 10, 2: 20}


def test_research_cannot_pin_a_superseded_smaller_lag(catalog: Catalog) -> None:
    """Review #4: lag versions only grow; pinning an older one would make data visible earlier."""
    catalog.add_lag_policy(LagPolicy("oi", 2 * MIN5, "observed p99 grew"))
    frame = pl.DataFrame({"event_ns": [0], "value": [1.0]})
    with pytest.raises(SealViolationError, match="superseded"):
        PointInTime(catalog, lag_versions={"oi": 1}).join_asof(pl.Series("decision_ns", [2 * MIN5]), frame, "oi")


@pytest.mark.parametrize("dtype", [pl.Float64, pl.Datetime("us")])
def test_decision_times_must_be_integer_nanoseconds(catalog: Catalog, dtype: pl.DataType) -> None:
    """Review #5: a float or Datetime column used to skip the seal check and mis-scale the join."""
    frame = pl.DataFrame({"event_ns": [0], "value": [1.0]})
    decisions = pl.Series("decision_ns", [20_000 * S]).cast(dtype)
    with pytest.raises(AppError, match="integer nanoseconds"):
        PointInTime(catalog).join_asof(decisions, frame, "oi")


def test_rows_without_event_time_stay_joinable_by_receive_time(catalog: Catalog) -> None:
    """Review-2 #3: the revision filter must not silently drop rows whose event_ns is null."""
    pit = PointInTime(catalog)
    frame = pl.DataFrame({"event_ns": [5 * S, None, None], "received_ns": [5 * S, 6 * S, 7 * S], "value": [1, 2, 3]})
    out = pit.join_asof(pl.Series("decision_ns", [6 * S, 8 * S]), frame, "trades")
    assert out["value"].to_list() == [2, 3]


# ---------------------------------------------------------------- v2.2 B2: seal epoch 2 with windows (ADR-040)
K = 1_000 * S  # the fixture's epoch 1 seals research at 10 K; epoch 2 keeps that boundary (it may not move back)


@pytest.fixture
def windowed(catalog: Catalog) -> Catalog:
    """Epoch 2 of 'trades': research [0, 1K) + [3K, 10K), historical holdout [1K, 3K), forward [10K, 20K)."""
    catalog.add_seal_windows(
        SealEpoch("trades", 2, research_until_ns=10 * K, holdout_until_ns=20 * K, rationale="ADR-040 test"),
        [
            SealWindow("research", 0, 1 * K),
            SealWindow("holdout_hist", 1 * K, 3 * K),
            SealWindow("research", 3 * K, 10 * K),
            SealWindow("holdout_fwd", 10 * K, 20 * K),
        ],
    )
    return catalog


def test_research_sees_only_rows_inside_its_windows_and_the_validator_sees_all(windowed: Catalog) -> None:
    stamps = [K // 2, 3 * K // 2, 7 * K // 2, 15 * K]
    frame = pl.DataFrame({"event_ns": stamps, "received_ns": stamps, "value": [1.0, 2.0, 3.0, 4.0]})
    seen = PointInTime(windowed, role="research").rows_visible(frame, "trades")
    assert seen["value"].to_list() == [1.0, 3.0]  # neither the historical nor the forward holdout
    assert PointInTime(windowed, role="validator").rows_visible(frame, "trades").height == 4
    assert windowed.research_windows("trades") == [(0, 1 * K), (3 * K, 10 * K)]
    assert windowed.research_windows("oi") == [(-(2**63), 10_000 * S)]  # an epoch-1 dataset: as before


@pytest.mark.parametrize(
    ("first", "last", "ok"),
    [
        (0.1, 0.9, True),
        (3.1, 9.5, True),
        (0.9, 1.1, False),  # runs into the historical holdout
        (1.5, 1.6, False),  # inside it
        (2.5, 3.5, False),  # a warm-up that starts in the holdout
        (9.8, 10.2, False),  # into the forward holdout
        (0.1, 3.5, False),  # spans the holdout (two research blocks in one run)
    ],
)
def test_a_span_must_lie_inside_one_research_window(windowed: Catalog, first: float, last: float, ok: bool) -> None:
    pit = PointInTime(windowed, role="research")
    lo, hi = int(first * K), int(last * K)
    if ok:
        pit.check_span("trades", lo, hi)
    else:
        with pytest.raises(SealViolationError):
            pit.check_span("trades", lo, hi)
    PointInTime(windowed, role="validator").check_span("trades", lo, hi)  # never refused


def test_join_asof_refuses_decisions_spanning_the_historical_holdout(windowed: Catalog) -> None:
    pit = PointInTime(windowed, role="research")
    frame = pl.DataFrame({"event_ns": [0], "received_ns": [0], "value": [1.0]})
    with pytest.raises(SealViolationError, match="research window"):
        pit.join_asof(pl.Series("decision_ns", [K // 10, 7 * K // 2]), frame, "trades")
    assert pit.join_asof(pl.Series("decision_ns", [K // 10, K // 5]), frame, "trades").height == 2


def test_seal_windows_are_validated_and_immutable(catalog: Catalog) -> None:
    e2 = SealEpoch("oi", 2, research_until_ns=10 * K, holdout_until_ns=20 * K, rationale="x")
    with pytest.raises(CatalogError, match="overlap"):
        catalog.add_seal_windows(e2, [SealWindow("research", 0, 2 * K), SealWindow("holdout_hist", 1 * K, 3 * K)])
    with pytest.raises(CatalogError, match="research_until"):
        catalog.add_seal_windows(e2, [SealWindow("research", 0, 15 * K), SealWindow("holdout_fwd", 15 * K, 20 * K)])
    with pytest.raises(CatalogError, match="holdout_until"):
        catalog.add_seal_windows(e2, [SealWindow("research", 0, 10 * K), SealWindow("holdout_fwd", 10 * K, 30 * K)])
    assert catalog.seal("oi").epoch == 1  # refused windows registered nothing
    catalog.add_seal_windows(e2, [SealWindow("research", 0, 10 * K), SealWindow("holdout_fwd", 10 * K, 20 * K)])
    assert catalog.seal("oi").epoch == 2
    with pytest.raises(sqlite3.IntegrityError):
        catalog.conn.execute("update seal_window set to_ns = 1 where dataset_id = 'oi'")
    with pytest.raises(sqlite3.IntegrityError):
        catalog.conn.execute("delete from seal_window where dataset_id = 'oi'")


def test_a_row_whose_event_lies_in_the_holdout_stays_hidden_even_once_available_in_research(
    windowed: Catalog,
) -> None:
    """B1/B2 review finding 1: filtering on available_ns alone showed a bar whose EVENT lies in the historical
    holdout once its availability crossed into the next research window (an hourly bar stamped 2:59 K becomes
    available at 3.05 K). Both the event and the availability must lie inside the same research window."""
    frame = pl.DataFrame(
        {
            "event_ns": [29 * K // 10, 31 * K // 10, 9 * K // 10],
            "received_ns": [305 * K // 100, 32 * K // 10, 11 * K // 10],
            "value": [1.0, 2.0, 3.0],
        }
    )
    seen = PointInTime(windowed, role="research").rows_visible(frame, "trades")
    assert seen["value"].to_list() == [2.0]  # event 2.9 K in the holdout: hidden; event 0.9 K, available 1.1 K: hidden


def test_a_holdout_event_received_early_by_a_lagging_clock_stays_hidden(windowed: Catalog) -> None:
    """Second B2 review: a receive clock behind the exchange gives received < event, so a holdout event could be
    'available' just before a research window ends; the event must also lie before the window's end."""
    frame = pl.DataFrame(
        {
            "event_ns": [1 * K + 5, 10 * K + 5, K // 2],
            "received_ns": [1 * K - 5, 10 * K - 5, K // 2 + 5],
            "value": [1.0, 2.0, 3.0],
        }
    )
    seen = PointInTime(windowed, role="research").rows_visible(frame, "trades")
    assert seen["value"].to_list() == [3.0]
