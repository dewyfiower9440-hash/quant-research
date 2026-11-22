"""Autonomous research campaign, pure core (v2.2 B7, design delta D8, ADR-047).

A cell is one experiment setting: (family, timeframe, universe top-M, research window). Every search batch on a
cell ends with exactly one cause code; the transition rules turn the cause into the next experiment instead of
stopping ("failure is not the end", root cause 4):

    cost_dominated -> the same cell on slower bars (1h -> 4h -> 1d)
    sparse         -> the same cell on a wider universe (top 30 -> top 100)
    unstable       -> the same cell on the other research window
    data_missing   -> a data request (the collection queue); the cell retires until the data exists
    no_signal      -> nothing; the bandit learns the cell is poor
    candidate      -> the cell's best S3 survivor goes to the validator; the cell retires

Queued transition targets run first, then a discounted Thompson bandit over the cells that are not retired. A cell
retires after `max_pulls` batches, after a candidate or after data_missing. When nothing is eligible the campaign
does not end (ADR-049): `next_round` reopens the data_missing cells whose data has arrived and adds the next
pre-registered grid group; only when neither exists does the caller stop with `waiting_data` (the next scheduled
tick computes the round again). Everything here is pure and JSON-serialisable (restart = same decisions).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, cast

from quant_research.search.bandit import FamilyBandit

CauseCode = Literal["candidate", "no_signal", "cost_dominated", "sparse", "unstable", "data_missing", "error"]
TIMEFRAMES = ("1h", "4h", "1d")
TOP_MS = (30, 100)
WINDOWS = ("w2", "w1")
# when the cells ask for nothing in particular: the tiers of ADR-044 / D9 in their pre-registered order
DEFAULT_DATA_TIERS = (
    "binance.um.bulk.funding",
    "binance.um.bulk.metrics_5m",
    "binance.spot.bulk.klines_1m",
    "binance.um.bulk.book_depth",
    "binance.um.bulk.agg_flow_1m",
    "external (alternative.fng_1d, coinmetrics.asset_1d, deribit.dvol_1h)",
)


@dataclass(frozen=True, slots=True, order=True)
class Cell:
    family: str  # template family (strategy/templates.py)
    timeframe: str
    top_m: int
    window: str  # research window of seal epoch 2: "w1" [2020-01, 2023-01) or "w2" [2024-12, 2026-07)

    def __post_init__(self) -> None:
        if self.timeframe not in TIMEFRAMES:
            raise ValueError(f"cell timeframe must be one of {TIMEFRAMES}, got {self.timeframe!r}")
        if self.top_m < 1 or self.window not in WINDOWS:
            raise ValueError(f"invalid cell {self}")

    @property
    def key(self) -> str:
        return f"{self.family}@{self.timeframe}|top{self.top_m}|{self.window}"

    @property
    def space(self) -> str:
        return f"{self.family}@{self.timeframe}"

    @staticmethod
    def parse(key: str) -> Cell:
        try:
            space, top, window = key.split("|")
            family, timeframe = space.split("@")
            return Cell(family, timeframe, int(top.removeprefix("top")), window)
        except ValueError as e:
            raise ValueError(f"not a cell key: {key!r}") from e


@dataclass(frozen=True, slots=True)
class BatchSummary:
    """What a search batch left, reduced to the numbers the cause rules need (filled by the runner)."""

    s1_scored: int  # S1 trials
    s1_finite: int  # S1 trials with a defined score (enough events, B1)
    s2_done: int
    s2_exposed: int  # done S2 runs with mean gross exposure >= the minimum (the S3-eligible ones)
    best_sr_net: float | None  # best S3-eligible S2 daily SR after costs and funding
    best_total_return: float | None  # of that S2 run, after costs
    best_cost_fraction: float | None  # its trading costs / capital
    best_gross_exposure: float | None
    s3_sr: float | None  # the S3 of that best S2 on the confirmation split (None: it did not run / was refused)
    data_missing: tuple[str, ...]  # datasets the cell's family needs that had no rows in the panel
    s3_threshold: float = 0.0  # expected max SR under the null of this campaign's S3 runs on the same split
    s3_trial_id: int | None = None  # the candidate's S3 trial (what B8 submits)
    error: str = ""  # the exception of a failed batch
    # ADR-049 decision 3: the same spec's S3 on the other research window's confirmation split, the SR of both
    # splits' daily pnl joined, and the best-of-N null bar of every S3 judged on the two splits
    cross_sr: float | None = None
    pooled_sr: float | None = None
    pooled_threshold: float = 0.0
    cross_trial_id: int | None = None


def classify(b: BatchSummary, *, min_exposure: float) -> CauseCode:  # min_exposure: applied by the runner
    """Exactly one cause per batch, checked in this order."""
    if b.error:
        return "error"
    if b.data_missing:
        return "data_missing"
    if b.s1_finite == 0 or b.s2_done == 0:
        return "no_signal"
    if b.s2_exposed == 0 or b.best_sr_net is None:
        return "sparse"  # nothing in the market often enough to be judged (ADR-042 needs 5 % exposure)
    if b.best_sr_net <= 0:
        gross = (b.best_total_return or 0.0) + (b.best_cost_fraction or 0.0)
        return "cost_dominated" if gross > 0 else "no_signal"  # positive before costs, negative after
    if b.s3_sr is None or b.s3_sr <= 0:
        return "unstable"  # positive on selection, not on its own confirmation split
    if b.cross_sr is None or b.cross_sr <= 0 or b.pooled_sr is None or b.pooled_sr <= b.pooled_threshold:
        return "unstable"  # ADR-049: signs disagree across the windows, or the pooled SR is within the null bar
    return "candidate"


TRANSITION_CAUSES: tuple[CauseCode, ...] = ("cost_dominated", "sparse", "unstable")

# ADR-049: the pre-registered grid, one group per round, in this order (windows, timeframes, top-M)
GRID_GROUPS: tuple[tuple[tuple[str, ...], tuple[str, ...], tuple[int, ...]], ...] = (
    (("w2",), ("4h", "1d"), (100,)),
    (("w1",), ("4h", "1d"), (100,)),
    (("w2", "w1"), ("1h",), (100,)),
    (("w2", "w1"), TIMEFRAMES, (30,)),
)


def preregistered_groups(families: Sequence[str]) -> list[list[Cell]]:
    """The grid groups G1..G4 of ADR-049 for these families (deterministic order)."""
    return [
        [Cell(f, tf, m, w) for w in windows for tf in tfs for m in tops for f in families]
        for windows, tfs, tops in GRID_GROUPS
    ]


@dataclass(frozen=True, slots=True)
class RoundPlan:
    reopened: tuple[str, ...]  # data_missing cells whose datasets now have rows in their window
    added: tuple[str, ...]  # the next grid group's cells not yet in the campaign
    waiting: tuple[str, ...]  # datasets still asked for by retired data_missing cells

    @property
    def empty(self) -> bool:
        return not self.reopened and not self.added


Availability = Mapping[str, Mapping[str, str]]  # window -> {dataset: version} with research rows in that window


def _versions(cell: Cell, needs: Sequence[str], available: Availability) -> dict[str, str] | None:
    """The versions of every needed dataset in the cell's window, or None when one is still missing."""
    have = available.get(cell.window, {})
    if not all(d in have for d in needs):
        return None
    return {d: have[d] for d in needs}


