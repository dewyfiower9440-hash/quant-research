"""Research coverage of the campaign windows (v2.3 step 3, ADR-049): which datasets the research role may read in
each window, and a version that changes whenever files inside the window are added or regenerated.

A dataset counts only when its latest seal epoch has a research window containing the whole campaign window (an
unsealed dataset is never readable, B4) and at least one bulk file lies in a month of that window. The version is
"<file rows>:<latest fetch ns>" over those months, so a re-download or a new symbol reopens a data_missing cell."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from quant_research.catalog.store import Catalog, CatalogError


def _month(ns: int) -> str:
    return datetime.fromtimestamp(ns // 10**9, tz=UTC).strftime("%Y-%m")


def research_coverage(catalog: Catalog, windows: Mapping[str, tuple[int, int]]) -> dict[str, dict[str, str]]:
    """window name -> {dataset: version} for the [from, to) windows (month-aligned, exclusive end)."""
    out: dict[str, dict[str, str]] = {w: {} for w in windows}
    datasets = [str(r[0]) for r in catalog.conn.execute("select distinct dataset_id from bulk_file order by 1")]
    for ds in datasets:
        try:
            research = catalog.research_windows(ds)
        except CatalogError:
            continue  # no seal policy: research may not read it
        for name, (lo, hi) in windows.items():
            if not any(a <= lo and hi <= b for a, b in research):
                continue
            row = catalog.conn.execute(
                "select count(*), coalesce(max(fetched_ns), 0) from bulk_file "
                "where dataset_id = ? and month >= ? and month < ?",
                (ds, _month(lo), _month(hi)),
            ).fetchone()
            if int(row[0]) > 0:
                out[name][ds] = f"{int(row[0])}:{int(row[1])}"
    return out
