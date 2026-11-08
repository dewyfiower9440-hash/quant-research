"""Step 6: point-in-time access (design v2.1 §5.4). The leak tests inject future information on purpose."""

import ast
from collections.abc import Iterator
from pathlib import Path

import polars as pl
import pytest

from quant_research.catalog.store import Catalog, Dataset, LagPolicy, SealEpoch
from quant_research.data.access import PointInTime, SealViolationError

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
    pinned = PointInTime(catalog, lag_versions={"oi": 1}).join_asof(decisions, frame, "oi")
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
