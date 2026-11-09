"""Research resource / thermal gate (shared by the backtest and director runners)."""

import json
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from quant_research.bulk.guard import GB, MB
from quant_research.research import guard as guard_module
from quant_research.research.guard import research_gate


@dataclass(frozen=True)
class Disk:
    total: int
    used: int
    free: int


def _memory(available: int, private: int) -> dict[str, int]:
    return {"available_ram": available, "private": private, "working_set": private, "peak_working_set": private}


def test_gate_passes_on_a_healthy_host_and_blocks_on_low_ram_or_a_large_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = research_gate(tmp_path, max_process_mem=2 * GB)

    def disk(_path: Path) -> Disk:
        return Disk(900 * GB, 400 * GB, 500 * GB)

    monkeypatch.setattr(guard_module.shutil, "disk_usage", disk)
    monkeypatch.setattr(guard_module, "memory_sample", lambda: _memory(8 * GB, 500 * MB))
    assert gate()[0] is True
    monkeypatch.setattr(guard_module, "memory_sample", lambda: _memory(1 * GB, 500 * MB))
    assert gate()[0] is False
    monkeypatch.setattr(guard_module, "memory_sample", lambda: _memory(8 * GB, 3 * GB))
    blocked, why = gate()
    assert blocked is False and "process" in why


def test_gate_blocks_while_the_ssd_is_critical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "run").mkdir()
    state = {"at_ns": time.time_ns(), "temp_c": 80, "level": "critical"}
    (tmp_path / "run" / "ssd_state.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(guard_module, "memory_sample", lambda: _memory(8 * GB, 100 * MB))
    ok, why = research_gate(tmp_path)()
    assert ok is False and "80" in why
