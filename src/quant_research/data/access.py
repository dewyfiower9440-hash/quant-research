"""Point-in-time access (plan step 6; design v2.1 §5.4).

`available_ns` is never stored: it is derived at read time from the dataset's time semantics and a
versioned, monotone lag policy, so re-calibrating the lag never rewrites data.

    event / snapshot     received_ns if observed live, else event_ns + lag   (modelled)
    interval_end         max(event_ns + lag, received_ns)
    interval_start       max(event_ns + interval + lag, received_ns)
    unknown_interval     same as interval_start (conservative: assume the stamp is the start)

As-of answers are the row with the greatest `event_ns` among rows available at the decision: a later
revision of an OLD bucket stays in the data but never replaces a newer bucket (review #3); a revision of
the newest bucket replaces it once available. Research may not pin a superseded (smaller) lag version
unless it explicitly reproduces an archived run (review #4).

The research role may not ask for decisions at/after the seal boundary and never receives a row that
only becomes available inside the sealed holdout. Only the validator role may read the holdout.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

import polars as pl

from quant_research.catalog.store import Catalog
from quant_research.errors import AppError

Role = Literal["research", "validator"]


class SealViolationError(AppError):
    """A research query reached into the sealed holdout."""


class PointInTime:
    def __init__(
        self,
        catalog: Catalog,
        *,
        role: Role = "research",
        lag_versions: Mapping[str, int] | None = None,
        reproduce_archived_run: bool = False,
    ) -> None:
        self.catalog = catalog
        self.role = role
        self.lag_versions = dict(lag_versions or {})
        self.reproduce_archived_run = reproduce_archived_run

    def _semantics(self, dataset_id: str) -> tuple[str, int]:
        row = self.catalog.conn.execute(
            "select time_semantics, coalesce(interval_ms, 0) from dataset where dataset_id = ?", (dataset_id,)
        ).fetchone()
        if row is None:
            raise AppError(f"unknown dataset {dataset_id!r}")
        return str(row[0]), int(row[1]) * 1_000_000

    def with_available(self, frame: pl.DataFrame, dataset_id: str) -> pl.DataFrame:
        """Add `available_ns` (Int64) to a frame with `event_ns` and optionally `received_ns`."""
        semantics, interval_ns = self._semantics(dataset_id)
        pinned = self.lag_versions.get(dataset_id)
        lag = self.catalog.lag_ns(dataset_id, pinned)
        if pinned is not None and self.role == "research" and not self.reproduce_archived_run:
            latest = self.catalog.lag_ns(dataset_id)
            if lag < latest:
                raise SealViolationError(
                    f"lag version {pinned} of {dataset_id} ({lag} ns) is superseded by a larger lag ({latest} ns); "
                    "pass reproduce_archived_run=True only to replay an archived run"
                )
        event = pl.col("event_ns").cast(pl.Int64)
        has_received = "received_ns" in frame.columns
        received = pl.col("received_ns").cast(pl.Int64) if has_received else pl.lit(None, dtype=pl.Int64)
        if semantics in ("event", "snapshot"):
            modelled = event + lag
            available = pl.coalesce(received, modelled) if has_received else modelled
        else:
            shift = lag if semantics == "interval_end" else interval_ns + lag
            modelled = event + shift
            available = pl.max_horizontal(modelled, received) if has_received else modelled
        return frame.with_columns(available.alias("available_ns"))

    def check_decisions(self, dataset_id: str, latest_decision_ns: int) -> None:
        """Research may only decide strictly before the dataset's seal boundary (sealed holdout)."""
        if self.role != "research":
            return
        boundary = self.catalog.seal(dataset_id).research_until_ns
        if latest_decision_ns >= boundary:
            raise SealViolationError(
                f"decision at {latest_decision_ns} is not before research_until {boundary} for {dataset_id} "
                "(sealed holdout)"
            )

    def check_span(self, dataset_id: str, first_ns: int, last_ns: int) -> None:
        """Research: the whole span a run reads (warm-up included) and decides in must lie inside ONE research
        window (ADR-040: a warm-up reaching into a holdout, or a run across a holdout, is refused, not trimmed)."""
        if self.role != "research":
            return
        self.check_decisions(dataset_id, last_ns)
        windows = self.catalog.research_windows(dataset_id)
        if not any(lo <= first_ns and last_ns < hi for lo, hi in windows):
            raise SealViolationError(
                f"span [{first_ns}, {last_ns}] of {dataset_id} is not inside one research window {windows}"
            )

    def rows_visible(self, frame: pl.DataFrame, dataset_id: str) -> pl.DataFrame:
        """Rows this role may see at all (research: available inside a research window of the latest epoch)."""
        out = self.with_available(frame, dataset_id)
        if self.role == "research":
            # the EVENT and the availability must lie in the same research window: a holdout bar that only
            # becomes available after the window starts is still holdout data (B1/B2 review)
            inside = pl.lit(False)
            for lo, hi in self.catalog.research_windows(dataset_id):
                in_window = (pl.col("available_ns") >= lo) & (pl.col("available_ns") < hi)
                if "event_ns" in out.columns:
                    in_window = in_window & (pl.col("event_ns").cast(pl.Int64).fill_null(lo) >= lo)
                inside = inside | in_window
            out = out.filter(inside)
        return out

    def join_asof(
        self,
        decisions: pl.Series | pl.DataFrame,
        frame: pl.DataFrame,
        dataset_id: str,
        *,
        by: str | None = None,
    ) -> pl.DataFrame:
        """For each decision time, the latest row available at or before it (backward as-of join)."""
        left = decisions.to_frame() if isinstance(decisions, pl.Series) else decisions
        if "decision_ns" not in left.columns:
            raise AppError("decisions need a 'decision_ns' column")
        if not left.schema["decision_ns"].is_integer():
            raise AppError(f"decision_ns must be integer nanoseconds, got {left.schema['decision_ns']}")
        latest = left["decision_ns"].cast(pl.Int64).max()  # integer dtype checked above
        earliest = left["decision_ns"].cast(pl.Int64).min()
        if isinstance(latest, int) and isinstance(earliest, int):
            self.check_span(dataset_id, earliest, latest)
        # Stable sort on (available, event); then drop rows whose event is older than one already
        # available (revisions of old buckets), so the backward as-of match is the newest event.
        right = self.rows_visible(frame, dataset_id).sort(["available_ns", "event_ns"], maintain_order=True)
        frontier = pl.col("event_ns").cum_max().shift(1)
        if by is not None:
            frontier = frontier.over(by)
        # rows without an event time are kept (they join on receive time as before)
        keep = frontier.is_null() | pl.col("event_ns").is_null() | (pl.col("event_ns") >= frontier)
        right = right.filter(keep)
        left = left.with_columns(pl.col("decision_ns").cast(pl.Int64)).sort("decision_ns")
        # Both sides are sorted on the as-of key just above. Polars cannot verify sortedness inside `by`
        # groups (in-memory engine), so the check is skipped only then; the guarantee is this sort and the
        # shuffled-input test in tests/data/test_access.py.
        return left.join_asof(
            right,
            left_on="decision_ns",
            right_on="available_ns",
            strategy="backward",
            by=by,
            coalesce=False,
            check_sortedness=by is None,
        )
