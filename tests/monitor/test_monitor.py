"""Plan step 10 monitor: pure checks, incident open/close in the catalog, notification of new incidents."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from quant_research.adapters.notify import ToastNotifier
from quant_research.capture.clockref import ClockReference
from quant_research.catalog.store import Catalog
from quant_research.monitor.checks import Finding, Observation, Thresholds, evaluate
from quant_research.monitor.incidents import open_incidents, reconcile
from quant_research.monitor.observe import monitor_once

S = 10**9
GB = 1024**3
NOW = 1_794_230_000 * S


HEALTHY = Observation(
    now_ns=NOW,
    heartbeats={"ws": NOW - 10 * S},
    free_disk=200 * GB,
    available_ram=4 * GB,
    clock_offset_ms=60.0,
    clock_measured_ns=NOW - 30 * S,
    ssd_state_ns=NOW - 60 * S,
    ssd_level="ok",
    receive_gaps=(),
)


def _obs(**changes: object) -> Observation:
    return replace(HEALTHY, **changes)


def keys(findings: list[Finding]) -> set[str]:
    return {f.key for f in findings}


def test_healthy_host_has_no_findings() -> None:
    assert evaluate(_obs(), Thresholds()) == []


@pytest.mark.parametrize(
    ("changes", "key", "severity"),
    [
        ({"heartbeats": {"ws": NOW - 121 * S}}, "heartbeat:ws", "critical"),
        ({"heartbeats": {"ws": None}}, "heartbeat:ws", "critical"),
        ({"free_disk": 49 * GB}, "disk", "warn"),
        ({"free_disk": 19 * GB}, "disk", "critical"),
        ({"available_ram": GB - 1}, "ram", "warn"),
        ({"clock_offset_ms": -251.0}, "clock", "warn"),
        ({"clock_measured_ns": NOW - 301 * S}, "clock", "warn"),
        ({"clock_offset_ms": None, "clock_measured_ns": None}, "clock", "warn"),
        ({"ssd_state_ns": None, "ssd_level": None}, "ssd_monitor", "warn"),
        ({"ssd_state_ns": NOW - 31 * 60 * S}, "ssd_monitor", "warn"),
        ({"ssd_level": "critical"}, "ssd_temp", "critical"),
        ({"ssd_level": "warn"}, "ssd_temp", "warn"),
    ],
)
def test_each_check_fires_past_its_threshold(changes: dict[str, object], key: str, severity: str) -> None:
    found = evaluate(_obs(**changes), Thresholds())
    assert [(f.key, f.severity) for f in found] == [(key, severity)]


def test_values_exactly_at_the_thresholds_do_not_fire() -> None:
    edge = _obs(
        heartbeats={"ws": NOW - 120 * S},
        free_disk=50 * GB,
        available_ram=GB,
        clock_offset_ms=250.0,
        clock_measured_ns=NOW - 300 * S,
        ssd_state_ns=NOW - 30 * 60 * S,
    )
    assert evaluate(edge, Thresholds()) == []


def test_only_receive_gaps_of_five_minutes_or_more_are_reported_each_under_its_own_key() -> None:
    gaps = [("upbit.orderbook_l1_1s", NOW - 600 * S, NOW - 300 * S), ("binance.um.book_ticker_1s", NOW - 90 * S, NOW)]
    found = evaluate(_obs(receive_gaps=gaps), Thresholds())
    assert keys(found) == {f"gap:upbit.orderbook_l1_1s:{NOW - 600 * S}"}
    assert "5.0 min" in found[0].detail


def test_reconcile_opens_once_keeps_open_and_closes_when_the_condition_clears(tmp_path: Path) -> None:
    catalog = Catalog.open(tmp_path / "c.db")
    disk = Finding("disk", "warn", "free 40 GB")
    try:
        assert reconcile(catalog, [disk], now_ns=1) == [disk]  # newly opened -> notify
        assert reconcile(catalog, [disk], now_ns=2) == []  # still open -> no second notification
        assert [i.key for i in open_incidents(catalog)] == ["disk"]
        assert reconcile(catalog, [], now_ns=3) == []
        assert open_incidents(catalog) == []
        row = catalog.conn.execute("select opened_ns, last_seen_ns, closed_ns from incident").fetchone()
        assert tuple(row) == (1, 2, 3)
        assert reconcile(catalog, [disk], now_ns=4) == [disk]  # a new episode is a new incident
        assert catalog.conn.execute("select count(*) from incident").fetchone()[0] == 2
    finally:
        catalog.close()


def test_reconcile_escalation_updates_severity_and_notifies_again(tmp_path: Path) -> None:
    catalog = Catalog.open(tmp_path / "c.db")
    try:
        reconcile(catalog, [Finding("disk", "warn", "40 GB")], now_ns=1)
        critical = Finding("disk", "critical", "15 GB")
        assert reconcile(catalog, [critical], now_ns=2) == [critical]
        assert [(i.key, i.severity) for i in open_incidents(catalog)] == [("disk", "critical")]
    finally:
        catalog.close()


def test_monitor_pass_records_logs_and_notifies_only_new_incidents(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "ws.heartbeat.json").write_text(json.dumps({"at_ns": NOW - 600 * S}), encoding="utf-8")
    catalog = Catalog.open(tmp_path / "meta" / "catalog.db")
    catalog.conn.execute(
        "insert into gap(source, start_ns, end_ns, cause, detail, recorded_ns) values (?,?,?,?,?,?)",
        ("upbit.orderbook_l1_1s", NOW - 3_600 * S, NOW - 600 * S, "unknown", "receive_gap", NOW - 5 * S),
    )
    catalog.close()
    clock = ClockReference()
    clock.update([(40.0, 20)], measured_ns=NOW - 10 * S)
    sent: list[list[Finding]] = []
    first = monitor_once(tmp_path, clock, sent.append, since_ns=NOW - 60 * S, now_ns=lambda: NOW)
    assert {"heartbeat:ws", "ssd_monitor", f"gap:upbit.orderbook_l1_1s:{NOW - 3_600 * S}"} <= keys(first)
    assert sent == [first]
    log_keys = {json.loads(line)["key"] for line in (run / "alerts.log").read_text(encoding="utf-8").splitlines()}
    assert log_keys == keys(first)
    again = monitor_once(tmp_path, clock, sent.append, since_ns=NOW, now_ns=lambda: NOW + 60 * S)
    assert again == [] and len(sent) == 1  # nothing new: no second toast


def test_monitor_treats_a_heartbeat_caught_mid_write_as_missing(tmp_path: Path) -> None:
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "ws.heartbeat.json").write_text('{"at_ns": 17', encoding="utf-8")
    Catalog.open(tmp_path / "meta" / "catalog.db").close()
    found = monitor_once(tmp_path, ClockReference(), lambda _f: None, since_ns=0, now_ns=lambda: NOW)
    assert next(f for f in found if f.key == "heartbeat:ws").detail == "ws heartbeat file missing"


def test_toast_notifier_writes_the_alert_file_for_the_script(tmp_path: Path) -> None:
    notifier = ToastNotifier(tmp_path / "missing-notify.ps1", tmp_path / "alerts.json")
    notifier([Finding("disk", "critical", "free 10 GB"), Finding("ram", "warn", "800 MB")])
    body = json.loads((tmp_path / "alerts.json").read_text(encoding="utf-8"))
    assert body["title"] == "quant-research critical: 2 incident(s)"
    assert body["lines"] == ["[critical] disk: free 10 GB", "[warn] ram: 800 MB"]
