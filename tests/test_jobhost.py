"""Shared job-CLI helpers: status files are atomic, logs are timestamped, memory sampling works on Windows."""

import io
import json
import sys
from pathlib import Path

import pytest

from quant_research.adapters.jobhost import Log, memory_sample, write_json


def test_write_json_replaces_the_file_atomically(tmp_path: Path) -> None:
    target = tmp_path / "progress.json"
    write_json(target, {"state": "running"})
    write_json(target, {"state": "done", "n": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"state": "done", "n": 2}
    assert [p.name for p in tmp_path.iterdir()] == ["progress.json"]  # no temporary left behind


def test_log_appends_timestamped_lines(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "logs" / "job.log"
    log = Log(path)
    log("START a")
    log("END b")
    log.close()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [line.split(" ", 1)[1] for line in lines] == ["START a", "END b"]
    assert lines[0][:4].isdigit() and "+00:00" in lines[0]
    assert "START a" in capsys.readouterr().out


def test_log_escapes_characters_the_console_cannot_encode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp949", errors="strict")  # the Korean Windows console
    monkeypatch.setattr(sys, "stdout", console)
    path = tmp_path / "job.log"
    log = Log(path)
    log("[fundingRate 629/632] 币安人生USDT months=10")  # a listed symbol with CJK characters
    log.close()
    console.flush()
    assert "币安人生USDT" in path.read_text(encoding="utf-8")  # the file keeps the real name
    assert "\\u5e01" in raw.getvalue().decode("cp949")  # the console gets an escape, not a crash


def test_memory_sample_reports_this_process_or_fails_loudly_off_windows() -> None:
    if sys.platform == "win32":
        mem = memory_sample()
        assert mem["available_ram"] > 0 and mem["private"] > 0
        assert mem["peak_working_set"] >= mem["working_set"] > 0
    else:
        with pytest.raises(OSError, match="Windows only"):
            memory_sample()
