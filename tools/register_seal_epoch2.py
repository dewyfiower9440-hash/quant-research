"""v2.2 B2: register seal epoch 2 (ADR-040) in the operational catalog. One time; dry run unless --apply.

    uv run python tools/register_seal_epoch2.py            # shows what would move
    uv run python tools/register_seal_epoch2.py --apply    # registers it (immutable afterwards)

Must run BEFORE any research run reads 2020-2024 data. Prints the windows of every moved dataset."""

from __future__ import annotations

import argparse
import json


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    from quant_research.catalog.epochs import EPOCH2_WINDOWS, register_epoch2
    from quant_research.catalog.store import Catalog
    from quant_research.config import load_settings

    catalog = Catalog.open(load_settings().data_root / "meta" / "catalog.db")
    try:
        moved = register_epoch2(catalog, apply=a.apply)
        report = {
            "applied": a.apply,
            "datasets": moved,
            "windows": [{"role": w.role, "from_ns": w.from_ns, "to_ns": w.to_ns} for w in EPOCH2_WINDOWS],
            "now": {ds: {"epoch": catalog.seal(ds).epoch, "research": catalog.research_windows(ds)} for ds in moved},
        }
    finally:
        catalog.close()
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
