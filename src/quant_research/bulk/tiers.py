"""Data queue planning, pure core (v2.3 step 5, ADR-049 decision 5, tiers of ADR-044 / design v2.2 §3).

The campaign writes the datasets its cells asked for to `run/data-queue.json`; `tools/run_data_queue.py` consumes
them one action per scheduled tick. A bulk tier first runs a pilot (its top symbols, newest month), whose lake bytes
are projected to the full tier; the full run starts only when the projection stays under the lake cap and leaves
the disk reserve, otherwise the tier is `skipped_cap` and the decision goes to the user. Tool exits move a tier:
0 = step done; 2 (lock held), 3 (gate pause) or a kill at the tick's time budget = the same step next tick (the
download tools resume from the catalog); 1 / 4 = retried a bounded number of times. Pure; the tool does the I/O."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal, cast

from quant_research.bulk.plan import slowing

GB = 1024**3
LAKE_CAP = 200 * GB  # design v2.1 §14 / bulk.guard.Limits.max_bulk_bytes (user decision to raise)
DISK_RESERVE = 50 * GB  # bulk.guard.Limits.min_free_disk
MARGIN = 1.25  # projection safety factor: pilot months are recent, older months are usually smaller
MAX_ATTEMPTS = 3
MAX_STALLS = 6  # ticks in a row that paused or were killed without a new (symbol, month) file

Status = Literal["pending", "piloted", "running", "deferred", "done", "skipped_cap", "failed"]
Action = Literal["pilot", "full"]
TERMINAL: tuple[Status, ...] = ("done", "skipped_cap", "failed")


@dataclass(frozen=True, slots=True)
class Tier:
    source: Literal["bulk", "external"]
    bulk_kind: str | None = None  # tools/bulk_extras.py --kinds
    top: int | None = None  # --top (None: every universe symbol)
    since: str | None = None  # --since YYYY-MM


# ADR-044 tiers T1..T5, T7 (T6 Upbit backfill is a user-registered task)
TIERS: dict[str, Tier] = {
    "binance.um.bulk.funding": Tier("bulk", "fundingRate"),
    # ADR-049 addendum 2: tiers a family reads take every universe symbol in its member months. `--top N` picks
    # the symbols with the most member months up to today: a look-ahead choice that left most of an earlier
    # month's universe without data (41 % covered in 2025-06) and biased the rest towards survivors.
    "binance.um.bulk.metrics_5m": Tier("bulk", "metrics"),
    "binance.spot.bulk.klines_1m": Tier("bulk", "spotKlines"),
    "binance.um.bulk.book_depth": Tier("bulk", "bookDepth", top=30, since="2023-01"),
    "binance.um.bulk.agg_flow_1m": Tier("bulk", "aggTrades", top=10),
}
EXTERNAL = Tier("external")


def tier_for(request: str) -> Tier:
    """The tier of a queued request: a dataset id, or the 'external (...)' entry of the default tiers."""
    if request.startswith("external"):
        return EXTERNAL
    if request not in TIERS:
        raise ValueError(f"no tier for the data request {request!r} (known: {', '.join(TIERS)}, external)")
    return TIERS[request]


@dataclass(frozen=True, slots=True)
class TierState:
    status: Status = "pending"
    projected_bytes: int | None = None
    attempts: int = 0
    note: str = ""
    stalls: int = 0
    recent_new: tuple[int, ...] = ()  # new (symbol, month) files of the last budget-long ticks, newest last


STATUSES: tuple[Status, ...] = ("pending", "piloted", "running", "deferred", "done", "skipped_cap", "failed")


def parse_state(request: str, entry: Mapping[str, object]) -> TierState:
    """One entry of `run/data-queue-state.json` (boundary: an unknown status or a bad number fails loudly)."""
    raw = entry.get("status", "pending")
    status: Status | None = None
    for s in STATUSES:
        if s == raw:
            status = s
    if status is None:
        raise ValueError(f"data queue state: tier {request!r} has an unknown status {entry.get('status')!r}")
    projected, attempts, stalls = entry.get("projected_bytes"), entry.get("attempts", 0), entry.get("stalls", 0)
    if not (projected is None or isinstance(projected, int)) or not isinstance(attempts, int):
        raise ValueError(f"data queue state: tier {request!r} has a bad number in {dict(entry)}")
    if not isinstance(stalls, int):
        raise ValueError(f"data queue state: tier {request!r} has a bad number in {dict(entry)}")
    raw_recent = entry.get("recent_new", [])
    if not isinstance(raw_recent, list) or not all(isinstance(n, int) for n in cast(list[object], raw_recent)):
        raise ValueError(f"data queue state: tier {request!r} has a bad number in {dict(entry)}")
    recent = tuple(cast(list[int], raw_recent))
    return TierState(status, projected, attempts, str(entry.get("note", "")), stalls, recent)


RECENT_TICKS = 5


def with_tick(state: TierState, new_units: int) -> TierState:
    """The state after a tick that ran to its time budget and brought `new_units` new (symbol, month) files: the
    count is kept (last RECENT_TICKS) and a slowing download is said in the note (v2.5 N02; `plan.slowing`)."""
    recent = (*state.recent_new, new_units)[-RECENT_TICKS:]
    note = state.note
    if slowing(recent):
        usual = sorted(recent[:3])[1]
        warning = f"slowing: {recent[3]}, {recent[4]} new months in the last two ticks ({usual} before)"
        note = f"{note}; {warning}" if note else warning
    return replace(state, recent_new=recent, note=note)


def unready_datasets(states: Mapping[str, TierState]) -> frozenset[str]:
    """Tier datasets whose download is known to be incomplete (a state entry that is not `done`): research must
    not use them yet. A campaign cell that ran while a tier was still downloading judged the family on a fraction
    of the symbols and spent a pull (c1 round 2, v2.3 step 11). A tier without an entry is not judged here (no
    state file hides nothing); datasets outside the tiers are never listed."""
    return frozenset(d for d in TIERS if d in states and states[d].status != "done")


def usable_datasets(datasets: Sequence[str], unready: frozenset[str], *, keep: Sequence[str]) -> tuple[str, ...]:
    """A family's panel datasets without the unready optional ones (its column is then absent and the cell is
    data_missing until the tier is done). `keep` (the default panel datasets: prices, funding cash flows) are
    never dropped, so no family's backtest silently loses its funding costs."""
    return tuple(d for d in datasets if d in keep or d not in unready)


