"""Phase 4 step 9: MAP-Elites archive with pre-registered descriptor bins, deterministic replacement,
shrinkage by sample size and a signal-correlation cap on a fixed probe set."""

from __future__ import annotations

import numpy as np
import pytest

from quant_research.search.archive import (
    PROBE_POINTS,
    Archive,
    Descriptor,
    Elite,
    exposure_metrics,
    n_cells,
    probe_corr,
    probe_vector,
)


def _elite(
    tid: int,
    score: float,
    *,
    n: int = 120,
    probe: tuple[float, ...] | None = None,
    turnover: float = 5.0,
    hold: float = 10.0,
    net: float = 0.0,
    family: str = "trend",
    h: str | None = None,
) -> Elite:
    p = probe if probe is not None else tuple(float(np.sin(i * 0.37 + tid)) for i in range(PROBE_POINTS))
    return Elite(tid, h or f"h{tid}", family, score, n, Descriptor(turnover, hold, net), p)


def test_descriptor_bins_are_pre_registered_and_cover_the_grid() -> None:
    assert Descriptor(0.0, 0.0, -1.0).cell() == (0, 0, 0)
    assert Descriptor(1_000.0, 1_000.0, 1.0).cell() == (4, 5, 4)
    assert Descriptor(2.0, 8.0, 0.1).cell() == (1, 2, 3)  # right-closed edges
    assert n_cells() == 5 * 6 * 5
    d = Descriptor.of({"turnover": 3.0, "avg_holding_hours": float("nan"), "net_exposure": 0.2})
    assert d == Descriptor(3.0, 0.0, 0.2)  # undefined -> 0


def test_exposure_metrics_and_probe_vector_from_target_weights() -> None:
    w = np.array([[0.5, -0.5], [1.0, 0.0], [np.nan, 0.25], [0.0, 0.0]])
    m = exposure_metrics(w)
    assert m["net_exposure"] == pytest.approx((0.0 + 1.0 + 0.25 + 0.0) / 4)
    assert m["gross_exposure"] == pytest.approx((1.0 + 1.0 + 0.25 + 0.0) / 4)
    p = probe_vector(w, 4)
    assert p == [0.0, 1.0, 0.25, 0.0]
    assert len(probe_vector(w)) == PROBE_POINTS and probe_vector(np.zeros((0, 2)), 3) == [0.0, 0.0, 0.0]
    assert probe_corr([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1.0)
    assert probe_corr([1, 1, 1], [1, 2, 3]) == 0.0


def test_replacement_is_deterministic_and_ties_keep_the_lower_trial_id() -> None:
    a = Archive(k0=0.0)
    assert a.offer(_elite(5, 0.10)).accepted
    worse = a.offer(_elite(6, 0.05, probe=tuple(float(np.cos(i)) for i in range(PROBE_POINTS))))
    assert not worse.accepted and "keeps the cell" in worse.reason
    tie = a.offer(_elite(7, 0.10, probe=tuple(float(np.cos(i * 1.3)) for i in range(PROBE_POINTS))))
    assert not tie.accepted  # same shrunk score, higher trial id loses
    tie_low = a.offer(_elite(2, 0.10, probe=tuple(float(np.cos(i * 1.7)) for i in range(PROBE_POINTS))))
    assert tie_low.accepted and tie_low.replaced == 5
    better = a.offer(_elite(8, 0.20, probe=tuple(float(np.cos(i * 2.1)) for i in range(PROBE_POINTS))))
    assert better.accepted and better.replaced == 2
    assert [e.trial_id for e in a.ranked()] == [8]
    assert a.offers == 5


def test_shrinkage_pulls_small_samples_towards_the_prior() -> None:
    a = Archive(k0=30.0, prior_mean=0.0)
    big = _elite(1, 0.10, n=300)
    small = _elite(2, 0.12, n=10)  # higher raw score, tiny sample
    assert a.shrunk(small) < a.shrunk(big)  # the small sample is shrunk harder
    assert a.shrunk(_elite(3, 0.10, n=10**9)) == pytest.approx(0.10)
    assert a.shrunk(_elite(4, 0.10, n=0)) == pytest.approx(0.0)
    with pytest.raises(ValueError):
        Archive(corr_cap=0.0)


def test_correlated_candidates_are_semantic_duplicates_unless_better() -> None:
    a = Archive(k0=0.0, corr_cap=0.9)
    base = tuple(float(np.sin(i * 0.2)) for i in range(PROBE_POINTS))
    assert a.offer(_elite(1, 0.10, probe=base, turnover=1.0)).accepted
    near = tuple(v * 2.0 + 0.01 for v in base)  # rho = 1 with the elite, but a different cell
    dup = a.offer(_elite(2, 0.05, probe=near, turnover=50.0))
    assert not dup.accepted and "semantic duplicate" in dup.reason
    rep = a.offer(_elite(3, 0.30, probe=near, turnover=50.0))
    assert rep.accepted and rep.replaced == 1 and len(a.cells) == 1
    assert next(iter(a.cells.values())).trial_id == 3
    other = tuple(float(np.cos(i * 1.1)) for i in range(PROBE_POINTS))
    assert a.offer(_elite(4, 0.01, probe=other, turnover=1.0)).accepted  # uncorrelated: its own cell
    div = a.diversity()
    assert div["cells"] == 2.0 and div["fill_rate"] == pytest.approx(2 / n_cells())
    assert -1.0 <= div["mean_pairwise_corr"] <= 1.0


def test_archive_round_trips_through_json() -> None:
    a = Archive(k0=10.0, corr_cap=0.8)
    a.offer(_elite(1, 0.1, family="trend"))
    a.offer(
        _elite(2, 0.2, family="momentum", turnover=50.0, probe=tuple(float(np.cos(i)) for i in range(PROBE_POINTS)))
    )
    b = Archive.from_json(a.to_json())
    assert b.to_json() == a.to_json() and b.offers == 2 and b.diversity()["families"] == 2.0
    assert [e.trial_id for e in b.ranked()] == [e.trial_id for e in a.ranked()]
