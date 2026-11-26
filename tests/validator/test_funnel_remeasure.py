"""ADR-060: the pure part of the re-measurement after step 8a. The null read per replication, the raw-N reading
kept beside the cell rule, the oracle ceiling and the before / after pairing."""

from __future__ import annotations

import math

import numpy as np
import pytest

from quant_research.validator.funnel_power import (
    NULL_REPS_REMEASURE,
    REPS,
    Condition,
    as_raw_rule,
    branch,
    criterion_conditions,
    null_conditions,
    oracle_passes,
    oracle_rates,
    paired,
    recovery,
    render_remeasure,
    row_of,
)
from quant_research.validator.null_pipeline import oracle_pnl, parse_alpha, realizable_z
from quant_research.validator.verdict import DecisionRule

VARIANT: dict[str, object] = {"space_version": 4, "n_rule": "cell"}
PASS: dict[str, object] = {
    "s1_finite": 60,
    "s2_done": 5,
    "s2_exposed": 2,
    "best_sr_net": 0.05,
    "s3_sr": 0.1,
    "cross_sr": 0.08,
    "pooled_sr": 0.2,
    "pooled_threshold": 0.09,
    "pooled_threshold_raw": 0.13,
}
MISS: dict[str, object] = PASS | {"s3_sr": -0.01, "cross_sr": None, "pooled_sr": None, "pooled_threshold_raw": None}


def _cell(cond: Condition, recovered: int, n: int, *, oracle: int = 0, pooled: float = 0.2) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for rep in range(n):
        hit = rep < recovered
        out.append(
            row_of(
                cond,
                rep,
                "candidate" if hit else "unstable",
                PASS | {"pooled_sr": pooled} if hit else MISS,
                validated=True if hit else None,
                variant=VARIANT,
                sizing="portfolio_vol_target" if rep % 2 else "equal",
                oracle=None if cond.alpha == "none" else rep < oracle,
            )
        )
    return out


def _criterion(mech4: int, mech6: int, cross4: int, cross6: int, **kw: int) -> list[dict[str, object]]:
    counts = {
        ("slow_reversion", 4.0): mech4,
        ("slow_reversion", 6.0): mech6,
        ("cross", 4.0): cross4,
        ("cross", 6.0): cross6,
    }
    rows: list[dict[str, object]] = []
    for cond in criterion_conditions():
        rows += _cell(cond, counts[(cond.alpha, cond.z)], REPS, **kw)
    return rows


def _null(hit_reps: int = 0, reps: int = NULL_REPS_REMEASURE, *, cells_hit: int = 1) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for i, cond in enumerate(null_conditions()):
        for rep in range(reps):
            hit = i < cells_hit and rep < hit_reps
            out.append(row_of(cond, rep, "candidate" if hit else "no_signal", MISS, validated=None, variant=VARIANT))
    return out


def test_the_before_run_measures_the_four_cells_the_branch_reads_in_running_order() -> None:
    assert [(c.alpha, c.z, c.family, c.role) for c in criterion_conditions()] == [
        ("slow_reversion", 6.0, "mean_reversion", "expressible"),
        ("cross", 6.0, "low_vol", "expressible"),
        ("slow_reversion", 4.0, "mean_reversion", "expressible"),
        ("cross", 4.0, "low_vol", "expressible"),
    ]
    assert NULL_REPS_REMEASURE == 111 and len(_null()) == 777