def visible_coverage(found: Mapping[str, Mapping[str, str]], unready: frozenset[str]) -> dict[str, dict[str, str]]:
    """Research coverage without the unready tiers: a tier still downloading does not reopen its cells."""
    return {w: {d: v for d, v in have.items() if d not in unready} for w, have in found.items()}


def project(*, pilot_bytes: int, pilot_units: int, total_units: int) -> int:
    """Lake bytes of the full tier from the pilot's bytes per symbol-month, with the safety margin."""
    if pilot_units < 1 or total_units < 0 or pilot_bytes < 0:
        raise ValueError(f"invalid pilot ({pilot_bytes} bytes over {pilot_units} units, {total_units} total)")
    return math.ceil(pilot_bytes / pilot_units * total_units * MARGIN)


def decide(
    *, projected: int, lake_bytes: int, free_disk: int, cap: int = LAKE_CAP, reserve: int = DISK_RESERVE
) -> tuple[bool, str]:
    """(run the full tier, why)."""
    if lake_bytes + projected > cap:
        return False, f"lake {lake_bytes / GB:.1f} GB + projected {projected / GB:.1f} GB > cap {cap / GB:.0f} GB"
    if free_disk - projected < reserve:
        return False, f"free disk {free_disk / GB:.1f} GB - {projected / GB:.1f} GB < reserve {reserve / GB:.0f} GB"
    return True, f"projected {projected / GB:.2f} GB fits (lake {lake_bytes / GB:.1f} GB)"


def next_action(queue: Sequence[str], states: Mapping[str, TierState]) -> tuple[str, Action] | None:
    """The first queued tier that is not finished, and its step; a deferred tier only when no other is left (it
    stalled: the others are not held up by it). None when every tier is finished."""
    for deferred_pass in (False, True):
        for request in queue:
            state = states.get(request, TierState())
            if state.status in TERMINAL or (state.status == "deferred") != deferred_pass:
                continue
            if tier_for(request).source == "external":
                return request, "full"
            piloted = state.status in ("piloted", "running") or state.projected_bytes is not None
            return request, "full" if piloted else "pilot"
    return None


def after_exit(state: TierState, action: Action, code: int, *, progressed: bool, gate_reason: str = "") -> TierState:
    """The tier after a tool exit (`code` < 0: killed at the tick's time budget). `progressed`: the catalog gained a
    (symbol, month) file during the tick; `gate_reason`: the download tool's pause reason (its progress note)."""
    if code == 0:
        return replace(state, status="piloted" if action == "pilot" else "done", note="", stalls=0, attempts=0)
    same: Status = "running" if action == "full" else ("pending" if state.status == "deferred" else state.status)
    if progressed:  # it moved: earlier stalls and transient errors no longer count
        state = replace(state, stalls=0, attempts=0)
    if code == 2:  # another bulk job holds the lock: wait for it, not a stall of this tier
        return replace(state, status=same, note="exit 2 (bulk lock held)")
    if code == 3:  # a gate pause: the disk limits are a user decision; heat / RAM pass, never a stall
        if "cap" in gate_reason or "free disk" in gate_reason:
            return replace(state, status="skipped_cap", note=f"paused at the disk limits: {gate_reason}")
        return replace(state, status=same, note=f"exit 3 {gate_reason}".strip())
    if code < 0:  # killed at the tick budget: resumes; without progress for MAX_STALLS ticks it waits behind others
        stalls = 0 if progressed else state.stalls + 1
        if stalls >= MAX_STALLS:
            return replace(state, status="deferred", stalls=0, note=f"deferred: {stalls} ticks without a new file")
        return replace(state, status=same, stalls=stalls, note=f"exit {code}")
    attempts = state.attempts + 1  # 1 error (offline, S3 5xx), 4 failed months
    if attempts >= MAX_ATTEMPTS:  # not the end of the tier (an offline evening would hide its dataset for good):
        return replace(state, status="deferred", attempts=0, note=f"deferred: exit {code} {attempts} times")
    return replace(state, status=same, attempts=attempts, note=f"exit {code}")
