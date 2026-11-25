"""ADR-056: the conditions of the funnel power measurement, the stage accounting and the pre-registered branch."""

from quant_research.validator.funnel_power import (
    REPS,
    STAGES,
    Condition,
    branch,
    conditions,
    null_conditions,
    null_replications,
    recovery,
    render,
    row_of,
    stages,
)


def test_the_required_conditions_are_three_alphas_by_two_z_by_four_cells() -> None:
    required = conditions((6.0, 4.0))
    assert len(required) == 3 * 2 * 4
    assert [c.z for c in required[:12]] == [6.0] * 12  # z = 6 first (ADR-056 decision 5)
    cells = {(c.alpha, c.role): c.family for c in required if c.z == 6.0}
    assert cells[("random", "expressible")] == "breakout" and cells[("random", "not_expressible")] == "xs_reversal"
    assert cells[("slow_reversion", "expressible")] == "mean_reversion"
    assert cells[("slow_reversion", "not_expressible")] == "momentum"
    assert cells[("cross", "expressible")] == "low_vol" and cells[("cross", "not_expressible")] == "momentum"
    assert {c.family for c in required if c.role in ("combo", "ridge")} == {"combo", "ridge"}
    assert len(conditions((2.0,))) == 12


def test_the_null_condition_runs_every_cell_family_once() -> None:
    null = null_conditions()
    assert [c.family for c in null] == [
        "breakout",
        "xs_reversal",
        "mean_reversion",
        "momentum",
        "low_vol",
        "combo",
        "ridge",
    ]
    assert all(c.alpha == "none" and c.z == 0.0 and c.role == "null" for c in null)
    assert REPS == 43 and len(conditions((6.0, 4.0))) * REPS + len(null) * REPS == 1_333


def _summary(**changes: object) -> dict[str, object]:
    base: dict[str, object] = {
        "s1_finite": 60,
        "s2_done": 5,
        "s2_exposed": 2,
        "best_sr_net": 0.05,
        "s3_sr": 0.1,
        "cross_sr": 0.08,
        "pooled_sr": 0.2,
        "pooled_threshold": 0.11,
    }
    return {**base, **changes}


def test_stages_are_cumulative_and_stop_at_the_first_one_missed() -> None:
    full = stages(_summary(), "candidate", validated=True)
    assert list(full) == list(STAGES) and all(full.values())
    assert stages(_summary(), "candidate", validated=False)["validated"] is False
    below = stages(_summary(pooled_sr=0.05), "below_bar", validated=None)
    assert below["cross_positive"] is True and below["candidate"] is False and below["validated"] is False
    unstable = stages(_summary(s3_sr=-0.02, cross_sr=None, pooled_sr=None), "unstable", validated=None)
    assert unstable["exposed"] is True and unstable["s3_positive"] is False and unstable["cross_positive"] is False
    sparse = stages(_summary(s2_exposed=0, s3_sr=None, cross_sr=None, pooled_sr=None), "sparse", validated=None)
    assert sparse["s2"] is True and sparse["exposed"] is False and sparse["s3_positive"] is False
    nothing = stages(_summary(s1_finite=0, s2_done=0, s2_exposed=0, s3_sr=None), "no_signal", validated=None)
    assert not any(nothing.values())


def test_a_later_stage_never_holds_without_the_earlier_ones() -> None:
    odd = stages(_summary(s2_exposed=0), "candidate", validated=True)  # an inconsistent summary
    assert odd["exposed"] is False and odd["candidate"] is False and odd["validated"] is False


def _rows(alpha: str, z: float, role: str, family: str, recovered: int, n: int = REPS) -> list[dict[str, object]]:
    cond = Condition(alpha, z, family, role)
    out: list[dict[str, object]] = []
    for rep in range(n):
        hit = rep < recovered
        summary = _summary() if hit else _summary(s3_sr=-0.01, cross_sr=None, pooled_sr=None)
        out.append(row_of(cond, rep, "candidate" if hit else "unstable", summary, validated=True if hit else None))
    return out


def _criterion_rows(mech4: int, mech6: int, cross4: int, cross6: int, n: int = REPS) -> list[dict[str, object]]:
    return (
        _rows("slow_reversion", 4.0, "expressible", "mean_reversion", mech4, n)
        + _rows("slow_reversion", 6.0, "expressible", "mean_reversion", mech6, n)
        + _rows("cross", 4.0, "expressible", "low_vol", cross4, n)
        + _rows("cross", 6.0, "expressible", "low_vol", cross6, n)
    )


def _null_rows(candidates: int = 0) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for i, cond in enumerate(null_conditions()):
        for rep in range(REPS):
            hit = i == 0 and rep < candidates
            out.append(row_of(cond, rep, "candidate" if hit else "no_signal", _summary(), validated=None))
    return out


def test_recovery_counts_validated_batches_with_a_wilson_interval() -> None:
    table = recovery(_rows("cross", 4.0, "expressible", "low_vol", 22))
    ((key, cell),) = table.items()
    assert key == ("cross", 4.0, "expressible", "low_vol")
    assert (cell.n, cell.recovered) == (43, 22) and abs(cell.rate - 22 / 43) < 1e-12
    assert cell.low < cell.rate < cell.high and cell.high - cell.low < 0.31  # half-width within 0.15 at n = 43
    assert cell.passed["candidate"] == 22 and cell.passed["s2"] == 43 and cell.passed["s3_positive"] == 22
    assert (cell.failed, cell.insufficient, cell.lag_stress_failed) == (0, 0, 0)


