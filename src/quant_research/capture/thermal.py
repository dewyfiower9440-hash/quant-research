"""SSD thermal gate (2026-11-09 decision 3).

An elevated, read-only scheduled task (`tools/ssd_temp_monitor.ps1`, every 10 minutes) writes the
SSD temperature to `<data_root>/health/ssd_temp.jsonl` and the current level to
`<data_root>/run/ssd_state.json`. The collectors run unprivileged and only read that file.

Levels (initial priors, to be re-tuned from the recorded series; the drive reports TemperatureMax 100):
    warn      >= 70 C
    critical  >= 75 C   -> heavy jobs (compression, backup, 30-day metrics refetch, bulk downloads) pause
    back to ok below 68 C (hysteresis)
A missing or stale (> 30 min) state never blocks work: a broken monitor must not stop backups; the
reason is reported instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

Level = Literal["ok", "warn", "critical"]
WARN_C = 70
CRITICAL_C = 75
RESUME_BELOW_C = 68
STALE_NS = 30 * 60 * 10**9


@dataclass(frozen=True, slots=True)
class ThermalState:
    at_ns: int
    temp_c: int
    level: Level


def next_level(previous: str, temp_c: int) -> Level:
    """Same rule as the PowerShell monitor (kept in sync by the tests of both)."""
    if temp_c >= CRITICAL_C:
        return "critical"
    if previous == "critical" and temp_c >= RESUME_BELOW_C:
        return "critical"
    if temp_c >= WARN_C:
        return "warn"
    if previous == "warn" and temp_c >= RESUME_BELOW_C:
        return "warn"
    return "ok"


def read_state(path: Path) -> ThermalState | None:
    if not path.exists():
        return None
    body = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8-sig")))
    level = body.get("level")
    if level not in ("ok", "warn", "critical"):
        raise ValueError(f"unknown thermal level {level!r} in {path}")
    return ThermalState(int(cast(int, body["at_ns"])), int(cast(int, body["temp_c"])), level)


def heavy_work_allowed(state: ThermalState | None, *, now_ns: int) -> tuple[bool, str]:
    if state is None:
        return True, "no SSD temperature state (monitor not installed or not run yet)"
    if now_ns - state.at_ns > STALE_NS:
        return True, f"SSD temperature state is stale ({(now_ns - state.at_ns) // 10**9} s old)"
    if state.level == "critical":
        return False, f"SSD at {state.temp_c} C (critical >= {CRITICAL_C} C, resume below {RESUME_BELOW_C} C)"
    return True, f"SSD at {state.temp_c} C ({state.level})"
