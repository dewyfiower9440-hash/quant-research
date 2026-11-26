"""Review of step 8a: the run rules of tools/measure_funnel_power.py. Which command lines are registered runs, when
the report of ADR-060 may be written, and that no run of the re-measurement prints a pass count (decision 9)."""

from __future__ import annotations

import json

import numpy as np
import pytest
from search_fixtures import DAYS, NAMES, T0, H, walk_klines

from quant_research.backtest.panel import Panel, PanelConfig, assemble, decision_grid, settlements
from quant_research.validator.funnel_power import (
    NULL_REPS_REMEASURE,
    REPS,
    Condition,
    RunKind,
    closing_line,
    conditions,
    criterion_conditions,
    design_keys,
    key_of,
    null_conditions,
    oracle_tail,
    report_problems,
    row_of,
    run_kind,
    variant_problem,
)
from quant_research.validator.null_pipeline import (
    SyntheticSpec,
    hourly_closes,
    oracle_pnl,
    parse_alpha,
    synthetic_panel,
)

AFTER: dict[str, object] = {"space_version": 4, "n_rule": "cell"}
BEFORE: dict[str, object] = {"space_version": 3, "n_rule": "raw"}
SUMMARY: dict[str, object] = {
    "s1_finite": 60,
    "s2_done": 5,
    "s2_exposed": 2,
    "best_sr_net": 0.05,
    "s3_sr": 0.1,
    "cross_sr": 0.08,
    "pooled_sr": 0.2,
    "pooled_threshold": 0.09,
}


def _kind(**changes: object) -> RunKind:
    args: dict[str, object] = {
        "space_version": 3,
        "n_rule": "raw",
        "only_criterion": False,
        "seed": 56,
        "reps": None,
        "null_reps": None,
        "zs": [6.0, 4.0],
        "setup_given": False,
        "trial": False,
    } | changes
    return run_kind(**json.loads(json.dumps(args)))  # as argparse hands them over: plain values


def _rows(
    conds: list[Condition], reps: int, variant: dict[str, object] | None, *, recovered: bool = True
) -> list[dict[str, object]]:
    return [
        row_of(
            c,
            rep,
            "candidate" if recovered else "unstable",
            SUMMARY,
            validated=True if recovered else None,
            variant=variant,
        )
        for c in conds
        for rep in range(reps)
    ]


def _after() -> list[dict[str, object]]:
    return _rows(null_conditions(), NULL_REPS_REMEASURE, AFTER, recovered=False) + _rows(conditions(), REPS, AFTER)


def _before() -> list[dict[str, object]]:
    return _rows(criterion_conditions(), REPS, BEFORE)


def test_the_three_registered_runs_are_recognised() -> None:
    assert _kind() == "first"  # ADR-056 as it always ran
    assert _kind(zs=[6.0, 4.0, 2.0]) == "first"  # the optional z = 2 of ADR-056
    assert _kind(space_version=4, n_rule="cell", seed=57) == "after"
    assert _kind(space_version=4, n_rule="cell", seed=57, null_reps=111, reps=43, zs=[6.0]) == "after"  # a split call
    assert _kind(only_criterion=True, seed=57, setup_given=True) == "before"


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"space_version": 4, "seed": 57}, "not a registered run"),  # v4 with the raw rule
        ({"n_rule": "cell", "seed": 57}, "not a registered run"),  # the cell rule on v3
        ({"space_version": 4, "n_rule": "cell", "only_criterion": True, "seed": 57}, "not a registered run"),
        ({"seed": 57}, "not a registered run"),  # the before run with --only-criterion forgotten
        ({"seed": 57, "setup_given": True}, "not a registered run"),  # the same, with the shared setup given
        ({"null_reps": 111}, "not a registered run"),  # the first measurement has no separate null count
        ({"setup_given": True}, "not a registered run"),
        ({"space_version": 4, "n_rule": "cell"}, "seed 56"),  # the after run on the first measurement's markets
        ({"space_version": 4, "n_rule": "cell", "seed": 57, "reps": 10}, "--reps 10"),
        ({"space_version": 4, "n_rule": "cell", "seed": 57, "null_reps": 73}, "--null-reps 73"),
        ({"space_version": 4, "n_rule": "cell", "seed": 57, "zs": [6.0, 4.0, 2.0]}, "--z"),
        ({"only_criterion": True, "seed": 57}, "give --setup"),  # another N snapshot would be fixed silently
        ({"only_criterion": True, "seed": 56, "setup_given": True}, "seed 56"),
        ({"only_criterion": True, "seed": 57, "setup_given": True, "null": True}, "--null"),  # it has no null
    ],
)
def test_a_command_line_that_is_no_registered_run_is_refused(changes: dict[str, object], why: str) -> None:
    with pytest.raises(ValueError, match=why):
        _kind(**changes)
    assert _kind(**changes, trial=True) == "trial"  # only as an explicit trial, which prints and reports nothing