def transitions(cell: Cell, cause: CauseCode) -> list[Cell]:
    match cause:
        case "cost_dominated":
            i = TIMEFRAMES.index(cell.timeframe)
            return [] if i + 1 == len(TIMEFRAMES) else [Cell(cell.family, TIMEFRAMES[i + 1], cell.top_m, cell.window)]
        case "sparse":
            wider = [m for m in TOP_MS if m > cell.top_m]
            return [Cell(cell.family, cell.timeframe, wider[0], cell.window)] if wider else []
        case "unstable":
            return [Cell(cell.family, cell.timeframe, cell.top_m, w) for w in WINDOWS if w != cell.window]
        case _:
            return []


@dataclass
class CampaignState:
    run_id: str
    max_pulls: int
    cells: list[str]  # every cell key ever eligible (initial grid + transition targets), in order of arrival
    pulls: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    retired: list[str] = field(default_factory=lambda: list[str]())
    queue: list[str] = field(default_factory=lambda: list[str]())
    candidates: list[str] = field(default_factory=lambda: list[str]())
    data_requests: list[str] = field(default_factory=lambda: list[str]())
    history: list[dict[str, object]] = field(default_factory=lambda: list[dict[str, object]]())
    settings: dict[str, object] = field(default_factory=lambda: dict[str, object]())  # grid + config (resume check)
    round: int = 1  # ADR-049
    pull_base: dict[str, int] = field(default_factory=lambda: dict[str, int]())  # pulls when a cell was reopened
    data_versions: dict[str, dict[str, str]] = field(  # dataset versions a reopened cell was opened with
        default_factory=lambda: dict[str, dict[str, str]]()
    )

    @staticmethod
    def new(run_id: str, cells: Iterable[Cell], *, max_pulls: int = 3) -> CampaignState:
        if max_pulls < 1:
            raise ValueError(f"max_pulls must be >= 1, got {max_pulls}")
        keys = list(dict.fromkeys(c.key for c in cells))
        if not keys:
            raise ValueError("a campaign needs at least one cell")
        return CampaignState(run_id, max_pulls, keys)

    def all_cells(self) -> list[Cell]:
        """Every cell the campaign may run in this round: its cells plus every transition target. A round adds cells
        that no transition reaches (1h, top30), so the caller extends its bandit with these after `start_round`."""
        out = dict.fromkeys(self.cells)
        frontier = [Cell.parse(k) for k in self.cells]
        while frontier:  # one closure over every transition kind (mixed paths: slower bars, then wider, ...)
            nxt: list[Cell] = []
            for c in frontier:
                for cause in TRANSITION_CAUSES:
                    for t in transitions(c, cause):
                        if t.key not in out:
                            out[t.key] = None
                            nxt.append(t)
            frontier = nxt
        return [Cell.parse(k) for k in out]

    def eligible(self) -> list[str]:
        return [k for k in self.cells if k not in self.retired]

    def next_cell(self, bandit: FamilyBandit, *, seed: int, seq: int) -> Cell | None:
        """A queued transition target first, else the bandit's pick among eligible cells; None = campaign over."""
        while self.queue:
            key = self.queue[0]
            if key not in self.retired:
                return Cell.parse(key)
            self.queue.pop(0)
        eligible = self.eligible()
        if not eligible:
            return None
        key, _why = bandit.choose(eligible, seed=seed, seq=seq)
        return Cell.parse(key)

    def record(self, cell: Cell, cause: CauseCode, summary: BatchSummary, *, reward: float) -> None:
        key = cell.key
        if key not in self.cells:
            self.cells.append(key)
        if self.queue and self.queue[0] == key:
            self.queue.pop(0)
        self.pulls[key] = self.pulls.get(key, 0) + 1
        self.history.append({"cell": key, "cause": cause, "reward": reward, "summary": _summary_json(summary)})
        for target in transitions(cell, cause):
            if target.key not in self.retired and target.key not in self.queue:
                self.queue.append(target.key)
                if target.key not in self.cells:
                    self.cells.append(target.key)
        if cause == "candidate":
            self.candidates.append(key)
        if cause == "data_missing":
            for ds in summary.data_missing:
                if ds not in self.data_requests:
                    self.data_requests.append(ds)
        used = self.pulls[key] - self.pull_base.get(key, 0)
        done = cause in ("candidate", "data_missing", "error") or used >= self.max_pulls
        if done and key not in self.retired:
            self.retired.append(key)

    def start_round(self, plan: RoundPlan, available: Availability) -> None:
        """Apply `next_round`'s plan: reopen (with a fresh pull budget), add the group, keep the open requests."""
        self.round += 1
        for key in plan.reopened:
            needs = _last_missing(self.history, key)
            versions = _versions(Cell.parse(key), needs, available)
            if key in self.retired:
                self.retired.remove(key)
            self.pull_base[key] = self.pulls.get(key, 0)
            self.data_versions[key] = {**self.data_versions.get(key, {}), **(versions or {})}  # every version seen
        for key in plan.added:
            if key not in self.cells:
                self.cells.append(key)
        self.data_requests = list(plan.waiting)

    def to_json(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "max_pulls": self.max_pulls,
            "cells": list(self.cells),
            "pulls": dict(self.pulls),
            "retired": list(self.retired),
            "queue": list(self.queue),
            "candidates": list(self.candidates),
            "data_requests": list(self.data_requests),
            "history": list(self.history),
            "settings": dict(self.settings),
            "round": self.round,
            "pull_base": dict(self.pull_base),
            "data_versions": {k: dict(v) for k, v in self.data_versions.items()},
        }

    @staticmethod
    def from_json(raw: Mapping[str, object]) -> CampaignState:
        def strings(name: str) -> list[str]:
            return [str(v) for v in cast(Sequence[object], raw.get(name, []))]

        pulls = {str(k): int(cast(int, v)) for k, v in cast(Mapping[str, object], raw.get("pulls", {})).items()}
        history = [dict(cast(Mapping[str, object], h)) for h in cast(Sequence[object], raw.get("history", []))]
        base = {str(k): int(cast(int, v)) for k, v in cast(Mapping[str, object], raw.get("pull_base", {})).items()}
        versions = {
            str(k): {str(d): str(x) for d, x in cast(Mapping[str, object], v).items()}
            for k, v in cast(Mapping[str, object], raw.get("data_versions", {})).items()
        }
        return CampaignState(
            str(raw["run_id"]),
            int(cast(int, raw["max_pulls"])),
            strings("cells"),
            pulls,
            strings("retired"),
            strings("queue"),
            strings("candidates"),
            strings("data_requests"),
            history,
            dict(cast(Mapping[str, object], raw.get("settings", {}))),
            int(cast(int, raw.get("round", 1))),  # a state saved before ADR-049 is in its first round
            base,
            versions,
        )


