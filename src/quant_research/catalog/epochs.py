"""Seal epoch 2 (v2.2 B2, ADR-040): a 23-month historical holdout plus the forward holdout.

Research may read [2020-01-01, 2023-01-01) and [2024-12-01, 2026-07-01); [2023-01-01, 2024-12-01) is the
historical holdout (never read by research: the earliest research backtest window started 2025-01-01, its warm-up
2024-12-02); [2026-07-01, 2026-11-01) is the forward holdout (after the LLM knowledge cutoff, ADR-013) and grows
with later epochs. Only datasets still on their ADR-013 epoch 1 move; the T0 collection datasets (ADR-011) keep
their own epoch. Registration is one transaction per dataset and refuses nothing silently."""

from __future__ import annotations

from datetime import UTC, datetime

from quant_research.catalog.store import Catalog, CatalogError, SealEpoch, SealWindow


def _ns(year: int, month: int) -> int:
    return int(datetime(year, month, 1, tzinfo=UTC).timestamp()) * 10**9


RESEARCH_UNTIL_NS = _ns(2026, 7)
HOLDOUT_UNTIL_NS = _ns(2026, 11)
EPOCH2_WINDOWS: tuple[SealWindow, ...] = (
    SealWindow("research", _ns(2020, 1), _ns(2023, 1)),
    SealWindow("holdout_hist", _ns(2023, 1), _ns(2024, 12)),
    SealWindow("research", _ns(2024, 12), RESEARCH_UNTIL_NS),
    SealWindow("holdout_fwd", RESEARCH_UNTIL_NS, HOLDOUT_UNTIL_NS),
)
RATIONALE = (
    "ADR-040: research [2020-01, 2023-01) + [2024-12, 2026-07); historical holdout [2023-01, 2024-12) "
    "(unread by research); forward holdout [2026-07, 2026-11)"
)


def epoch2_candidates(catalog: Catalog) -> list[str]:
    """Datasets whose latest epoch is the ADR-013 epoch 1."""
    rows = catalog.conn.execute(
        "select dataset_id, max(epoch), rationale from seal_policy group by dataset_id order by dataset_id"
    ).fetchall()
    out: list[str] = []
    for ds, epoch, _r in rows:
        latest = catalog.seal(str(ds))
        if int(epoch) == 1 and latest.rationale.startswith("ADR-013"):
            out.append(str(ds))
    return out


def register_epoch2(catalog: Catalog, *, apply: bool, candidates: list[str] | None = None) -> list[str]:
    """The datasets moved to epoch 2 by this call (or that would be, with `apply=False`). A candidate that another
    process moved in the meantime (two tools running bootstrap at once) is skipped, not an error."""
    todo = epoch2_candidates(catalog) if candidates is None else candidates
    if not apply:
        return todo
    moved: list[str] = []
    for ds in todo:
        try:
            catalog.add_seal_windows(
                SealEpoch(ds, 2, RESEARCH_UNTIL_NS, HOLDOUT_UNTIL_NS, RATIONALE), list(EPOCH2_WINDOWS)
            )
        except CatalogError:
            if catalog.seal(ds).epoch >= 2:
                continue  # lost the race: the other process registered the same pre-registered windows
            raise
        moved.append(ds)
    return moved