def test_the_registered_designs_have_the_registered_sizes() -> None:
    after, before = design_keys("after"), design_keys("before")
    assert (len(after), len(set(after))) == (1_809, 1_809) and (len(before), len(set(before))) == (172, 172)
    assert sum(1 for k in after if k[0] == "none") == 777 and {k[1] for k in after} == {0.0, 4.0, 6.0}
    assert set(before) <= set(after) - {k for k in after if k[0] == "none"}  # the same cells and replications
    assert len(design_keys("after", zs=[6.0], null=False)) == 516 and len(design_keys("after", zs=[], null=True)) == 777
    with pytest.raises(ValueError, match="only the runs of ADR-060"):
        design_keys("first")


def test_a_state_file_never_mixes_variants() -> None:
    first = [row_of(criterion_conditions()[0], 0, "candidate", SUMMARY, validated=True)]
    after, before = _rows(criterion_conditions(), 2, AFTER), _rows(criterion_conditions(), 2, BEFORE)
    assert variant_problem([], "after", AFTER) is None and variant_problem(after, "after", AFTER) is None
    assert variant_problem(first, "first", {}) is None and variant_problem(before, "before", BEFORE) is None
    assert "8 rows of another space version, N rule or kind of run" in (variant_problem(before, "after", AFTER) or "")
    assert "1 rows without a variant" in (
        variant_problem(first, "before", BEFORE) or ""
    )  # the first measurement's file
    assert "8 rows of a re-measurement or a trial" in (variant_problem(after, "first", {}) or "")
    assert (
        variant_problem(after, "trial", {"space_version": 4, "n_rule": "raw"}) is not None
    )  # v4 / raw is its own file
    # a trial in a registered variant still gets a file of its own, and a registered run never continues a trial's
    assert "8 rows of another space version, N rule or kind of run" in (variant_problem(after, "trial", AFTER) or "")
    trial_rows = [dict(r) | {"trial": True} for r in after]
    assert variant_problem(trial_rows, "trial", AFTER) is None and variant_problem([], "trial", AFTER) is None
    assert variant_problem(trial_rows, "after", AFTER) is not None


def test_the_report_waits_until_both_runs_hold_their_whole_design() -> None:
    after, before = _after(), _before()
    assert report_problems(after, before, seed=57) == []
    assert report_problems(after, [], seed=57) == ["before: 172 of 172 batches are missing"]
    null_only = [r for r in after if r["alpha"] == "none"]
    assert report_problems(null_only, before, seed=57) == [
        "after: 1032 of 1809 batches are missing"
    ]  # 777 rows are not enough
    planted_only = [r for r in after if r["alpha"] != "none"]
    assert report_problems(planted_only, before, seed=57) == [
        "after: 777 of 1809 batches are missing"
    ]  # --null forgotten
    assert report_problems(after[:-1], before[1:], seed=57) == [
        "after: 1 of 1809 batches are missing",
        "before: 1 of 172 batches are missing",
    ]
    swapped = report_problems(after, [dict(r) | AFTER for r in before], seed=57)  # the before file in the wrong variant
    assert swapped == ["before: 172 rows are not space version 3 / N rule raw"]
    plain = [{k: v for k, v in r.items() if k not in AFTER} for r in after]  # rows without a variant
    assert report_problems(plain, before, seed=57)[0] == "after: 1809 rows are not space version 4 / N rule cell"
    wide = before + _rows([Condition("random", 6.0, "breakout", "expressible")], 1, BEFORE)
    assert report_problems(after, wide, seed=57) == ["before: 1 batches are outside the registered design"]
    doubled = before + before[:5]  # two processes appended to one state file
    assert report_problems(after, doubled, seed=57) == ["before: 5 batches are there more than once"]
    assert report_problems(after + after[:1], before, seed=57) == ["after: 1 batches are there more than once"]
    marked = [dict(r) | {"trial": True} for r in before]
    assert report_problems(after, marked, seed=57) == ["before: 172 rows are of a trial run"]
    assert report_problems(after, before, seed=9057) == ["the setup was fixed with seed 9057, not the registered 57"]
    assert not any(
        "candidate" in line or "validated" in line for line in report_problems(after[:5], before[:5], seed=57)
    )


