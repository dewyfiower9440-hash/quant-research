"""Host and collector health checks (plan step 10). Pure: the rest worker gathers an Observation, this
module decides which checks fail. Thresholds are initial priors (CLAUDE.md §9: re-tune from the
recorded incidents)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

Severity = Literal["warn", "critical"]
S = 10**9
GB = 1024**3


@dataclass(frozen=True, slots=True)
class Finding:
    key: str  # stable identity of the failing condition (one open incident per key)
    severity: Severity
    detail: str


@dataclass(frozen=True, slots=True)
class Observation:
    now_ns: int
    heartbeats: Mapping[str, int | None]  # other collector processes: name -> last at_ns (None: missing)
    free_disk: int
    available_ram: int | None  # None where it cannot be measured (non-Windows)
    clock_offset_ms: float | None
    clock_measured_ns: int | None
    ssd_state_ns: int | None  # at_ns of run/ssd_state.json (None: SSD monitor never ran)
    ssd_level: str | None
    receive_gaps: Sequence[tuple[str, int, int]]  # (source, start_ns, end_ns) recorded since last check
    # consecutive short runs per supervised child (run/supervisor.state.json; empty: no supervisor state yet)
    restart_streaks: Mapping[str, int] = field(default_factory=lambda: dict[str, int]())


@dataclass(frozen=True, slots=True)
class Thresholds:
    heartbeat_stale_ns: int = 120 * S  # writers beat every 10 s
    disk_warn: int = 50 * GB  # same floor as the bulk gate
    disk_critical: int = 20 * GB
    ram_warn: int = GB
    clock_abs_ms: float = 250.0  # ClockReference suspect threshold (§5.4)
    clock_stale_ns: int = 300 * S
    ssd_stale_ns: int = 30 * 60 * S  # thermal.STALE_NS
    gap_report_ns: int = 300 * S
    restart_loop: int = 10  # OPS-009: consecutive short runs of one child that make a critical incident


def evaluate(o: Observation, t: Thresholds) -> list[Finding]:
    out: list[Finding] = []
    for name, at in sorted(o.heartbeats.items()):
        if at is None:
            out.append(Finding(f"heartbeat:{name}", "critical", f"{name} heartbeat file missing"))
        elif o.now_ns - at > t.heartbeat_stale_ns:
            out.append(Finding(f"heartbeat:{name}", "critical", f"{name} heartbeat {(o.now_ns - at) // S} s old"))
    if o.free_disk < t.disk_warn:
        severity: Severity = "critical" if o.free_disk < t.disk_critical else "warn"
        out.append(Finding("disk", severity, f"free disk {o.free_disk / GB:.1f} GB"))
    if o.available_ram is not None and o.available_ram < t.ram_warn:
        out.append(Finding("ram", "warn", f"available RAM {o.available_ram // 2**20} MB"))
    if o.clock_offset_ms is None or o.clock_measured_ns is None:
        out.append(Finding("clock", "warn", "exchange clock never measured"))
    elif o.now_ns - o.clock_measured_ns > t.clock_stale_ns:
        out.append(Finding("clock", "warn", f"clock measurement {(o.now_ns - o.clock_measured_ns) // S} s old"))
    elif abs(o.clock_offset_ms) > t.clock_abs_ms:
        out.append(Finding("clock", "warn", f"clock offset {o.clock_offset_ms:.0f} ms"))
    if o.ssd_state_ns is None:
        out.append(Finding("ssd_monitor", "warn", "no SSD temperature state (monitor task not installed)"))
    elif o.now_ns - o.ssd_state_ns > t.ssd_stale_ns:
        out.append(Finding("ssd_monitor", "warn", f"SSD state {(o.now_ns - o.ssd_state_ns) // S} s old"))
    elif o.ssd_level in ("warn", "critical"):
        level: Severity = "critical" if o.ssd_level == "critical" else "warn"
        out.append(Finding("ssd_temp", level, f"SSD temperature level {o.ssd_level}"))
    for name, streak in sorted(o.restart_streaks.items()):
        if streak >= t.restart_loop:
            out.append(Finding(f"restart_loop:{name}", "critical", f"{name} restarted {streak} times in a row"))
    for source, start, end in o.receive_gaps:
        if end - start >= t.gap_report_ns:
            out.append(Finding(f"gap:{source}:{start}", "warn", f"{source} silent {(end - start) / 60 / S:.1f} min"))
    return out
