"""Phase-1 completion 4b: fill `instrument` and `universe_membership` from data already captured.

    uv run python tools/load_universe_catalog.py --universe C:/dev/qr-data/bulk/universe-20261109.json

Reads the newest Binance UM exchangeInfo and Upbit market snapshots, every collector.universe record and
the bulk universe report. Idempotent: rerun after new snapshots or a new universe report.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from quant_research.catalog.store import Catalog
from quant_research.catalog.universe_load import load_universe
from quant_research.config import load_settings
from quant_research.data.lake import SegmentStream


def records(raw: Path, source: str) -> list[tuple[int, object]]:
    out: list[tuple[int, object]] = []
    for path in sorted((raw / source).glob("*/*.jsonl*")):
        if path.name.endswith(".tmp"):
            continue
        out += [(rec.recv_ns, rec.payload) for rec in SegmentStream(path)]
    return sorted(out, key=lambda r: r[0])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", type=Path, required=True)
    a = ap.parse_args()
    root = load_settings().data_root
    raw = root / "raw"
    info = cast(Mapping[str, object], records(raw, "binance.um.exchange_info")[-1][1])
    markets = cast(Sequence[Mapping[str, object]], records(raw, "upbit.markets")[-1][1])
    universes = [(at, cast(Mapping[str, Sequence[str]], body)) for at, body in records(raw, "collector.universe")]
    report = cast(dict[str, object], json.loads(a.universe.read_text(encoding="utf-8")))
    spans = cast(Mapping[str, Sequence[Sequence[str | None]]], report["spans"])
    catalog = Catalog.open(root / "meta" / "catalog.db")
    try:
        done = load_universe(catalog, cast(Sequence[Mapping[str, object]], info["symbols"]), spans, markets, universes)
        totals = {
            "instruments": catalog.conn.execute("select venue, count(*) from instrument group by venue").fetchall(),
            "memberships": catalog.conn.execute(
                "select rule_version, count(*) from universe_membership group by rule_version"
            ).fetchall(),
        }
    finally:
        catalog.close()
    print(json.dumps({"added": {"instruments": done.instruments, "memberships": done.memberships}, **totals}))
    print(f"collector universe records: {len(universes)}")  # CLI output
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
