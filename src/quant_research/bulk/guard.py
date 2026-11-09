"""Resource gate for the bulk download (plan step 8, 2026-11-09 decision on the resumed run).

The bulk run shares the PC with the T0 collectors. Before every file it samples free disk, available
physical RAM and its own memory, and stops (resumably) when any limit is crossed, so the collectors
are never starved. Limits are initial priors, re-tuned from the logged samples.
"""

from __future__ import annotations

from dataclasses import dataclass

GB = 1024**3
MB = 1024**2


@dataclass(frozen=True, slots=True)
class Limits:
    min_free_disk: int = 50 * GB  # stop: leave room for the collectors' raw segments and backups
    max_bulk_bytes: int = 200 * GB  # plan step 8 disk cap for the bulk lake
    warn_bulk_ratio: float = 0.8  # alert at 80 % of the cap
    min_available_ram: int = 1536 * MB  # stop before the OS starts reaping processes
    max_process_mem: int = 1024 * MB  # this process's private bytes


@dataclass(frozen=True, slots=True)
class Sample:
    free_disk: int
    bulk_bytes: int
    available_ram: int
    process_mem: int


def resource_gate(sample: Sample, limits: Limits) -> tuple[bool, str]:
    """(allowed, reason). Not allowed means: stop now, rerun later resumes from the catalog."""
    if sample.free_disk < limits.min_free_disk:
        return False, f"free disk {sample.free_disk // GB} GB < {limits.min_free_disk // GB} GB"
    if sample.bulk_bytes >= limits.max_bulk_bytes:
        return False, f"bulk lake {sample.bulk_bytes // GB} GB reached the {limits.max_bulk_bytes // GB} GB cap"
    if sample.available_ram < limits.min_available_ram:
        return False, f"available RAM {sample.available_ram // MB} MB < {limits.min_available_ram // MB} MB"
    if sample.process_mem > limits.max_process_mem:
        return False, f"process memory {sample.process_mem // MB} MB > {limits.max_process_mem // MB} MB"
    if sample.bulk_bytes >= limits.max_bulk_bytes * limits.warn_bulk_ratio:
        return True, f"WARN bulk lake {sample.bulk_bytes // GB} GB >= {limits.warn_bulk_ratio:.0%} of the cap"
    return True, "ok"


def combine(*gates: tuple[bool, str]) -> tuple[bool, str]:
    """First blocking gate wins; otherwise all reasons are kept for the log."""
    for allowed, why in gates:
        if not allowed:
            return False, why
    return True, "; ".join(why for _ok, why in gates)