def test_the_null_is_read_per_replication_and_two_replications_with_a_candidate_stop_the_branch() -> None:
    good = _criterion(30, 40, 30, 40)

    def decide(null: list[dict[str, object]]) -> str:
        return branch(null + good, null_reps=NULL_REPS_REMEASURE, null_unit="replication").verdict

    assert decide(_null(0)) == "sufficient"
    assert decide(_null(1)) == "sufficient"  # 1 / 111: Wilson upper 0.049
    stopped = branch(_null(2) + good, null_reps=NULL_REPS_REMEASURE, null_unit="replication")
    assert stopped.verdict == "null_stop" and "2 of 111" in stopped.detail and "0.063" in stopped.detail
    # one replication with candidates in several cells is still one replication (their markets are shared)
    assert decide(_null(1, cells_hit=7)) == "sufficient"
    assert branch(_null(1, cells_hit=7) + good, null_reps=NULL_REPS_REMEASURE).verdict == "sufficient"  # 7 / 777
    assert branch(_null(2, cells_hit=7) + good, null_reps=NULL_REPS_REMEASURE).verdict == "sufficient"  # 14 / 777
    assert decide(_null(2, cells_hit=7)) == "null_stop"  # the batch unit would not have stopped here
    short = branch(_null(0, reps=REPS) + good, null_reps=NULL_REPS_REMEASURE, null_unit="replication")
    assert short.verdict == "incomplete" and "43 of 111" in short.detail
    assert (
        decide(_null(0)) == "sufficient"
        and branch(_null(0) + _criterion(21, 40, 30, 40), null_reps=111).verdict == "low"
    )


def test_a_row_of_the_re_measurement_carries_its_variant_and_the_first_measurements_rows_do_not() -> None:
    cond = criterion_conditions()[0]
    plain = row_of(cond, 0, "candidate", PASS, validated=True)
    assert not {"space_version", "n_rule", "pooled_threshold_raw", "sizing", "oracle_pass"} & set(plain)
    row = row_of(cond, 0, "candidate", PASS, validated=True, variant=VARIANT, sizing="equal", oracle=True)
    assert {k: row[k] for k in ("space_version", "n_rule", "pooled_threshold_raw", "sizing", "oracle_pass")} == {
        "space_version": 4,
        "n_rule": "cell",
        "pooled_threshold_raw": 0.13,
        "sizing": "equal",
        "oracle_pass": True,
    }
    assert {k: v for k, v in row.items() if k in plain} == plain


def test_the_raw_n_reading_drops_candidates_that_only_cleared_the_cell_bar() -> None:
    cond = criterion_conditions()[0]
    over = _cell(cond, 5, 10, pooled=0.2)  # above the raw bar (0.13) too
    between = _cell(cond, 5, 10, pooled=0.1)  # above the cell bar (0.09) only
    assert recovery(as_raw_rule(over))[cond.key].recovered == 5
    raw = as_raw_rule(between)
    assert recovery(raw)[cond.key].recovered == 0 and recovery(raw)[cond.key].passed["candidate"] == 0
    assert recovery(raw)[cond.key].passed["cross_positive"] == 5  # the earlier stages are as they were
    assert [r["cause"] for r in raw[:6]] == ["below_bar"] * 5 + ["unstable"]
    assert recovery(between)[cond.key].recovered == 5 and between[0]["cause"] == "candidate"  # the input is not changed
    first_measurement = [row_of(cond, 0, "candidate", PASS, validated=True)]  # no raw bar recorded: the raw rule
    assert as_raw_rule(first_measurement) == first_measurement


def _series(sr: float, days: int, seed: int) -> np.ndarray:
    x = np.random.default_rng(seed).standard_normal(days)
    x = (x - x.mean()) / x.std(ddof=1)
    return np.asarray(x + sr, dtype=np.float64)  # exactly this daily SR


def test_the_oracle_passes_only_above_the_pooled_bar_and_with_a_significant_holdout() -> None:
    rule = DecisionRule()
    strong_in, strong_hold = _series(0.25, 454, 1), _series(0.25, 700, 2)
    assert oracle_passes(strong_in, strong_hold, bar=0.13, rule=rule) is True
    assert oracle_passes(_series(0.12, 454, 1), strong_hold, bar=0.13, rule=rule) is False  # under the bar
    exact = float(np.mean(strong_in)) / float(np.std(strong_in, ddof=1))
    assert oracle_passes(strong_in, strong_hold, bar=exact, rule=rule) is False  # at the bar is not above it
    weak_hold = _series(0.02, 700, 2)  # z = 0.5 over 700 days: nowhere near alpha / k
    assert oracle_passes(strong_in, weak_hold, bar=0.13, rule=rule) is False
    assert oracle_passes(np.zeros(454), strong_hold, bar=0.0, rule=rule) is False  # no variance: no SR


