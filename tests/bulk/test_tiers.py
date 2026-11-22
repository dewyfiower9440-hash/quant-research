"""v2.3 step 5 (ADR-049 decision 5): the data queue's pure planning: request -> tier, pilot -> projection -> cap
check -> full run, and how each tool exit moves a tier."""

from __future__ import annotations

import pytest

from quant_research.bulk.tiers import (
    GB,
    MAX_ATTEMPTS,
    MAX_STALLS,
    Action,
    TierState,
    after_exit,
    decide,
    next_action,
    parse_state,
    project,
    tier_for,
)


def test_every_queued_request_maps_to_its_preregistered_tier() -> None:
    assert tier_for("binance.um.bulk.metrics_5m").bulk_kind == "metrics"
    assert tier_for("binance.um.bulk.metrics_5m").top == 100
    depth = tier_for("binance.um.bulk.book_depth")
    assert (depth.bulk_kind, depth.top, depth.since) == ("bookDepth", 30, "2023-01")
    assert tier_for("binance.um.bulk.agg_flow_1m").top == 10
    assert tier_for("external (alternative.fng_1d, coinmetrics.asset_1d, deribit.dvol_1h)").source == "external"
    with pytest.raises(ValueError, match="no tier"):
        tier_for("binance.um.bulk.unknown")


def test_a_saved_tier_state_is_parsed_and_a_bad_one_refused() -> None:
    saved = {"status": "piloted", "projected_bytes": 5, "attempts": 1, "note": "n", "stalls": 2}
    assert parse_state("x", saved) == TierState("piloted", 5, 1, "n", 2)
    assert parse_state("x", {}) == TierState()
    with pytest.raises(ValueError, match="unknown status"):
        parse_state("x", {"status": "half"})
    with pytest.raises(ValueError, match="bad number"):
        parse_state("x", {"status": "running", "attempts": "2"})


def test_the_projection_scales_the_pilot_per_symbol_month_with_a_margin() -> None:
    assert project(pilot_bytes=10 * 2**20, pilot_units=2, total_units=200) == int(5 * 2**20 * 200 * 1.25)
    with pytest.raises(ValueError):
        project(pilot_bytes=1, pilot_units=0, total_units=10)


def test_a_tier_whose_projection_crosses_the_lake_cap_or_the_disk_reserve_is_skipped() -> None:
    assert decide(projected=10 * GB, lake_bytes=63 * GB, free_disk=246 * GB)[0]
    ok, why = decide(projected=150 * GB, lake_bytes=63 * GB, free_disk=246 * GB)
    assert not ok and "cap" in why
    ok, why = decide(projected=10 * GB, lake_bytes=63 * GB, free_disk=55 * GB)
    assert not ok and "disk" in why


def test_the_next_action_is_the_first_unfinished_tier_in_queue_order() -> None:
    queue = ["binance.um.bulk.funding", "binance.um.bulk.metrics_5m", "binance.um.bulk.book_depth"]
    states = {"binance.um.bulk.funding": TierState("done")}
    assert next_action(queue, states) == ("binance.um.bulk.metrics_5m", "pilot")
    states["binance.um.bulk.metrics_5m"] = TierState("piloted", projected_bytes=GB)
    assert next_action(queue, states) == ("binance.um.bulk.metrics_5m", "full")
    states["binance.um.bulk.metrics_5m"] = TierState("skipped_cap")
    assert next_action(queue, states) == ("binance.um.bulk.book_depth", "pilot")
    states["binance.um.bulk.book_depth"] = TierState("failed")
    assert next_action(queue, states) is None
    assert next_action(["external (x)"], {}) == ("external (x)", "full")  # key-less sources are small: no pilot


@pytest.mark.parametrize(
    ("before", "action", "code", "status", "attempts"),
    [
        (TierState("pending"), "pilot", 0, "piloted", 0),
        (TierState("pending"), "pilot", 3, "pending", 0),  # paused by a gate: the same step next tick
        (TierState("pending"), "pilot", 2, "pending", 0),  # another bulk job holds the lock
        (TierState("piloted"), "full", 0, "done", 0),
        (TierState("piloted"), "full", 3, "running", 0),
        (TierState("running"), "full", 4, "running", 1),  # failed months are retried
        (TierState("running", attempts=2), "full", 1, "failed", 3),  # bounded retries
        (TierState("piloted"), "full", -9, "running", 0),  # killed at the tick's time budget: resumes, no failure
    ],
)
def test_a_tool_exit_moves_the_tier(before: TierState, action: Action, code: int, status: str, attempts: int) -> None:
    after = after_exit(before, action, code, progressed=False)
    assert (after.status, after.attempts) == (status, attempts)


def test_a_tier_killed_without_progress_is_deferred_behind_the_others_not_failed() -> None:
    """Review (step 5) 1, re-review 2: a month longer than the tick kept the first tier 'running' forever and the
    later tiers never ran; making it 'failed' instead lost a good tier for good. It is deferred and retried last."""
    state = TierState("running", projected_bytes=GB)
    for _ in range(MAX_STALLS - 1):
        state = after_exit(state, "full", -9, progressed=False)
        assert state.status == "running"
    assert after_exit(state, "full", -9, progressed=True).stalls == 0  # progress resets the count
    deferred = after_exit(state, "full", -9, progressed=False)
    assert deferred.status == "deferred" and deferred.stalls == 0
    queue = ["binance.um.bulk.agg_flow_1m", "external (x)"]
    states = {"binance.um.bulk.agg_flow_1m": deferred}
    assert next_action(queue, states) == ("external (x)", "full")  # the others first
    states["external (x)"] = TierState("done")
    assert next_action(queue, states) == ("binance.um.bulk.agg_flow_1m", "full")  # then the deferred one again
    assert next_action(["binance.um.bulk.metrics_5m"], {"binance.um.bulk.metrics_5m": TierState("deferred")}) == (
        "binance.um.bulk.metrics_5m",
        "pilot",  # deferred before its pilot finished
    )


def test_gate_pauses_and_lock_waits_are_not_stalls_and_progress_clears_attempts() -> None:
    """Re-review 1, 2: a heat / RAM pause for half a day, or three transient S3 errors over a week of progress,
    must not end a tier."""
    heat = TierState("running")
    for _ in range(MAX_STALLS * 2):
        heat = after_exit(heat, "full", 3, progressed=False, gate_reason="SSD at 71 C")
    assert (heat.status, heat.stalls) == ("running", 0)
    assert after_exit(TierState("running"), "full", 2, progressed=False).stalls == 0  # another job holds the lock
    state = TierState("running")
    for _ in range(MAX_ATTEMPTS + 2):
        state = after_exit(state, "full", 1, progressed=True)
    assert state.status == "running" and state.attempts == 1  # only the error since the last progress counts


def test_a_pause_at_the_lake_cap_or_the_disk_reserve_is_a_user_decision() -> None:
    cap = after_exit(TierState("running"), "full", 3, progressed=False, gate_reason="bulk lake 200 GB reached the cap")
    disk = after_exit(TierState("running"), "full", 3, progressed=True, gate_reason="free disk 49 GB < 50 GB")
    heat = after_exit(TierState("running"), "full", 3, progressed=True, gate_reason="SSD 72 C")
    assert (cap.status, disk.status, heat.status) == ("skipped_cap", "skipped_cap", "running")
