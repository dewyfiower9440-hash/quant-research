"""N_eff over the recorded S2+ trials (I/O shell around `search.multiplicity`, Phase 4 step 7).

Daily pnl vectors (`backtest_run.pnl_daily`, zlib float64) of done S2/S3 trials are aligned by UTC day,
grouped by evaluation window (same period = same multiple-testing family), clustered with ONC and reported
with N_total = every S1 + S2 + S3 trial recorded for the group (S1 selection is multiplicity too, design §8)."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from quant_research.backtest.run import unblob
from quant_research.research.ledger import TrialLedger
from quant_research.search.multiplicity import F64, average_offdiag, onc_clusters, pairwise_corr


@dataclass(frozen=True, slots=True)
class NeffGroup:
    window_from_ns: int
    window_to_ns: int
    trial_ids: tuple[int, ...]
    n_s2plus: int
    n_eff: int
    n_s1: int
    avg_rho: float
    quality: float

    @property
    def n_total(self) -> int:
        return self.n_s2plus + self.n_s1


def load_pnl_matrix(
    ledger: TrialLedger, *, window_from_ns: int, window_to_ns: int, fold_prefix: str | None = None
) -> tuple[list[int], F64]:
    """(trial ids, days x trials matrix) of done S2/S3 trials of one window; shorter vectors are NaN-padded."""
    rows = ledger.store.conn.execute(
        "select b.trial_id, b.pnl_daily from backtest_run b join trial t on t.trial_id = b.trial_id "
        "where t.status = 'done' and t.stage in ('S2', 'S3') and b.window_from_ns = ? and b.window_to_ns = ? "
        "and (? is null or t.fold like ? || '%') order by b.trial_id",
        (window_from_ns, window_to_ns, fold_prefix, fold_prefix),
    ).fetchall()
    ids = [int(r[0]) for r in rows]
    series = [unblob(bytes(r[1])) for r in rows]
    if not series:
        return ids, np.zeros((0, 0))
    days = max(int(s.shape[0]) for s in series)
    mat = np.full((days, len(series)), np.nan)
    for j, s in enumerate(series):
        mat[: s.shape[0], j] = s
    return ids, mat


def neff_groups(ledger: TrialLedger, *, seed: int = 0, fold_prefix: str | None = None) -> list[NeffGroup]:
    """Per evaluation window; `fold_prefix` restricts to one run's trials (A11: re-runs of the same batch add to
    the cumulative N_total, which the DSR keeps; the per-run view shows what this run alone tried)."""
    windows = ledger.store.conn.execute(
        "select distinct b.window_from_ns, b.window_to_ns from backtest_run b join trial t on t.trial_id = b.trial_id "
        "where t.status = 'done' and t.stage in ('S2', 'S3') and (? is null or t.fold like ? || '%') order by 1, 2",
        (fold_prefix, fold_prefix),
    ).fetchall()
    n_s1 = int(
        ledger.store.conn.execute(
            "select count(*) from trial where stage = 'S1' and (? is null or fold like ? || '%')",
            (fold_prefix, fold_prefix),
        ).fetchone()[0]
    )
    out: list[NeffGroup] = []
    for w in windows:
        wf, wt = int(w[0]), int(w[1])
        ids, mat = load_pnl_matrix(ledger, window_from_ns=wf, window_to_ns=wt, fold_prefix=fold_prefix)
        if not ids:
            continue
        corr = pairwise_corr(mat)
        cl = onc_clusters(corr, seed=seed)
        out.append(NeffGroup(wf, wt, tuple(ids), len(ids), cl.n_eff, n_s1, average_offdiag(corr), cl.quality))
    return out


def group_summary(g: NeffGroup) -> dict[str, object]:
    return {
        "window_from_ns": g.window_from_ns,
        "window_to_ns": g.window_to_ns,
        "n_s2plus": g.n_s2plus,
        "n_eff": g.n_eff,
        "n_s1": g.n_s1,
        "n_total": g.n_total,
        "avg_rho": g.avg_rho,
        "quality": None if math.isinf(g.quality) else g.quality,
    }