def test_oracle_results_are_counted_once_per_replication() -> None:
    rows: list[dict[str, object]] = []
    for family, role in (("mean_reversion", "expressible"), ("momentum", "not_expressible"), ("combo", "combo")):
        rows += _cell(Condition("slow_reversion", 6.0, family, role), 0, 10, oracle=7)
    rows += _cell(Condition("cross", 4.0, "low_vol", "expressible"), 0, 10, oracle=0)
    rows += _null(0, reps=3)
    assert oracle_rates(rows) == {("slow_reversion", 6.0): (10, 7), ("cross", 4.0): (10, 0)}


def test_the_oracle_pnl_is_the_strategy_the_strength_calibration_scores() -> None:
    rets = np.random.default_rng(5).standard_normal((300, 12)) * 0.03
    for name, dropped in (("random", 0), ("mechanism", 1), ("slow_reversion", 5), ("cross", 20)):
        alpha = parse_alpha(name)
        pnl = oracle_pnl(alpha, rets)
        assert pnl.shape == (300 - dropped,)
        z = float(np.mean(pnl)) / float(np.std(pnl, ddof=1)) * math.sqrt(pnl.shape[0])
        assert realizable_z(alpha, rets) == pytest.approx(z)
    later = rets.copy()
    later[200:] = 0.5
    assert np.array_equal(
        oracle_pnl(parse_alpha("slow_reversion"), later)[:195], oracle_pnl(parse_alpha("slow_reversion"), rets)[:195]
    )


def test_before_and_after_are_paired_by_replication() -> None:
    cond = Condition("cross", 6.0, "low_vol", "expressible")
    before = _cell(cond, 3, 10)  # replications 0, 1, 2 recovered
    after = [r for r in _cell(cond, 6, 12) if r["rep"] != 1]  # 0, 2, 3, 4, 5 recovered; 1 missing, 10 and 11 extra
    out = paired(before, after)
    assert out[("cross", 6.0, "low_vol")] == (2, 0, 3, 4)  # both: 0, 2; after only: 3, 4, 5; neither: 6..9
    assert out[("slow_reversion", 4.0, "mean_reversion")] == (0, 0, 0, 0)  # nothing measured in both
    assert set(out) == {(c.alpha, c.z, c.family) for c in criterion_conditions()}
    lost = paired(after, before)[("cross", 6.0, "low_vol")]
    assert lost == (2, 3, 0, 4)


def test_the_report_gives_both_readings_the_null_in_both_units_and_the_auxiliary_tables() -> None:
    after = _null(1, cells_hit=2) + _criterion(10, 20, 12, 30, oracle=40)
    before = [dict(r) | {"space_version": 3, "n_rule": "raw"} for r in _criterion(0, 0, 0, 7)]
    text = render_remeasure(
        after,
        before,
        start_n={"w1": 110, "w2": 112},
        start_pairs=60,
        strengths={"cross:6": 0.009},
        seconds_per_batch=14.2,
    )
    assert "쌍의 수 60" in text and "원시 판정 수 222" in text and "14.2초" in text
    assert "주 판정(ADR-056 결정 8, 귀무는 ADR-060 결정 8): **low**" in text
    assert "원시 N 기준으로 읽은 같은 판정(병행 보고): **low**" in text
    assert "후보가 하나라도 나온 반복 1 / 111 (Wilson 95% 상한 0.049)" in text
    assert "후보 2 / 777" in text
    assert "| cross | 6 | 43 | 40 | 0.930 |" in text and "low_vol 0.75" in text  # 30 / 43 over 40 / 43
    assert "| cross | 6 | low_vol | 7 | 0 | 23 | 13 |" in text
    assert "수정 뒤 949, 수정 전 172" in text
    none = render_remeasure(
        _null(0) + _criterion(1, 1, 1, 1), [], start_n={"w2": 1}, start_pairs=2, strengths={}, seconds_per_batch=None
    )
    assert "오라클 통과 0: 비율 없음" in none and "미측정" in none
