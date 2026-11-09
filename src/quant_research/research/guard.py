"""Resource and thermal gate of research processes (plan §8: one worker <= 2 GB; SSD thermal state ADR-012).

Shared by tools/run_backtest.py and tools/run_director.py so both pause (never fail) when the host is busy.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from pathlib import Path

from quant_research.adapters.jobhost import memory_sample
from quant_research.bulk.guard import GB, Limits, Sample, combine, resource_gate
from quant_research.capture.thermal import heavy_work_allowed, read_state


def research_gate(data_root: Path, *, max_process_mem: int = 2 * GB) -> Callable[[], tuple[bool, str]]:
    limits = Limits(max_process_mem=max_process_mem)

    def gate() -> tuple[bool, str]:
        mem = memory_sample()
        sample = Sample(shutil.disk_usage(data_root).free, 0, mem["available_ram"], mem["private"])
        return combine(
            heavy_work_allowed(read_state(data_root / "run" / "ssd_state.json"), now_ns=time.time_ns()),
            resource_gate(sample, limits),
        )

    return gate
