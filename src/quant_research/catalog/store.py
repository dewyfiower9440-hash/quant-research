"""Catalog store (plan step 5): instruments, datasets, gaps, seal epochs, universe membership, lag policy.

One writer (the owning process) per SQLite file (design v2.1 §3.1); other processes open read-only.
Immutability and monotone lag are enforced twice: here (clear errors) and by SQL triggers (so a direct
write cannot bypass them).
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Literal, cast

from quant_research.adapters.sqlite import connect, migrate
from quant_research.errors import AppError

# Literal per-table row counts for backup snapshots and restore drills (no dynamic SQL); a test keeps
# this in step with the migrations.
ROW_COUNT_SQL: dict[str, str] = {
    "instrument": "select count(*) from instrument",
    "dataset": "select count(*) from dataset",
    "gap": "select count(*) from gap",
    "seal_policy": "select count(*) from seal_policy",
    "seal_window": "select count(*) from seal_window",
    "universe_membership": "select count(*) from universe_membership",
    "lag_policy": "select count(*) from lag_policy",
    "compacted_segment": "select count(*) from compacted_segment",
    "bulk_file": "select count(*) from bulk_file",
    "backfill_chunk": "select count(*) from backfill_chunk",
    "backfill_done": "select count(*) from backfill_done",
    "gapscan_segment": "select count(*) from gapscan_segment",
    "incident": "select count(*) from incident",
}

MarketType = Literal["spot", "usdm_perp", "usdm_future"]
TimeSemantics = Literal["event", "interval_start", "interval_end", "snapshot", "unknown_interval"]
GAP_CAUSES = frozenset(
    {
        "restart",
        "reconnect",
        "renewal",
        "api_window_exceeded",
        "no_trade",
        "exchange_outage",
        "clock_suspect",
        "unknown",
    }
)


class CatalogError(AppError):
    """A catalog invariant would be violated."""


@dataclass(frozen=True, slots=True)
class Instrument:
    venue: str
    venue_symbol: str
    base: str
    quote: str
    market_type: MarketType
    onboard_ns: int | None = None
    delist_ns: int | None = None
    contract_multiplier: float = 1.0


@dataclass(frozen=True, slots=True)
class Dataset:
    dataset_id: str
    venue: str
    time_semantics: TimeSemantics
    interval_ms: int | None
    description: str


@dataclass(frozen=True, slots=True)
class LagPolicy:
    dataset_id: str
    lag_ns: int
    rationale: str


SealRole = Literal["research", "holdout_hist", "holdout_fwd"]
MIN_NS = -(2**63)


@dataclass(frozen=True, slots=True)
class SealWindow:
    role: SealRole
    from_ns: int
    to_ns: int  # exclusive


@dataclass(frozen=True, slots=True)
class SealEpoch:
    dataset_id: str
    epoch: int
    research_until_ns: int
    holdout_until_ns: int
    rationale: str


@dataclass(frozen=True, slots=True)
class GapImport:
    imported: int
    skipped_lines: int


class Catalog:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @classmethod
    def open(cls, path: Path) -> Catalog:
        conn = connect(path)
        migrate(conn, "quant_research.catalog")
        return cls(conn)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Generator[None]:
        """All-or-nothing group of writes (`begin immediate`: takes the write lock up front, waits up to
        busy_timeout for other writers such as the rest worker's compaction)."""
        self.conn.execute("begin immediate")
        try:
            yield
            self.conn.execute("commit")  # a failing commit (e.g. deferred FK) is rolled back below
        except BaseException:
            if self.conn.in_transaction:  # SQLite may already have rolled back on its own
                self.conn.execute("rollback")
            raise

    def schema_version(self) -> int:
        return int(self.conn.execute("pragma user_version").fetchone()[0])

    # ------------------------------------------------------------------ instruments
    def add_instrument(self, i: Instrument) -> int:
        cur = self.conn.execute(
            "insert into instrument(venue, venue_symbol, base, quote, market_type, contract_multiplier, onboard_ns, "
            "delist_ns) values (?,?,?,?,?,?,?,?)",
            (i.venue, i.venue_symbol, i.base, i.quote, i.market_type, i.contract_multiplier, i.onboard_ns, i.delist_ns),
        )
        return int(cast(int, cur.lastrowid))

    def link_successor(self, old_id: int, new_id: int) -> None:
        self.conn.execute("update instrument set successor_id = ? where instrument_id = ?", (new_id, old_id))

    def lineage(self, instrument_id: int) -> list[int]:
        """Predecessors first, ending with `instrument_id` (rebrands such as MATIC -> POL)."""
        chain = [instrument_id]
        while True:
            row = self.conn.execute(
                "select instrument_id from instrument where successor_id = ?", (chain[0],)
            ).fetchone()
            if row is None:
                return chain
            chain.insert(0, int(row[0]))

    # ------------------------------------------------------------------ datasets & lag
    def register_dataset(self, d: Dataset) -> None:
        self.conn.execute(
            "insert into dataset(dataset_id, venue, time_semantics, interval_ms, description, registered_ns) "
            "values (?,?,?,?,?,?)",
            (d.dataset_id, d.venue, d.time_semantics, d.interval_ms, d.description, time.time_ns()),
        )

    def add_lag_policy(self, p: LagPolicy) -> int:
        row = self.conn.execute(
            "select coalesce(max(version), 0), coalesce(max(lag_ns), 0) from lag_policy where dataset_id = ?",
            (p.dataset_id,),
        ).fetchone()
        version, current = int(row[0]) + 1, int(row[1])
        if p.lag_ns < current:
            raise CatalogError(
                f"lag policy may only grow for {p.dataset_id}: {p.lag_ns} < current {current} (design v2.1 §5.4)"
            )
        self.conn.execute(
            "insert into lag_policy(dataset_id, version, lag_ns, rationale, decided_ns) values (?,?,?,?,?)",
            (p.dataset_id, version, p.lag_ns, p.rationale, time.time_ns()),
        )
        return version

    def lag_ns(self, dataset_id: str, version: int | None = None) -> int:
        if version is None:
            row = self.conn.execute(
                "select lag_ns from lag_policy where dataset_id = ? order by version desc limit 1", (dataset_id,)
            ).fetchone()
        else:
            row = self.conn.execute(
                "select lag_ns from lag_policy where dataset_id = ? and version = ?", (dataset_id, version)
            ).fetchone()
        if row is None:
            raise CatalogError(f"no lag policy for {dataset_id} (version {version})")
        return int(row[0])

    # ------------------------------------------------------------------ seal epochs
    def add_seal_epoch(self, e: SealEpoch) -> None:
        if e.holdout_until_ns <= e.research_until_ns:
            raise CatalogError(f"holdout must end after research for {e.dataset_id} epoch {e.epoch}")
        prev = self.conn.execute(
            "select epoch, research_until_ns from seal_policy where dataset_id = ? order by epoch desc limit 1",
            (e.dataset_id,),
        ).fetchone()
        if prev is not None:
            if e.epoch != int(prev[0]) + 1:
                raise CatalogError(f"epochs are consecutive: expected {int(prev[0]) + 1}, got {e.epoch}")
            if e.research_until_ns < int(prev[1]):
                raise CatalogError(f"seal boundary may not move backwards for {e.dataset_id}")
        self.conn.execute(
            "insert into seal_policy(dataset_id, epoch, research_until_ns, holdout_until_ns, rationale, decided_ns) "
            "values (?,?,?,?,?,?)",
            (e.dataset_id, e.epoch, e.research_until_ns, e.holdout_until_ns, e.rationale, time.time_ns()),
        )

    def add_seal_windows(self, e: SealEpoch, windows: Sequence[SealWindow]) -> None:
        """Register an epoch made of windows (ADR-040), in one transaction: the epoch row (same rules as
        `add_seal_epoch`) and its windows. Windows must not overlap; research windows end by `research_until_ns`,
        every window by `holdout_until_ns`."""
        if not windows or not any(w.role == "research" for w in windows):
            raise CatalogError(f"epoch {e.epoch} of {e.dataset_id} needs at least one research window")
        ordered = sorted(windows, key=lambda w: w.from_ns)
        for a, b in pairwise(ordered):
            if b.from_ns < a.to_ns:
                raise CatalogError(f"seal windows overlap for {e.dataset_id}: {a} and {b}")
        for w in ordered:
            if w.to_ns <= w.from_ns:
                raise CatalogError(f"empty seal window {w} for {e.dataset_id}")
            if w.role == "research" and w.to_ns > e.research_until_ns:
                raise CatalogError(f"research window {w} ends after research_until for {e.dataset_id}")
            if w.to_ns > e.holdout_until_ns:
                raise CatalogError(f"seal window {w} ends after holdout_until for {e.dataset_id}")
        with self.transaction():
            self.add_seal_epoch(e)
            for w in ordered:
                self.conn.execute(
                    "insert into seal_window(dataset_id, epoch, role, from_ns, to_ns) values (?,?,?,?,?)",
                    (e.dataset_id, e.epoch, w.role, w.from_ns, w.to_ns),
                )

    def research_windows(self, dataset_id: str) -> list[tuple[int, int]]:
        """[from, to) research windows of the dataset's latest epoch; an epoch without windows means
        everything before its `research_until_ns` (epoch-1 semantics)."""
        e = self.seal(dataset_id)
        rows = self.conn.execute(
            "select from_ns, to_ns from seal_window where dataset_id = ? and epoch = ? and role = 'research' "
            "order by from_ns",
            (dataset_id, e.epoch),
        ).fetchall()
        if not rows:
            return [(MIN_NS, e.research_until_ns)]
        return [(int(r[0]), int(r[1])) for r in rows]

    def seal(self, dataset_id: str) -> SealEpoch:
        row = self.conn.execute(
            "select epoch, research_until_ns, holdout_until_ns, rationale from seal_policy where dataset_id = ? "
            "order by epoch desc limit 1",
            (dataset_id,),
        ).fetchone()
        if row is None:
            raise CatalogError(f"no seal policy for {dataset_id}: research access is not allowed until one exists")
        return SealEpoch(dataset_id, int(row[0]), int(row[1]), int(row[2]), str(row[3]))

    # ------------------------------------------------------------------ universe
    def add_membership(self, instrument_id: int, rule_version: str, *, from_ns: int, to_ns: int | None) -> None:
        if to_ns is not None and to_ns <= from_ns:
            raise CatalogError(f"empty membership interval [{from_ns}, {to_ns}) for instrument {instrument_id}")
        self.conn.execute(
            "insert into universe_membership(instrument_id, rule_version, from_ns, to_ns) values (?,?,?,?)",
            (instrument_id, rule_version, from_ns, to_ns),
        )

    def members(self, rule_version: str, *, at_ns: int) -> list[int]:
        rows = self.conn.execute(
            "select instrument_id from universe_membership where rule_version = ? and from_ns <= ? "
            "and (to_ns is null or to_ns > ?) order by instrument_id",
            (rule_version, at_ns, at_ns),
        ).fetchall()
        return [int(r[0]) for r in rows]

    # ------------------------------------------------------------------ gaps
    def import_gaps(self, raw_root: Path) -> GapImport:
        """Load `raw/_gaps/*.jsonl` (written by the collectors) idempotently; torn lines are counted, not guessed."""
        imported = skipped = 0
        now = time.time_ns()
        for path in sorted((raw_root / "_gaps").glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    rec = cast(dict[str, object], json.loads(line))
                except ValueError:
                    skipped += 1
                    continue
                cause, _, detail = str(rec["cause"]).partition(":")
                if cause not in GAP_CAUSES:
                    raise CatalogError(f"unknown gap cause {cause!r} in {path.name}")
                start = rec.get("start_ns")
                cur = self.conn.execute(
                    "insert or ignore into gap(source, start_ns, end_ns, cause, detail, recorded_ns) "
                    "values (?,?,?,?,?,?)",
                    (
                        str(rec["source"]),
                        start if isinstance(start, int) else None,
                        int(cast(int, rec["end_ns"])),
                        cause,
                        detail or None,
                        now,
                    ),
                )
                imported += cur.rowcount
        return GapImport(imported, skipped)