def test_the_branch_is_sufficient_only_when_both_alphas_meet_both_thresholds() -> None:
    # z = 4 needs 0.5 (22 of 43 = 0.512), z = 6 needs 0.8 (35 of 43 = 0.814)
    good = _criterion_rows(22, 35, 22, 35) + _null_rows()
    assert branch(good).verdict == "sufficient"
    assert branch(_criterion_rows(21, 35, 22, 35) + _null_rows()).verdict == "low"  # 21 of 43 = 0.488
    assert branch(_criterion_rows(22, 34, 22, 35) + _null_rows()).verdict == "low"  # 34 of 43 = 0.791
    assert branch(_criterion_rows(43, 43, 22, 34) + _null_rows()).verdict == "low"  # both alphas, not their mean


def test_the_drift_alpha_does_not_decide_the_branch() -> None:
    drift = _rows("random", 4.0, "expressible", "breakout", 0) + _rows("random", 6.0, "expressible", "breakout", 0)
    assert branch(_criterion_rows(22, 35, 22, 35) + _null_rows() + drift).verdict == "sufficient"


def test_too_many_null_candidates_stop_the_branch() -> None:
    # 0 of 301 has a Wilson upper bound of 0.013; 8 of 301 is 0.052
    assert branch(_criterion_rows(43, 43, 43, 43) + _null_rows(7)).verdict == "sufficient"
    stopped = branch(_criterion_rows(43, 43, 43, 43) + _null_rows(8))
    assert stopped.verdict == "null_stop" and "0.05" in stopped.detail


def test_an_unfinished_measurement_gives_no_branch() -> None:
    assert branch(_criterion_rows(22, 35, 22, 35, n=42) + _null_rows()).verdict == "incomplete"
    assert branch(_criterion_rows(22, 35, 22, 35)).verdict == "incomplete"  # no null rows
    assert branch([]).verdict == "incomplete"


def test_the_report_names_the_starting_n_the_stage_counts_and_the_branch() -> None:
    rows = _criterion_rows(22, 35, 22, 35) + _null_rows()
    text = render(rows, start_n={"w2": 60, "w1": 45}, strengths={"cross:4": 0.0012}, seconds_per_batch=11.5)
    assert "N 105" in text and "sufficient" in text
    assert "| cross | 4 | expressible | low_vol | 43 | 22 |" in text
    for stage in STAGES:
        assert stage in text


def test_a_batch_whose_selection_sr_is_not_positive_is_lost_at_selection_not_at_the_cross_check() -> None:
    """Review: the runner runs an S3 even when the selection SR is not positive; the stage table then blamed the
    cross confirmation for a batch the selection split had already rejected."""
    flags = stages(_summary(best_sr_net=-0.01, cross_sr=None, pooled_sr=None), "no_signal", validated=None)
    assert flags["exposed"] is True and flags["s2_positive"] is False
    assert flags["s3_positive"] is False and flags["cross_positive"] is False


def test_batches_that_did_not_run_keep_the_branch_open() -> None:
    """Review: an error or an infeasible cell is not a failed recovery; counted, it would lower a recovery rate or
    widen the null's denominator."""
    rows = _criterion_rows(43, 43, 43, 43) + _null_rows()
    assert branch(rows).verdict == "sufficient"
    rows[0] = row_of(
        Condition("slow_reversion", 4.0, "mean_reversion", "expressible"), 0, "error", _summary(), validated=None
    )
    stopped = branch(rows)
    assert stopped.verdict == "incomplete" and "slow_reversion z=4: 1" in stopped.detail
    null_broken = _criterion_rows(43, 43, 43, 43) + _null_rows()
    null_broken[-1] = row_of(null_conditions()[-1], 42, "infeasible", _summary(), validated=None)
    assert branch(null_broken).verdict == "incomplete"


def test_a_candidate_without_a_holdout_verdict_and_a_lag_stress_failure_are_counted_apart() -> None:
    cond = Condition("cross", 6.0, "low_vol", "expressible")
    rows = [
        row_of(cond, 0, "candidate", _summary(), validated=True),
        row_of(cond, 1, "candidate", _summary(), validated=False, insufficient=True, reasons=["entries 12 < 30"]),
        row_of(cond, 2, "candidate", _summary(), validated=False, reasons=["stress lag+1: SR not maintained (-0.1)"]),
        row_of(cond, 3, "unstable", _summary(s3_sr=-0.1), validated=None),
    ]
    cell = recovery(rows)[cond.key]
    assert (cell.n, cell.recovered, cell.insufficient, cell.lag_stress_failed) == (4, 1, 1, 1)
    assert cell.passed["candidate"] == 3


def test_null_candidates_are_also_counted_per_replication() -> None:
    assert null_replications(_null_rows(3)) == (43, 3)
    both = _null_rows()
    both[0] = row_of(null_conditions()[0], 0, "candidate", _summary(), validated=None)
    both[43] = row_of(null_conditions()[1], 0, "candidate", _summary(), validated=None)
    assert null_replications(both) == (43, 1)  # two cells of the same replication share its markets