@pytest.mark.parametrize("kind", ["after", "before", "trial"])
def test_a_run_of_the_re_measurement_prints_no_pass_count(kind: RunKind) -> None:
    """ADR-060 decision 9, whether the call is a part of a split run, complete, or an unregistered trial."""
    rows = _after() if kind != "before" else _before()
    wanted = design_keys("after", zs=[6.0], null=False) if kind != "before" else design_keys("before")
    for part in (rows[:10], rows):
        line = closing_line(kind, part, ran=len(part), wanted=wanted, seconds_per_batch=11.26, report=None)
        assert set(line) == {
            "kind",
            "ran",
            "batches",
            "this_call_missing",
            "seconds_per_batch",
            "failed",
            "report",
            "report_waits_for",
        }
        text = json.dumps(line)
        assert "candidate" not in text and "validated" not in text and "recover" not in text
        assert line["batches"] == len(part) and line["seconds_per_batch"] == 11.3 and line["report"] is None
    done = {key_of(r) for r in rows}
    assert closing_line(kind, rows, ran=0, wanted=wanted, seconds_per_batch=None, report=None)[
        "this_call_missing"
    ] == len(set(wanted) - done)
    failed = [dict(rows[0]) | {"cause": "error"}, dict(rows[1]) | {"cause": "infeasible"}, rows[2]]
    assert closing_line(kind, failed, ran=3, wanted=wanted, seconds_per_batch=None, report=None)["failed"] == 2
    waiting = closing_line(
        kind, rows, ran=0, wanted=wanted, seconds_per_batch=None, report=None, waiting=["before: 172"]
    )
    assert waiting["report_waits_for"] == ["before: 172"]


def test_the_first_measurement_keeps_the_line_it_always_printed() -> None:
    rows = [row_of(c, 0, "candidate", SUMMARY, validated=True) for c in criterion_conditions()]
    rows.append(row_of(criterion_conditions()[0], 1, "error", SUMMARY, validated=None))
    line = closing_line("first", rows, ran=5, wanted=[], seconds_per_batch=14.06, report="x.md")
    assert line == {"ran": 5, "batches": 5, "seconds_per_batch": 14.1, "candidates": 4, "errors": 1, "report": "x.md"}


def test_the_oracle_window_ends_at_the_last_live_bar() -> None:
    """Review finding 7: a synthetic panel repeats its last closes; the oracle's window must not end on dead bars."""
    rng = np.random.default_rng(11)
    rets = rng.standard_normal((120, 6)) * 0.02
    closes = 100.0 * np.exp(np.vstack([np.zeros((1, 6)), np.cumsum(rets, axis=0)]))
    padded = np.vstack([closes, closes[-1:], closes[-1:]])  # two decisions without a new bar
    alpha = parse_alpha("slow_reversion")
    full = oracle_pnl(alpha, rets)
    tail = oracle_tail(alpha, padded, 50)
    # the last 50 decisions hold 48 live bars: the window keeps the split's start and leaves the dead bars out
    assert tail.shape == (48,) and np.allclose(tail, full[-48:], rtol=0, atol=1e-12) and tail[-1] != 0.0
    whole = oracle_tail(alpha, closes, 50)  # a market without a dead tail: every one of the 50 bars
    assert whole.shape == (50,) and np.allclose(whole, full[-50:], rtol=0, atol=1e-12)
    assert oracle_tail(alpha, padded, 2).shape == (0,) and oracle_tail(alpha, padded, 3).shape == (1,)
    naive = oracle_pnl(alpha, np.diff(np.log(padded), axis=0))[-50:]
    assert naive[-1] == 0.0 and naive[-2] == 0.0  # what the tool computed before
    assert oracle_tail(alpha, padded, 10_000).shape == full.shape  # fewer live bars than asked: all of them
    flat = np.full((30, 6), 100.0)
    assert oracle_tail(alpha, flat, 10).shape == (0,)  # a market that never moved has no oracle pnl
    for name in ("random", "cross"):
        other = parse_alpha(name)
        assert np.allclose(oracle_tail(other, padded, 40), oracle_pnl(other, rets)[-38:], rtol=0, atol=1e-12)


def test_a_synthetic_market_ends_on_bars_without_a_move_and_the_oracle_window_skips_them() -> None:
    """The trap behind review finding 7, on a real synthetic panel (not a constructed matrix)."""
    cfg = PanelConfig(H)
    frame = assemble(decision_grid(T0, T0 + (DAYS * 24 - 2) * H, cfg), NAMES, walk_klines(NAMES, DAYS, drift=0.0), cfg)
    base = Panel(frame, settlements(None, None), [("lake/rw.parquet", 1)], cfg)
    spec = SyntheticSpec(alpha=parse_alpha("slow_reversion"), strength=0.3, block_len=5, hours=201)
    closes = hourly_closes(synthetic_panel(base, spec, seed=3))[2]
    rets = np.diff(np.log(closes), axis=0)
    assert np.all(rets[-2:] == 0.0) and np.any(rets[-3] != 0.0)  # precondition: the dead tail is there
    tail = oracle_tail(parse_alpha("slow_reversion"), closes, 50)
    assert tail.shape == (48,) and tail[-1] != 0.0  # the 50 last decisions minus the two without a bar
    assert np.allclose(tail, oracle_pnl(parse_alpha("slow_reversion"), rets[:-2])[-48:], rtol=0, atol=1e-12)
