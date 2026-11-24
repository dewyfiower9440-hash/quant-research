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


H = 3_600 * S


def test_an_open_critical_incident_is_notified_again_every_six_hours(tmp_path: Path) -> None:
    """v2.2 A10 / OPS-009: the ws crash loop of F-017 opened one critical incident, toasted once and stayed silent
    for 7 days. Open critical incidents are re-notified every `realert_ns`; warnings are not."""
    catalog = Catalog.open(tmp_path / "c.db")
    hb = Finding("heartbeat:ws", "critical", "ws heartbeat 600 s old")
    disk = Finding("disk", "warn", "free 40 GB")
    try:
        assert reconcile(catalog, [hb, disk], now_ns=NOW) == [hb, disk]
        assert reconcile(catalog, [hb, disk], now_ns=NOW + H) == []
        assert reconcile(catalog, [hb, disk], now_ns=NOW + 6 * H) == [hb]  # due again; the warning is not
        assert reconcile(catalog, [hb, disk], now_ns=NOW + 7 * H) == []  # the clock restarts at the re-alert
        assert reconcile(catalog, [hb, disk], now_ns=NOW + 12 * H) == [hb]
        assert reconcile(catalog, [hb], now_ns=NOW + 12 * H + 1, realert_ns=H) == []  # re-notified just now
    finally:
        catalog.close()


def test_a_child_restart_loop_is_a_critical_finding(tmp_path: Path) -> None:
    """A10 / OPS-009: the supervisor restarted ws every 5 minutes for 7 days; 10 consecutive short runs open an
    incident of their own (the supervisor writes the streaks, the monitor reads them)."""
    assert keys(evaluate(_obs(restart_streaks={"ws": 9}), Thresholds())) == set()
    found = evaluate(_obs(restart_streaks={"ws": 10, "rest": 2}), Thresholds())
    assert keys(found) == {"restart_loop:ws"} and found[0].severity == "critical"
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "ws.heartbeat.json").write_text(json.dumps({"at_ns": NOW - 5 * S}), encoding="utf-8")
    current = {"streaks": {"ws": 12}, "at_ns": NOW - 30 * S}  # written by a running supervisor half a minute ago
    (tmp_path / "run" / "supervisor.state.json").write_text(json.dumps(current), encoding="utf-8")
    Catalog.open(tmp_path / "meta" / "catalog.db").close()
    out = monitor_once(tmp_path, ClockReference(), lambda _f: None, since_ns=0, now_ns=lambda: NOW)
    assert "restart_loop:ws" in keys(out)


def test_restart_streaks_count_consecutive_short_runs_and_reset_on_a_long_one() -> None:
    from quant_research.capture.run import update_streak

    streaks: dict[str, int] = {}
    for _ in range(3):
        update_streak(streaks, "ws", ran_s=5.0)
    assert streaks == {"ws": 3}
    update_streak(streaks, "ws", ran_s=3_600.0)  # it ran an hour: the loop is over
    assert streaks == {"ws": 0}


def test_the_paper_heartbeat_is_watched_once_the_task_is_registered(tmp_path: Path) -> None:
    """A10: the paper heartbeat was watched only after it had been written once, so a registered paper task that
    never started went unnoticed; the task script writes run/paper.expected."""
    run = tmp_path / "run"
    run.mkdir()
    (run / "ws.heartbeat.json").write_text(json.dumps({"at_ns": NOW - 5 * S}), encoding="utf-8")
    Catalog.open(tmp_path / "meta" / "catalog.db").close()
    quiet = monitor_once(tmp_path, ClockReference(), lambda _f: None, since_ns=0, now_ns=lambda: NOW)
    assert "heartbeat:paper" not in keys(quiet)  # not registered: not watched
    (run / "paper.expected").write_text("registered", encoding="utf-8")
    loud = monitor_once(tmp_path, ClockReference(), lambda _f: None, since_ns=0, now_ns=lambda: NOW + S)
    assert "heartbeat:paper" in keys(loud)


