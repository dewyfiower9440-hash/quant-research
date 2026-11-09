"""Plan step 4 restore drill: restore the raw backup and every catalog snapshot, verify, report.

    uv run python tools/restore_drill.py --to C:/dev/qr-restore-drill [--snapshot] [--keep]

--snapshot takes a fresh catalog snapshot first (the rest worker's hourly backup job does this too).
Raw files must match sha256 and line count; catalog snapshots must match sha256, pass
`pragma integrity_check` and reproduce every table's row count. The restore folder is deleted
afterwards unless --keep. Exit 0 = everything matched, 1 = at least one mismatch.
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

from quant_research.capture.backup import restore_and_verify, restore_catalog_and_verify, snapshot_catalog
from quant_research.config import load_settings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", type=Path, required=True, help="empty folder to restore into")
    ap.add_argument("--snapshot", action="store_true")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    settings = load_settings()
    if a.to.exists() and any(a.to.iterdir()):
        print(f"refusing: {a.to} is not empty")  # CLI output
        return 1
    if a.snapshot:
        snap = snapshot_catalog(settings.data_root, settings.backup_root, now_ns=time.time_ns())
        print(f"snapshot: {None if snap is None else snap.path} rows={None if snap is None else snap.tables}")
    t0 = time.monotonic()
    raw = restore_and_verify(settings.backup_root, a.to / "raw-restore")
    t1 = time.monotonic()
    cat = restore_catalog_and_verify(settings.backup_root, a.to / "catalog-restore")
    t2 = time.monotonic()
    print(
        json.dumps(
            {
                "raw": {"files": raw.files, "mismatches": raw.mismatches, "seconds": round(t1 - t0, 1)},
                "catalog": {"snapshots": cat.files, "mismatches": cat.mismatches, "seconds": round(t2 - t1, 1)},
            }
        )
    )
    if not a.keep:
        shutil.rmtree(a.to)
    return 0 if raw.ok and cat.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
