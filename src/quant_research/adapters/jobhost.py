"""Host helpers for long-running job CLIs (bulk download, REST backfills): memory sampling via the
Win32 API, a timestamped log that works under pythonw (no console), atomic JSON status files.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _MemoryStatusEx(ctypes.Structure):
        _fields_ = (
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        )

    class _ProcessMemoryCountersEx(ctypes.Structure):
        _fields_ = (
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        )

    def memory_sample() -> dict[str, int]:
        """Available physical RAM and this process's private / working-set bytes (Win32 API)."""
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError(ctypes.get_last_error(), "GlobalMemoryStatusEx failed")
        counters = _ProcessMemoryCountersEx()
        counters.cb = ctypes.sizeof(_ProcessMemoryCountersEx)
        # GetCurrentProcess returns the pseudo-handle -1 as a 64-bit HANDLE; without explicit
        # restype/argtypes ctypes truncates it to a 32-bit int and the call fails.
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.K32GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD)
        kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
        if not kernel32.K32GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise OSError(ctypes.get_last_error(), "K32GetProcessMemoryInfo failed")
        return {
            "available_ram": int(status.ullAvailPhys),
            "private": int(counters.PrivateUsage),
            "working_set": int(counters.WorkingSetSize),
            "peak_working_set": int(counters.PeakWorkingSetSize),
        }

else:

    def memory_sample() -> dict[str, int]:
        raise OSError("memory sampling is implemented for Windows only")


class Log:
    def __init__(self, path: Path | None) -> None:
        self._file: TextIO | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = path.open("a", encoding="utf-8")

    def __call__(self, line: str) -> None:
        stamped = f"{datetime.now(UTC).isoformat(timespec='seconds')} {line}"
        if self._file is not None:
            self._file.write(stamped + "\n")
            self._file.flush()
        if sys.stdout is not None:  # pythonw (Task Scheduler) has no console
            # the console (cp949 on this machine) cannot show every symbol name; the file above keeps the real text
            encoding = sys.stdout.encoding or "utf-8"
            print(stamped.encode(encoding, "backslashreplace").decode(encoding), flush=True)

    def close(self) -> None:
        if self._file is not None:
            self._file.close()


def write_json(path: Path, body: Mapping[str, object]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(body, indent=1), encoding="utf-8")
    tmp.replace(path)