def _last_missing(history: Sequence[Mapping[str, object]], key: str) -> tuple[str, ...]:
    """The datasets the cell's most recent data_missing batch asked for."""
    for h in reversed(history):
        if h.get("cell") == key:
            summary = h.get("summary")
            if h.get("cause") != "data_missing" or not isinstance(summary, Mapping):
                return ()
            raw = cast(Mapping[str, object], summary).get("data_missing", [])
            return tuple(str(d) for d in cast(Sequence[object], raw))
    return ()


def next_round(state: CampaignState, available: Availability, groups: Sequence[Sequence[Cell]]) -> RoundPlan:
    """ADR-049 round: (1) retired data_missing cells whose datasets now have rows in their window under versions
    they were not opened with, (2) the first grid group with cells not yet in the campaign; else only `waiting`."""
    reopened: list[str] = []
    waiting: list[str] = []
    for key in state.retired:
        needs = _last_missing(state.history, key)
        if not needs:
            continue  # retired by pulls, a candidate or an error: never reopened
        versions = _versions(Cell.parse(key), needs, available)
        opened = state.data_versions.get(key, {})
        if versions is not None and any(opened.get(d) != v for d, v in versions.items()):
            reopened.append(key)  # some needed dataset is new or changed since the cell was last opened
            continue
        if versions is None:  # (a dataset present under the version that already failed is not waited for)
            have = available.get(Cell.parse(key).window, {})
            waiting.extend(d for d in needs if d not in have and d not in waiting)
    known = set(state.cells)
    added: list[str] = []
    for group in groups:
        fresh = [c.key for c in group if c.key not in known]
        if fresh:
            added = list(dict.fromkeys(fresh))
            break
    return RoundPlan(tuple(reopened), tuple(added), tuple(waiting))


