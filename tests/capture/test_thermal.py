"""SSD thermal gate (2026-11-09 decision 3): heavy jobs pause while the SSD is critical."""

import json
from pathlib import Path

import pytest

from quant_research.capture.thermal import ThermalState, heavy_work_allowed, next_level, read_state

NS = 10**9


@pytest.mark.parametrize(
    ("previous", "temp", "expected"),
    [
        ("ok", 60, "ok"),
        ("ok", 70, "warn"),
        ("ok", 75, "critical"),
        ("warn", 74, "warn"),
        ("critical", 70, "critical"),  # hysteresis: stays critical until below 68
        ("critical", 67, "ok"),
        ("warn", 67, "ok"),
        ("critical", 69, "critical"),
    ],
)
def test_next_level_uses_thresholds_with_hysteresis(previous: str, temp: int, expected: str) -> None:
    assert next_level(previous, temp) == expected


def _write(path: Path, level: str, at_ns: int, temp: int = 60) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at_ns": at_ns, "temp_c": temp, "level": level}), encoding="utf-8")


def test_heavy_work_is_paused_only_by_a_fresh_critical_state(tmp_path: Path) -> None:
    p = tmp_path / "run" / "ssd_state.json"
    _write(p, "critical", at_ns=100 * NS, temp=78)
    allowed, why = heavy_work_allowed(read_state(p), now_ns=100 * NS + 60 * NS)
    assert not allowed and "78" in why
    _write(p, "warn", at_ns=100 * NS, temp=72)
    assert heavy_work_allowed(read_state(p), now_ns=101 * NS)[0]


def test_stale_or_missing_monitor_does_not_block_but_says_why(tmp_path: Path) -> None:
    p = tmp_path / "run" / "ssd_state.json"
    allowed, why = heavy_work_allowed(read_state(p), now_ns=1)
    assert allowed and "no SSD temperature" in why
    _write(p, "critical", at_ns=0, temp=80)
    allowed, why = heavy_work_allowed(read_state(p), now_ns=31 * 60 * NS)
    assert allowed and "stale" in why


def test_read_state_rejects_unknown_levels(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    _write(p, "melting", at_ns=1)
    with pytest.raises(ValueError, match="level"):
        read_state(p)
    assert read_state(tmp_path / "missing.json") is None
    _write(p, "ok", at_ns=5, temp=61)
    assert read_state(p) == ThermalState(at_ns=5, temp_c=61, level="ok")


def test_thermal_gated_job_skips_and_records_while_critical_and_runs_otherwise(tmp_path: Path) -> None:
    import asyncio
    import time

    from quant_research.capture.clockref import ClockReference
    from quant_research.capture.run import thermal_gated
    from quant_research.capture.sinks import Sinks
    from quant_research.config import Settings

    settings = Settings(data_root=tmp_path)
    sinks = Sinks(tmp_path / "raw", "v", "sha", ClockReference(), fsync=lambda _fd: None)
    ran: list[str] = []

    async def job() -> None:
        ran.append("ran")

    async def run_gated() -> None:
        await thermal_gated(settings, sinks, "compress", job)()

    state = tmp_path / "run" / "ssd_state.json"
    _write(state, "critical", at_ns=time.time_ns(), temp=79)
    asyncio.run(run_gated())
    assert ran == []
    _write(state, "ok", at_ns=time.time_ns(), temp=60)
    asyncio.run(run_gated())
    assert ran == ["ran"]
    sinks.close()
    incidents = [
        json.loads(line)
        for f in (tmp_path / "raw" / "collector.incidents.rest").glob("*/*.jsonl")
        for line in f.read_text(encoding="utf-8").splitlines()[1:]
    ]
    assert incidents[0]["payload"]["job"] == "compress" and "79" in incidents[0]["payload"]["thermal_pause"]