def test_a_recovered_child_resets_its_streak_and_a_stale_supervisor_state_is_ignored(tmp_path: Path) -> None:
    """A6-A12 review finding 3: the streak was updated only when a child exited, so a child that recovered after
    10 short runs kept 'restart_loop' open (and re-alerted) until its next exit."""
    from quant_research.capture.run import reset_recovered

    streaks = {"ws": 10, "rest": 3}
    changed = reset_recovered(streaks, running_for_s={"ws": 300.0, "rest": 20.0})
    assert changed and streaks == {"ws": 0, "rest": 3}
    assert not reset_recovered(streaks, running_for_s={"ws": 300.0})
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "ws.heartbeat.json").write_text(json.dumps({"at_ns": NOW - 5 * S}), encoding="utf-8")
    state = {"streaks": {"ws": 12}, "at_ns": NOW - 3_600 * S}  # an hour old: the supervisor is not running
    (tmp_path / "run" / "supervisor.state.json").write_text(json.dumps(state), encoding="utf-8")
    Catalog.open(tmp_path / "meta" / "catalog.db").close()
    out = monitor_once(tmp_path, ClockReference(), lambda _f: None, since_ns=0, now_ns=lambda: NOW)
    assert "restart_loop:ws" not in keys(out)


def test_writing_the_supervisor_state_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A6-A12 review finding 4: os.replace raises PermissionError on Windows while the monitor reads the file; the
    exception left the supervise loop and stopped every collector process."""
    import os

    from quant_research.capture.run import write_supervisor_state

    target = tmp_path / "run" / "supervisor.state.json"

    def busy(src: object, dst: object) -> None:
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(os, "replace", busy)
    write_supervisor_state(target, {"ws": 2})  # falls back to an in-place write
    assert json.loads(target.read_text(encoding="utf-8"))["streaks"] == {"ws": 2}


def test_a_one_hertz_job_without_a_success_for_five_minutes_is_critical() -> None:
    """v2.5 N01: the process beat its heartbeat for 53 hours while every request failed."""
    stale = evaluate(_obs(job_ok_ns={"book_ticker": NOW - 301 * S, "upbit_orderbook": NOW - 10 * S}), Thresholds())
    assert [(f.key, f.severity) for f in stale] == [("stale_job:book_ticker", "critical")]
    assert "301 s" in stale[0].detail
    assert evaluate(_obs(job_ok_ns={"book_ticker": NOW - 300 * S}), Thresholds()) == []  # the boundary itself
    both = evaluate(_obs(job_ok_ns={"book_ticker": NOW - 900 * S, "upbit_orderbook": NOW - 900 * S}), Thresholds())
    assert keys(both) == {"stale_job:book_ticker", "stale_job:upbit_orderbook"}


def test_the_monitor_reads_the_last_success_of_the_watched_jobs_from_the_rest_heartbeat(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "ws.heartbeat.json").write_text(json.dumps({"at_ns": NOW - 5 * S}), encoding="utf-8")
    beat = {
        "at_ns": NOW - 5 * S,
        "started_ns": NOW - 7_200 * S,
        "last_ok_ns": {"book_ticker": NOW - 2 * S, "clock": NOW - 4_000 * S},  # clock is not a watched job
        "fail_streak": {"upbit_orderbook": 7_100, "clock": 60},
    }
    (run / "rest.heartbeat.json").write_text(json.dumps(beat), encoding="utf-8")
    Catalog.open(tmp_path / "meta" / "catalog.db").close()
    clock = ClockReference()
    clock.update([(40.0, 20)], measured_ns=NOW - 10 * S)
    found = monitor_once(tmp_path, clock, lambda _f: None, since_ns=NOW - 60 * S, now_ns=lambda: NOW)
    # upbit_orderbook never succeeded since the start two hours ago: counted from the start
    assert [f.detail for f in found if f.key.startswith("stale_job:")] == ["upbit_orderbook: no success for 7200 s"]


def test_a_rest_heartbeat_without_the_new_field_reports_no_stale_job(tmp_path: Path) -> None:
    """A heartbeat written by the previous version (no last_ok_ns at all), from a process started two hours ago,
    says nothing about its jobs: no alarm (review: the first version of this test passed only because the process
    had just started)."""
    found = _stale_findings(tmp_path, {"at_ns": NOW - 5 * S, "started_ns": NOW - 7_200 * S})
    assert found == []


def _stale_findings(tmp_path: Path, rest_beat: dict[str, object], stale_state: str | None = None) -> list[Finding]:
    run = tmp_path / "run"
    run.mkdir()
    (run / "ws.heartbeat.json").write_text(json.dumps({"at_ns": NOW - 5 * S}), encoding="utf-8")
    (run / "rest.heartbeat.json").write_text(json.dumps(rest_beat), encoding="utf-8")
    if stale_state is not None:
        (run / "rest.stale.json").write_text(stale_state, encoding="utf-8")
    Catalog.open(tmp_path / "meta" / "catalog.db").close()
    clock = ClockReference()
    clock.update([(40.0, 20)], measured_ns=NOW - 10 * S)
    found = monitor_once(tmp_path, clock, lambda _f: None, since_ns=NOW - 60 * S, now_ns=lambda: NOW)
    return [f for f in found if f.key.startswith("stale_")]


def test_a_heartbeat_that_is_itself_behind_reports_no_stale_job(tmp_path: Path) -> None:
    """Resume from standby: the monitor runs before the heartbeat is rewritten. The last success it reads is the
    one from before the standby, not evidence of five minutes of failing."""
    beat: dict[str, object] = {
        "at_ns": NOW - 3_600 * S,
        "started_ns": NOW - 9_000 * S,
        "last_ok_ns": {"book_ticker": NOW - 3_601 * S, "upbit_orderbook": NOW - 3_601 * S},
    }
    assert _stale_findings(tmp_path, beat) == []


def test_an_old_last_success_without_a_run_of_failures_is_not_a_stale_job(tmp_path: Path) -> None:
    """Re-review: after a standby the heartbeat job may run before the monitor, so the heartbeat is current while
    the last success is from before the standby. Only a run of failures makes a job stale, as in the collector."""
    beat: dict[str, object] = {
        "at_ns": NOW - 1 * S,
        "started_ns": NOW - 9_000 * S,
        "last_ok_ns": {"book_ticker": NOW - 3_600 * S, "upbit_orderbook": NOW - 3_600 * S},
        "fail_streak": {"book_ticker": 1},
    }
    assert _stale_findings(tmp_path, beat) == []


def test_a_stale_restart_is_reported_even_though_the_new_process_looks_healthy(tmp_path: Path) -> None:
    """Review: the process exits at the same age the check fires, and the new one starts counting again. The
    restart itself has to be the incident."""
    healthy: dict[str, object] = {
        "at_ns": NOW - 5 * S,
        "started_ns": NOW - 60 * S,
        "last_ok_ns": {"book_ticker": NOW - 1 * S, "upbit_orderbook": NOW - 1 * S},
    }
    state = json.dumps({"upbit_orderbook": NOW - 90 * S, "book_ticker": NOW - 7 * 3_600 * S})
    found = _stale_findings(tmp_path, healthy, state)
    assert [(f.key, f.severity) for f in found] == [(f"stale_restart:upbit_orderbook:{NOW - 90 * S}", "critical")]
    assert "restarted" in found[0].detail


def test_an_unreadable_stale_restart_state_reports_nothing(tmp_path: Path) -> None:
    healthy: dict[str, object] = {"at_ns": NOW - 5 * S, "started_ns": NOW - 60 * S, "last_ok_ns": {}}
    assert _stale_findings(tmp_path, healthy, "[1, 2") == []