def _summary_json(s: BatchSummary) -> dict[str, object]:
    return {
        "s1_scored": s.s1_scored,
        "s1_finite": s.s1_finite,
        "s2_done": s.s2_done,
        "s2_exposed": s.s2_exposed,
        "best_sr_net": s.best_sr_net,
        "best_total_return": s.best_total_return,
        "best_cost_fraction": s.best_cost_fraction,
        "best_gross_exposure": s.best_gross_exposure,
        "s3_sr": s.s3_sr,
        "data_missing": list(s.data_missing),
        "s3_threshold": s.s3_threshold,
        "s3_trial_id": s.s3_trial_id,
        "error": s.error,
        "cross_sr": s.cross_sr,
        "pooled_sr": s.pooled_sr,
        "pooled_threshold": s.pooled_threshold,
        "cross_trial_id": s.cross_trial_id,
    }


def negative_report(state: CampaignState) -> dict[str, object]:
    """What an ended campaign found: batches, causes, candidates, and which data to collect next."""
    causes = Counter(str(h["cause"]) for h in state.history)
    asked = list(state.data_requests)
    next_data = asked + [t for t in DEFAULT_DATA_TIERS if t not in asked]
    best: dict[str, float] = {}
    for h in state.history:
        summary = cast(Mapping[str, object], h.get("summary", {}))
        sr = summary.get("best_sr_net")
        if isinstance(sr, int | float):
            best[str(h["cell"])] = max(best.get(str(h["cell"]), float("-inf")), float(sr))
    return {
        "run_id": state.run_id,
        "batches": len(state.history),
        "cells_tried": len(state.pulls),
        "causes": dict(causes),
        "candidates": list(state.candidates),
        "best_sr_net_by_cell": dict(sorted(best.items(), key=lambda kv: -kv[1])[:20]),
        "next_data": next_data,
    }
