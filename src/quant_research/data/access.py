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

    def rows_visible(self, frame: pl.DataFrame, dataset_id: str) -> pl.DataFrame:
        """Rows this role may see at all (research: available strictly before the seal boundary)."""
        out = self.with_available(frame, dataset_id)
        if self.role == "research":
            out = out.filter(pl.col("available_ns") < self.catalog.seal(dataset_id).research_until_ns)
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
        if self.role == "research":
            boundary = self.catalog.seal(dataset_id).research_until_ns
            latest = left["decision_ns"].cast(pl.Int64).max()  # integer dtype checked above
            if isinstance(latest, int) and latest >= boundary:
                raise SealViolationError(
                    f"decision at {latest} is not before research_until {boundary} for {dataset_id} (sealed holdout)"
                )
        # Stable sort on (available, event); then drop rows whose event is older than one already
        # available (revisions of old buckets), so the backward as-of match is the newest event.
        right = self.rows_visible(frame, dataset_id).sort(["available_ns", "event_ns"], maintain_order=True)
        frontier = pl.col("event_ns").cum_max().shift(1)
        if by is not None:
            frontier = frontier.over(by)
        right = right.filter(frontier.is_null() | (pl.col("event_ns") >= frontier))
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
