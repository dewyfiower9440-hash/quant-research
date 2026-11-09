"""Desktop notification for monitor incidents (plan step 10: alerts shown in the logon session).

The collector runs as an Interactive per-user task, so a hidden PowerShell started from it can show a
Windows toast. The text goes through a JSON file (no command-line quoting of free text); the launch uses
ShellExecuteW so no console window flashes. Off Windows this is a no-op (CI).
"""

from __future__ import annotations

import sys
from pathlib import Path

from quant_research.adapters.jobhost import write_json
from quant_research.monitor.checks import Finding

if sys.platform == "win32":
    import ctypes

    def _launch_hidden(exe: str, params: str) -> bool:
        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        rc = shell32.ShellExecuteW(None, "open", exe, params, None, 0)  # SW_HIDE
        return rc is not None and rc > 32  # ShellExecute: values <= 32 are errors

else:

    def _launch_hidden(exe: str, params: str) -> bool:
        return False


class ToastNotifier:
    def __init__(self, script: Path, alert_file: Path) -> None:
        self.script = script
        self.alert_file = alert_file
        self.launched = 0

    def __call__(self, findings: list[Finding]) -> None:
        worst = "critical" if any(f.severity == "critical" for f in findings) else "warn"
        write_json(
            self.alert_file,
            {
                "title": f"quant-research {worst}: {len(findings)} incident(s)",
                "lines": [f"[{f.severity}] {f.key}: {f.detail}" for f in findings][:5],
            },
        )
        params = f'-NoProfile -NonInteractive -WindowStyle Hidden -File "{self.script}" -AlertFile "{self.alert_file}"'
        if _launch_hidden("powershell.exe", params):
            self.launched += 1
